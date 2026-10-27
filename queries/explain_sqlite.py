"""
Captures EXPLAIN QUERY PLAN for the queries in queries.sql, to understand
not only HOW LONG a query takes (measured in Phase 4) but WHY: which
indexes are used, where a full scan happens instead of a lookup, where
SQLite creates automatic indexes on the fly.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from benchmark.query_loader import load_sql_queries  # noqa: E402

# The queries are read from queries.sql, the same file the benchmark runs:
# a copy kept here would drift away from it at the first change.
QUERIES = load_sql_queries(str(ROOT / "queries" / "queries.sql"))
# Q4 has a parameter; EXPLAIN only needs a value of the right type
PARAMS = {"start_address": "placeholder"}


def explain_all(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    results = {}
    for name, q in QUERIES.items():
        plan = conn.execute("EXPLAIN QUERY PLAN " + q,
                            PARAMS if ":start_address" in q else {}).fetchall()
        results[name] = [row[3] for row in plan]  # "detail" column
    conn.close()
    return results


def print_report(results: dict) -> None:
    for name, steps in results.items():
        print(f"=== {name} ===")
        for step in steps:
            flag = ""
            if step.startswith("SCAN") and step.split()[1] in ("flow", "mc"):
                flag = "  <-- the recursive CTE itself: one row of its queue per step"
            elif "SCAN" in step and "INDEX" not in step:
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
