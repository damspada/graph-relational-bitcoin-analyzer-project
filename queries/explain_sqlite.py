"""
Captures EXPLAIN QUERY PLAN for the queries in queries.sql, to understand
not only HOW LONG a query takes (measured in Phase 4) but WHY: which
indexes are used, where a full scan happens instead of a lookup, where
SQLite creates automatic indexes on the fly.
"""
import sqlite3
import sys

QUERIES = {
    "Q2_utxo": """
        SELECT o.txid, o.output_index, o.address, o.value
        FROM tx_outputs o
        WHERE NOT EXISTS (
            SELECT 1 FROM tx_inputs i
            WHERE i.prev_txid = o.txid AND i.prev_output_index = o.output_index
        )
        ORDER BY o.value DESC LIMIT 100
    """,
    "Q3_balance": """
        WITH received AS (
            SELECT address, SUM(value) AS total_received
            FROM tx_outputs WHERE address IS NOT NULL GROUP BY address
        ),
        spent AS (
            SELECT o.address, SUM(o.value) AS total_spent
            FROM tx_outputs o
            JOIN tx_inputs i ON i.prev_txid = o.txid AND i.prev_output_index = o.output_index
            WHERE o.address IS NOT NULL GROUP BY o.address
        )
        SELECT r.address, r.total_received, COALESCE(s.total_spent,0),
               r.total_received - COALESCE(s.total_spent,0)
        FROM received r LEFT JOIN spent s ON r.address = s.address
        WHERE r.total_received - COALESCE(s.total_spent,0) > 0
        ORDER BY 4 DESC LIMIT 100
    """,
    "Q4_fund_tracing": """
        WITH RECURSIVE flow(address, txid, output_index, depth) AS (
            SELECT o.address, o.txid, o.output_index, 0
            FROM tx_outputs o WHERE o.address = 'placeholder'
            UNION ALL
            SELECT o2.address, o2.txid, o2.output_index, flow.depth + 1
            FROM flow
            JOIN tx_inputs i ON i.prev_txid = flow.txid AND i.prev_output_index = flow.output_index
            JOIN tx_outputs o2 ON o2.txid = i.txid
            WHERE flow.depth < 8
        )
        SELECT DISTINCT address, depth FROM flow ORDER BY depth
    """,
    "Q5_hub_detection": """
        SELECT address, COUNT(*) AS n_outputs, SUM(value) AS total_value
        FROM tx_outputs
        WHERE address IS NOT NULL
        GROUP BY address ORDER BY n_outputs DESC LIMIT 20
    """,
}


def explain_all(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    results = {}
    for name, q in QUERIES.items():
        plan = conn.execute("EXPLAIN QUERY PLAN " + q).fetchall()
        results[name] = [row[3] for row in plan]  # "detail" column
    conn.close()
    return results


def print_report(results: dict) -> None:
    for name, steps in results.items():
        print(f"=== {name} ===")
        for step in steps:
            flag = ""
            if "SCAN" in step and "INDEX" not in step:
                flag = "  <-- full scan, no index used"
            elif "AUTOMATIC" in step:
                flag = "  <-- index created on the fly (no explicit index covers this column)"
            elif "TEMP B-TREE" in step:
                flag = "  <-- temporary structure for ORDER BY/DISTINCT/GROUP BY"
            print(f"  {step}{flag}")
        print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EXPLAIN QUERY PLAN for the Phase 3 queries (SQLite)")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    print_report(explain_all(args.db))
