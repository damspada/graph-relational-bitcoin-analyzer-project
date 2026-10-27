"""
Scalability curves on the Neo4j side: the exact counterpart of
benchmark/run_scaling_sqlite.py, on the same sub-datasets produced by
make_subsets.py, so that the two curves are directly comparable.

For every size it measures in sequence:
  1. ingestion time (loader/load_neo4j.py::load, timed here, repeated
     --repeats times so the measurement carries a median and not a
     single sample);
  2. size on disk of the datastore;
  3. prices and clustering, the two prerequisites of Q8 and Q7: without
     them those queries match nothing and their timing would be
     meaningless;
  4. execution time of the 8 queries (benchmark/bench_queries.py).

Why this script exists separately from the SQLite one
-----------------------------------------------------
On SQLite a size is rebuilt by deleting a file. On Neo4j the database is
a running server whose store has to be wiped from the filesystem with
the container stopped (see wipe_neo4j below for why Cypher is not an
option). That difference is the reason the two orchestrators are not one
script with a flag: they share the measuring code (bench_queries) but
not the setup.

Cost warning
------------
Loading 977 blocks into Neo4j takes about 40 minutes, so the four sizes
together take over an hour of pure ingestion before any query is timed,
multiplied by --repeats.

The clustering IS computed per size and its own cost is recorded as
cluster_s, because at full scale it is the dominant part of the Q7
story. Pass --cluster-sizes none to skip it and get the curve faster.
"""
import argparse
import csv
import glob
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "loader")      # load_neo4j.py does "from common import ...", it assumes this path
sys.path.insert(0, "clustering")  # cluster_neo4j.py is imported the same way
from load_neo4j import load as load_neo4j
from load_prices import load_neo4j as load_prices_neo4j
from cluster_neo4j import run as cluster_neo4j
from benchmark.bench_queries import bench_neo4j

COUNT_TX = "MATCH (t:Transaction) RETURN COUNT(t) AS n"


def find_subsets(subsets_dir: str) -> list[tuple[int, str]]:
    """Returns [(n_blocks, path), ...] sorted by increasing size,
    reading the number of blocks from the file name subset_NNNNN.jsonl."""
    paths = sorted(glob.glob(os.path.join(subsets_dir, "subset_*.jsonl")))
    result = []
    for p in paths:
        name = os.path.basename(p)
        n = int(name.replace("subset_", "").replace(".jsonl", ""))
        result.append((n, p))
    return result


def wipe_neo4j(container: str, uri: str, user: str, password: str,
               ready_timeout: float = 180.0) -> None:
    """Wipes the database by stopping the container, deleting the store
    files and restarting.

    Why NOT via Cypher (a lesson learned the hard way, and re-learned
    while writing this script). A `MATCH (n) CALL (n) { DETACH DELETE n }
    IN TRANSACTIONS` over millions of nodes:
      - generates hundreds of GB of reads and writes, because every
        deletion is recorded in the transaction log and the store gets
        rewritten;
      - makes Neo4j stop answering on the Bolt socket
        (ServiceUnavailable on the client side) without logging anything;
      - does NOT free the space: Neo4j does not compact the store files
        on the fly, so the next size starts from a bloated store and its
        ingestion time is no longer comparable.
    Deleting the store files with the container stopped does the same in
    seconds and is a more faithful "from scratch".

    Only the `neo4j` database and its transaction logs are removed; the
    `system` database, which holds users and credentials, stays intact.
    """
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError

    subprocess.run(["docker", "stop", container], check=True,
                   capture_output=True, timeout=180)
    # A minimal auxiliary container mounts the same volumes and deletes
    # the files: this avoids requiring root privileges on the host.
    subprocess.run(
        ["docker", "run", "--rm", "--volumes-from", container, "alpine",
         "sh", "-c", "rm -rf /data/databases/neo4j /data/transactions/neo4j"],
        check=True, capture_output=True, timeout=300)
    subprocess.run(["docker", "start", container], check=True,
                   capture_output=True, timeout=180)

    deadline = time.perf_counter() + ready_timeout
    while time.perf_counter() < deadline:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            driver.close()
            return
        except (OSError, Neo4jError, Exception):
            time.sleep(3)
    raise SystemExit(
        f"Neo4j did not come back available within {ready_timeout:.0f}s after the restart")


def measure_disk_bytes(container: str) -> int | None:
    """Size of the Neo4j datastore inside the Docker container.

    Read with `du` inside the container, not from the host: the Docker
    volume directory is not readable without root. Returns None if the
    docker CLI is not reachable: a missing datum is reported as missing,
    never replaced with an estimate.
    """
    try:
        out = subprocess.run(
            ["docker", "exec", container, "du", "-sb", "/data"],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return None
        return int(out.stdout.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None


def count_transactions(uri: str, user: str, password: str) -> int:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            return session.run(COUNT_TX).single()["n"]
    finally:
        driver.close()


def run_one_size(n_blocks: int, jsonl_path: str, prices_path: str, uri: str, user: str,
                 password: str, container: str, repeats: int, with_clustering: bool,
                 query_runs: int, query_timeout: float) -> list[dict]:
    rows = []

    # --- ingestion, repeated: every repetition starts from an empty store ---
    times = []
    for i in range(1, repeats + 1):
        print(f"[{n_blocks} blocks] wiping the store (repetition {i}/{repeats})...",
              file=sys.stderr)
        wipe_neo4j(container, uri, user, password)

        print(f"[{n_blocks} blocks] loading...", file=sys.stderr)
        t0 = time.perf_counter()
        load_neo4j(jsonl_path, uri, user, password, chunk_size=500)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        print(f"[{n_blocks} blocks] ingestion {i}/{repeats}: {elapsed:.1f}s",
              file=sys.stderr)

    ingest_s = statistics.median(times)
    rows.append({"n_blocks": n_blocks, "metric": "ingest_s", "query": "", "value": ingest_s})
    if repeats > 1:
        rows.append({"n_blocks": n_blocks, "metric": "ingest_min_s", "query": "",
                     "value": min(times)})
        rows.append({"n_blocks": n_blocks, "metric": "ingest_stdev_s", "query": "",
                     "value": statistics.stdev(times)})
        print(f"[{n_blocks} blocks] ingestion median: {ingest_s:.1f}s "
              f"(min {min(times):.1f}s, stdev {statistics.stdev(times):.1f}s)",
              file=sys.stderr)

    size_bytes = measure_disk_bytes(container)
    if size_bytes is not None:
        print(f"[{n_blocks} blocks] store size: {size_bytes/1e6:.1f} MB", file=sys.stderr)
        rows.append({"n_blocks": n_blocks, "metric": "disk_bytes", "query": "", "value": size_bytes})

    # Prices: Q8 joins the transactions with PriceSnapshot by time
    # interval. Without them that query matches nothing and its timing
    # measures an empty result, not the join (measured: 0.094 s instead
    # of 0.523 s on 50 blocks, i.e. the cost of returning nothing).
    # 121 rows, loaded after the timer above so they never enter ingest_s.
    print(f"[{n_blocks} blocks] loading prices (for Q8)...", file=sys.stderr)
    load_prices_neo4j(prices_path, uri, user, password)

    # Clustering: Q7 groups addresses by clusterId, which only exists
    # after this step. Timed and recorded as its own metric: on 50
    # blocks it already costs 194 s against 12 s of loading, and at full
    # scale it is the 84 minutes that make Q7 what it is.
    if with_clustering:
        print(f"[{n_blocks} blocks] clustering (for Q7)...", file=sys.stderr)
        t0 = time.perf_counter()
        cluster_neo4j(uri, user, password)
        cluster_s = time.perf_counter() - t0
        print(f"[{n_blocks} blocks] clustering: {cluster_s:.1f}s", file=sys.stderr)
        rows.append({"n_blocks": n_blocks, "metric": "cluster_s", "query": "", "value": cluster_s})

    # Row count to give context to the curves (not just "n blocks"),
    # same metric name as the SQLite side so the two CSVs line up.
    n_tx = count_transactions(uri, user, password)
    rows.append({"n_blocks": n_blocks, "metric": "n_transactions", "query": "", "value": n_tx})

    print(f"[{n_blocks} blocks] query benchmark...", file=sys.stderr)
    query_results = bench_neo4j(uri, user, password, runs=query_runs,
                                has_clustering=with_clustering,
                                per_query_timeout=query_timeout)
    for q, r in query_results.items():
        if "median_s" in r:
            rows.append({"n_blocks": n_blocks, "metric": "query_median_s", "query": q, "value": r["median_s"]})
        elif "timeout_s" in r:
            rows.append({"n_blocks": n_blocks, "metric": "query_timeout", "query": q, "value": r["timeout_s"]})
        elif "error" in r:
            rows.append({"n_blocks": n_blocks, "metric": "query_error", "query": q, "value": r["error"]})

    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Neo4j scalability curves (ingestion+disk+queries)")
    parser.add_argument("--subsets-dir", default="benchmark/subsets")
    parser.add_argument("--out-csv", default="benchmark/results/scaling_neo4j.csv")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    parser.add_argument("--container", default="nodecoin-neo4j",
                        help="Name of the Docker container, needed to wipe the store")
    parser.add_argument("--repeats", type=int, default=1,
                        help="Ingestion repetitions per size. 1 gives the curve, "
                             "3 also gives median and stdev (three times the cost)")
    parser.add_argument("--prices", default="prices_may.jsonl",
                        help="Price history, needed by Q8")
    parser.add_argument("--cluster-sizes", default="",
                        help="Sizes on which to actually run the clustering, "
                             "e.g. 50,200. Empty means every size; 'none' means "
                             "no size. Q7 is measured only where the clustering "
                             "ran: elsewhere it is reported as skipped, never "
                             "estimated.")
    parser.add_argument("--query-runs", type=int, default=3)
    parser.add_argument("--query-timeout", type=float, default=90.0)
    parser.add_argument("--sizes", default="",
                        help="Comma-separated list of sizes to run (e.g. 50,200). "
                             "Empty means every subset found.")
    args = parser.parse_args()

    subsets = find_subsets(args.subsets_dir)
    if args.sizes:
        wanted = {int(s) for s in args.sizes.split(",")}
        subsets = [(n, p) for n, p in subsets if n in wanted]
    if not subsets:
        print(f"No sub-dataset found in {args.subsets_dir} (run make_subsets.py first)", file=sys.stderr)
        sys.exit(1)

    if args.cluster_sizes.strip().lower() == "none":
        cluster_on = set()
    elif args.cluster_sizes.strip():
        cluster_on = {int(x) for x in args.cluster_sizes.split(",")}
    else:
        cluster_on = {n for n, _ in subsets}

    print(f"Sizes to process: {[n for n, _ in subsets]}", file=sys.stderr)
    print(f"Clustering on: {sorted(cluster_on) if cluster_on else 'no size'}",
          file=sys.stderr)
    print(f"WARNING: this DELETES the Neo4j store at every size "
          f"({args.repeats} time(s) per size).", file=sys.stderr)

    # INCREMENTAL writing, same as the SQLite side: the CSV is updated
    # after every size, so an interruption does not lose the
    # measurements already taken (here one size can cost 40 minutes).
    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)
    fieldnames = ["n_blocks", "metric", "query", "value"]
    with open(args.out_csv, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    total_rows = 0
    for n_blocks, jsonl_path in subsets:
        rows = run_one_size(n_blocks, jsonl_path, args.prices, args.uri, args.user,
                            args.password, args.container, args.repeats,
                            n_blocks in cluster_on, args.query_runs, args.query_timeout)
        with open(args.out_csv, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writerows(rows)
        total_rows += len(rows)
        print(f"[{n_blocks} blocks] saved {len(rows)} rows in {args.out_csv}", file=sys.stderr)

    print(f"\nCompleted: {args.out_csv} ({total_rows} rows)", file=sys.stderr)
    print("The Neo4j database now holds the LAST size processed: reload "
          "the demo dataset before presenting.", file=sys.stderr)
