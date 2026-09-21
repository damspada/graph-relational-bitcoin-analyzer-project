"""
Export/import of the address -> clusterId map computed by
clustering/cluster_neo4j.py (gds.wcc).

Why it is needed: computing the clustering at full scale costs ~84 minutes,
almost entirely spent building the COOWNED_WITH edges. Any operation that
reloads the database from scratch: for example the repeated measurement of
the ingestion (benchmark/run_ingest_neo4j.py): destroys that result.

The clustering, however, is a DERIVED datum and deterministic with respect to the
dataset: if the reloaded dataset is the same, the
address -> clusterId map stays valid. Exporting and re-importing it avoids
recomputing it, without altering the meaning of the data (it is exactly the
"materialized view" pattern).

WARNING: re-importing a map computed on a DIFFERENT dataset
would produce inconsistent clusters. The export file therefore records the
number of addresses and of clusters, and the import checks that the number of
addresses in the database matches before writing.
"""
import gzip
import json
import sys


def export_clusters(uri: str, user: str, password: str, out_path: str) -> dict:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()

    pairs = []
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Address)
            WHERE a.clusterId IS NOT NULL
            RETURN a.address AS address, a.clusterId AS clusterId
        """)
        for rec in result:
            pairs.append((rec["address"], rec["clusterId"]))

        n_addresses_total = session.run(
            "MATCH (a:Address) RETURN COUNT(a) AS n").single()["n"]
    driver.close()

    n_clusters = len({c for _, c in pairs})
    meta = {
        "n_with_cluster": len(pairs),
        "n_addresses_total": n_addresses_total,
        "n_clusters": n_clusters,
    }

    with gzip.open(out_path, "wt") as f:
        f.write(json.dumps(meta) + "\n")
        for addr, cid in pairs:
            f.write(json.dumps([addr, cid]) + "\n")

    print(f"Exported {len(pairs)} addresses in {n_clusters} clusters -> {out_path}",
          file=sys.stderr)
    return meta


def import_clusters(uri: str, user: str, password: str, in_path: str,
                    batch_size: int = 20000, force: bool = False) -> int:
    from neo4j import GraphDatabase

    with gzip.open(in_path, "rt") as f:
        meta = json.loads(f.readline())
        pairs = [json.loads(line) for line in f]

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()

    with driver.session() as session:
        n_now = session.run("MATCH (a:Address) RETURN COUNT(a) AS n").single()["n"]

    if n_now != meta["n_addresses_total"] and not force:
        driver.close()
        raise SystemExit(
            f"REFUSING the import: the database has {n_now} addresses, the export "
            f"recorded {meta['n_addresses_total']}. The dataset does not match, "
            f"the cluster map is not valid for this data. "
            f"Use --force only if you are sure of the contrary."
        )

    written = 0
    with driver.session() as session:
        for i in range(0, len(pairs), batch_size):
            batch = [{"address": a, "clusterId": c} for a, c in pairs[i:i + batch_size]]
            session.run("""
                UNWIND $rows AS row
                MATCH (a:Address {address: row.address})
                SET a.clusterId = row.clusterId
            """, rows=batch).consume()
            written += len(batch)
            print(f"  ...{written}/{len(pairs)} addresses updated", file=sys.stderr)
    driver.close()

    print(f"Re-imported {written} addresses ({meta['n_clusters']} clusters)", file=sys.stderr)
    return written


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backup/restore of the address->clusterId map")
    parser.add_argument("action", choices=["export", "import"])
    parser.add_argument("--file", default="benchmark/results/clusters_backup.jsonl.gz")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    parser.add_argument("--force", action="store_true",
                        help="Import: proceed even if the address count does not match")
    args = parser.parse_args()

    if args.action == "export":
        export_clusters(args.uri, args.user, args.password, args.file)
    else:
        import_clusters(args.uri, args.user, args.password, args.file, force=args.force)
