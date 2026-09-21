"""
Compares the data quality reports of SQLite and Neo4j (report_sqlite.py,
report_neo4j.py) to verify that the two databases, loaded from the same
JSONL, really contain the same data in the same quantity.

This is the post-loading consistency check between the two models: if a
number does not match, it indicates a bug in the loader (SQL or Neo4j),
not a legitimate modelling difference. It must be run before trusting the
results of the comparative benchmark (Phase 4): a performance comparison
between two different datasets (even if they differ only because of a
loading bug) is not a valid comparison.
"""
import sys

from report_sqlite import report as sqlite_report
from report_neo4j import report as neo4j_report

# Keys present in both reports with the same meaning, to be compared
# number by number. Some keys (e.g. tx_size_*, timestamp_*) are included:
# the size distribution must be identical if the data is really the same.
COMPARABLE_KEYS = [
    "n_blocks",
    "n_transactions",
    "n_addresses",
    "n_outputs",
    "n_inputs",
    "timestamp_min",
    "timestamp_max",
    "n_outputs_null_address",
    "n_inputs_coinbase",
    "n_inputs_orphan",
    "n_outputs_negative_value",
    "n_transactions_orphan_block",
    "n_duplicate_txids",
    "tx_size_min",
    "tx_size_max",
    "tx_size_median",
]


def compare(sqlite_db: str, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> bool:
    sql_r = sqlite_report(sqlite_db)
    neo_r = neo4j_report(neo4j_uri, neo4j_user, neo4j_password)

    all_match = True
    print(f"{'Metric':<35} {'SQLite':>15} {'Neo4j':>15} {'Match':>8}")
    print("-" * 76)
    for key in COMPARABLE_KEYS:
        sql_val = sql_r.get(key, "N/A")
        neo_val = neo_r.get(key, "N/A")
        match = sql_val == neo_val
        all_match = all_match and match
        marker = "OK" if match else "MISMATCH"
        print(f"{key:<35} {str(sql_val):>15} {str(neo_val):>15} {marker:>8}")

    print()
    if all_match:
        print("All values match: the two databases reflect the same dataset.")
    else:
        print("WARNING: one or more values do not match. Check the loader "
              "before using these databases for the comparative benchmark.")

    return all_match


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compares data quality reports SQLite vs Neo4j")
    parser.add_argument("--db", required=True, help="Path of the SQLite database")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    args = parser.parse_args()

    ok = compare(args.db, args.uri, args.user, args.password)
    sys.exit(0 if ok else 1)
