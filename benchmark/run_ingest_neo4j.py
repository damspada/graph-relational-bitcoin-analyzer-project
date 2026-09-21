"""
Repeated measurement of the INGESTION cost on the Neo4j side (load time
and disk space), with the same methodology already applied to the queries:
several repetitions instead of a single measurement.

Why this script exists
----------------------
The SQLite scalability curves (benchmark/run_scaling_sqlite.py)
rebuild the database from scratch and time every size. On the Neo4j side, the
ingestion cost had instead been collected as a SINGLE measurement during
development, a number that was not repeated, without warm-up, not comparable
with the same rigour. Presenting "Neo4j is ~4x slower to load"
on the basis of a single timing applies two different standards of rigour
depending on which number is convenient.

This script removes that asymmetry: it performs N complete loads from
scratch (default 3), each preceded by the total wipe of the
database, and reports the mean, median and deviation of the times, as well as the
resulting size on disk.

Cost: every repetition reloads the whole dataset (~40 minutes at 977
blocks), so 3 repetitions take ~2 hours. Wiping between the
repetitions is instead instantaneous (see wipe_neo4j). It is meant to be
launched once, not during iterative development.

Note on the size on disk: it is read from the filesystem of the Docker
container (`docker exec du -sb /data`), not from the host, because the Neo4j
volume lives inside the container. If the container is not reachable via
the docker CLI, the size is reported as None and the other measurements
remain valid.
"""
import statistics
import subprocess
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "loader")


def wipe_neo4j(container: str, uri: str, user: str, password: str,
               ready_timeout: float = 180.0) -> None:
    """Wipes the database by stopping the container, deleting the store
    files and restarting.

    Why NOT via Cypher (a lesson learned the hard way). The first
    version of this function used
    `MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS`.
    On 4.3 million transactions (~14 million nodes and several tens of
    millions of relationships) that operation:
      - generated ~278 GB of reads and ~314 GB of writes on disk,
        because every deletion has to be recorded in the transaction log and
        the store gets rewritten;
      - made Neo4j stop responding on the Bolt socket
        (ServiceUnavailable on the client side) without logging anything;
      - **did not even free up the space**: after the DELETE the store
        still took up 11 GB, because Neo4j does not compact the files on the fly.
    Deleting the store files with the container stopped does the same thing in
    a few seconds (11 GB -> 1.3 MB) and is also a more faithful "from scratch":
    no leftovers of a bloated store that would skew the next measurement.

    Only the `neo4j` database (the data one) and its transaction logs are
    removed; the `system` system database, which contains users
    and credentials, stays intact: deleting it would require
    reconfiguring authentication at every repetition.
    """
    import time as _time

    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError

    subprocess.run(["docker", "stop", container], check=True,
                   capture_output=True, timeout=180)
    # A minimal auxiliary container mounts the same volumes and deletes
    # the files: this avoids requiring root privileges on the host to access
    # the Docker volume directory.
    subprocess.run(
        ["docker", "run", "--rm", "--volumes-from", container, "alpine",
         "sh", "-c", "rm -rf /data/databases/neo4j /data/transactions/neo4j"],
        check=True, capture_output=True, timeout=300)
    subprocess.run(["docker", "start", container], check=True,
                   capture_output=True, timeout=180)

    deadline = _time.perf_counter() + ready_timeout
    while _time.perf_counter() < deadline:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            driver.close()
            return
        except (OSError, Neo4jError, Exception):
            _time.sleep(3)
    raise SystemExit(
        f"Neo4j did not come back available within {ready_timeout:.0f}s after the restart")


def measure_disk_bytes(container: str) -> int | None:
    """Size of the Neo4j datastore inside the Docker container."""
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


def run(jsonl_path: str, uri: str, user: str, password: str,
        repeats: int, container: str, chunk_size: int) -> dict:
    from load_neo4j import load as load_neo4j

    times = []
    sizes = []

    for i in range(1, repeats + 1):
        print(f"\n=== Repetition {i}/{repeats} ===", file=sys.stderr)

        print("  wiping the database (stop container + rm store)...", file=sys.stderr)
        wipe_neo4j(container, uri, user, password)

        # The schema (constraints + indexes) is NOT re-applied here: it is created by
        # load_neo4j.load() through init_schema(), so its cost
        # falls INSIDE the timed interval: exactly as on the
        # SQLite side, where load_sqlite() creates the tables and the indexes inside
        # the same measurement. Applying it outside the timer would have made
        # the two numbers not comparable.
        print(f"  loading {jsonl_path}...", file=sys.stderr)
        t0 = time.perf_counter()
        load_neo4j(jsonl_path, uri, user, password, chunk_size=chunk_size)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        print(f"  ingestion: {elapsed:.1f}s ({elapsed/60:.1f} min)", file=sys.stderr)

        size = measure_disk_bytes(container)
        if size is not None:
            sizes.append(size)
            print(f"  size on disk: {size/1e9:.2f} GB", file=sys.stderr)
        else:
            print("  size on disk: not measurable (docker not reachable)", file=sys.stderr)

    result = {
        "repeats": repeats,
        "times_s": times,
        "mean_s": statistics.mean(times),
        "median_s": statistics.median(times),
        "min_s": min(times),
        "max_s": max(times),
        "stdev_s": statistics.stdev(times) if len(times) > 1 else 0.0,
        "disk_bytes": sizes,
        "disk_bytes_mean": statistics.mean(sizes) if sizes else None,
    }
    return result


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Repeated measurement of the Neo4j ingestion (time + disk)")
    parser.add_argument("jsonl", help="Path to the JSONL dataset to load")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Number of complete loads from scratch (default 3)")
    parser.add_argument("--container", default="nodecoin-neo4j",
                        help="Name of the Docker container, to measure the space on disk")
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--out", default="benchmark/results/ingest_neo4j.json")
    args = parser.parse_args()

    res = run(args.jsonl, args.uri, args.user, args.password,
              args.repeats, args.container, args.chunk_size)

    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)

    print("\n=== Neo4j ingestion: summary ===")
    print(f"  repetitions:      {res['repeats']}")
    print(f"  times (s):        {', '.join(f'{t:.1f}' for t in res['times_s'])}")
    print(f"  mean:             {res['mean_s']:.1f}s ({res['mean_s']/60:.1f} min)")
    print(f"  median:           {res['median_s']:.1f}s ({res['median_s']/60:.1f} min)")
    print(f"  min / max:        {res['min_s']:.1f}s / {res['max_s']:.1f}s")
    print(f"  std. deviation:   {res['stdev_s']:.1f}s")
    if res["disk_bytes_mean"]:
        print(f"  disk (mean):      {res['disk_bytes_mean']/1e9:.2f} GB")
    print(f"\nSaved to {args.out}")
