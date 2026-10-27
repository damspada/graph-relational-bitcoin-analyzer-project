"""
Data quality report for the already populated Neo4j database. Exactly the
same metrics as quality/report_sqlite.py, so that the two reports are
directly comparable number by number: if the two databases were loaded
from the same JSONL, every value must match exactly. A discrepancy
indicates a bug in the loader (SQL or Neo4j), not a modelling difference:
see quality/compare.py for the automatic comparison.
"""

from neo4j import GraphDatabase

COINBASE_PREV_INDEX = 4294967295


def report(uri: str, user: str, password: str) -> dict:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    r = {}

    with driver.session() as session:
        r["n_blocks"] = session.run("MATCH (b:Block) RETURN COUNT(b) AS n").single()["n"]
        r["n_transactions"] = session.run("MATCH (t:Transaction) RETURN COUNT(t) AS n").single()["n"]
        r["n_addresses"] = session.run("MATCH (a:Address) RETURN COUNT(a) AS n").single()["n"]
        r["n_outputs"] = session.run("MATCH (o:Output) RETURN COUNT(o) AS n").single()["n"]
        r["n_inputs"] = session.run("MATCH (i:Input) RETURN COUNT(i) AS n").single()["n"]

        row = session.run("MATCH (b:Block) RETURN MIN(b.timestamp) AS mn, MAX(b.timestamp) AS mx").single()
        r["timestamp_min"], r["timestamp_max"] = row["mn"], row["mx"]

        # Outputs without a LOCKED_TO relationship (address not
        # resolvable): same meaning as address IS NULL on the SQL side.
        r["n_outputs_null_address"] = session.run("""
            MATCH (o:Output) WHERE NOT (o)-[:LOCKED_TO]->(:Address)
            RETURN COUNT(o) AS n
        """).single()["n"]
        r["pct_outputs_null_address"] = (
            100 * r["n_outputs_null_address"] / r["n_outputs"] if r["n_outputs"] else 0
        )

        # Coinbase inputs: prevIndex = 4294967295 (property saved by the
        # loader, see loader/load_neo4j.py), same criterion used on the SQL side.
        r["n_inputs_coinbase"] = session.run(f"""
            MATCH (i:Input) WHERE i.prevIndex = {COINBASE_PREV_INDEX}
            RETURN COUNT(i) AS n
        """).single()["n"]
        r["coinbase_matches_block_count"] = (r["n_inputs_coinbase"] == r["n_blocks"])

        # "Orphan" inputs: not coinbase, and no SPENDS relationship created
        # (the loader did not find the referenced Output in the dataset).
        r["n_inputs_orphan"] = session.run(f"""
            MATCH (i:Input)
            WHERE i.prevIndex <> {COINBASE_PREV_INDEX} AND NOT (i)-[:SPENDS]->(:Output)
            RETURN COUNT(i) AS n
        """).single()["n"]
        r["pct_inputs_orphan"] = 100 * r["n_inputs_orphan"] / r["n_inputs"] if r["n_inputs"] else 0

        # UNEXPECTED anomalies
        r["n_outputs_negative_value"] = session.run(
            "MATCH (o:Output) WHERE o.value < 0 RETURN COUNT(o) AS n"
        ).single()["n"]
        r["n_transactions_orphan_block"] = session.run("""
            MATCH (t:Transaction) WHERE NOT (:Block)-[:CONTAINS]->(t)
            RETURN COUNT(t) AS n
        """).single()["n"]

        # Always 0 by construction: tx_txid_unique prevents two
        # Transaction nodes with the same txid. This is a sanity check on
        # the constraint itself (that it is really active), not a check
        # that we expect to fail: symmetric to the check on the SQL side,
        # where txid is the PRIMARY KEY of transactions.
        r["n_duplicate_txids"] = session.run("""
            MATCH (t:Transaction)
            WITH t.txid AS txid, COUNT(*) AS n
            WHERE n > 1
            RETURN COUNT(*) AS n
        """).single()["n"]

        sizes_result = session.run("MATCH (t:Transaction) RETURN t.size AS size ORDER BY size").data()
        sizes = [row["size"] for row in sizes_result]
        if sizes:
            r["tx_size_min"] = sizes[0]
            r["tx_size_max"] = sizes[-1]
            r["tx_size_median"] = sizes[len(sizes) // 2]

    driver.close()
    return r


def print_report(r: dict) -> None:
    print("=== Data Quality Report: Neo4j ===")
    print(f"Blocks: {r['n_blocks']}")
    print(f"Transactions: {r['n_transactions']}")
    print(f"Distinct addresses: {r['n_addresses']}")
    print(f"Outputs: {r['n_outputs']}")
    print(f"Inputs: {r['n_inputs']}")
    print(f"Time coverage: {r['timestamp_min']} - {r['timestamp_max']}")
    print()
    print("--- Expected anomalies (documented as normal behavior) ---")
    print(f"Outputs without a resolvable address: {r['n_outputs_null_address']} ({r['pct_outputs_null_address']:.1f}%)")
    print(f"Coinbase inputs: {r['n_inputs_coinbase']} (expected = no. of blocks: {r['coinbase_matches_block_count']})")
    print(f"'Orphan' inputs (outside the time window): {r['n_inputs_orphan']} ({r['pct_inputs_orphan']:.1f}%)")
    print()
    print("--- UNEXPECTED anomalies (0 expected; > 0 indicates a real problem) ---")
    print(f"Outputs with a negative value: {r['n_outputs_negative_value']}")
    print(f"Transactions with a missing block: {r['n_transactions_orphan_block']}")
    print(f"Duplicate txids: {r['n_duplicate_txids']}")
    print()
    if "tx_size_min" in r:
        print(f"Transaction size (bytes): min={r['tx_size_min']} median={r['tx_size_median']} max={r['tx_size_max']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Data quality report on a Neo4j database")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    args = parser.parse_args()

    print_report(report(args.uri, args.user, args.password))
