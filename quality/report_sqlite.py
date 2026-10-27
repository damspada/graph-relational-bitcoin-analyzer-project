"""
Data quality report for the already populated SQLite database.

It does not modify the data: it only reads, and produces a quantitative
summary of the characteristics of the dataset (coverage, expected
anomalies, unexpected anomalies). It serves to establish confidence in the
dataset before using it for the benchmark (Phase 4): a performance
comparison between SQL and Neo4j only makes sense if both databases really
contain the same data, in the same quantity and with the same properties;
this script verifies exactly that, and the very same set of checks is also
run on the Neo4j side (quality/report_neo4j.py) for direct comparison.

The "expected anomalies" (outputs without an address, coinbase inputs,
inputs outside the time window) are normal behavior, not bugs: here their
real incidence on the dataset is measured, instead of merely asserting that
they exist.
"""
import sqlite3


COINBASE_PREV_INDEX = 4294967295


def report(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    r = {}

    r["n_blocks"] = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    r["n_transactions"] = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    r["n_addresses"] = conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    r["n_outputs"] = conn.execute("SELECT COUNT(*) FROM tx_outputs").fetchone()[0]
    r["n_inputs"] = conn.execute("SELECT COUNT(*) FROM tx_inputs").fetchone()[0]

    # Time coverage
    row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM blocks").fetchone()
    r["timestamp_min"], r["timestamp_max"] = row

    # Outputs without a resolvable address (OP_RETURN, non-standard
    # scripts): expected behavior.
    r["n_outputs_null_address"] = conn.execute(
        "SELECT COUNT(*) FROM tx_outputs WHERE address IS NULL"
    ).fetchone()[0]
    r["pct_outputs_null_address"] = (
        100 * r["n_outputs_null_address"] / r["n_outputs"] if r["n_outputs"] else 0
    )

    # Coinbase inputs: exactly one per block is expected
    r["n_inputs_coinbase"] = conn.execute(
        f"SELECT COUNT(*) FROM tx_inputs WHERE prev_output_index = {COINBASE_PREV_INDEX}"
    ).fetchone()[0]
    r["coinbase_matches_block_count"] = (r["n_inputs_coinbase"] == r["n_blocks"])

    # "Orphan" inputs: not coinbase, and their prev_txid/prev_output_index
    # does not resolve to any output present in the dataset. Expected for
    # transactions that spend outputs preceding the downloaded time
    # window: NOT a loading error, but it must be quantified to know how
    # widespread it is.
    r["n_inputs_orphan"] = conn.execute(f"""
        SELECT COUNT(*) FROM tx_inputs i
        WHERE i.prev_output_index != {COINBASE_PREV_INDEX}
        AND NOT EXISTS (
            SELECT 1 FROM tx_outputs o
            WHERE o.txid = i.prev_txid AND o.output_index = i.prev_output_index
        )
    """).fetchone()[0]
    r["pct_inputs_orphan"] = 100 * r["n_inputs_orphan"] / r["n_inputs"] if r["n_inputs"] else 0

    # UNEXPECTED anomalies: if these numbers are > 0, they indicate a real
    # problem in the parsing/loading, not a known limit of the domain.
    r["n_outputs_negative_value"] = conn.execute(
        "SELECT COUNT(*) FROM tx_outputs WHERE value < 0"
    ).fetchone()[0]
    r["n_transactions_orphan_block"] = conn.execute("""
        SELECT COUNT(*) FROM transactions t
        WHERE NOT EXISTS (SELECT 1 FROM blocks b WHERE b.hash = t.block_hash)
    """).fetchone()[0]
    r["n_duplicate_txids"] = conn.execute("""
        SELECT COUNT(*) FROM (SELECT txid FROM transactions GROUP BY txid HAVING COUNT(*) > 1)
    """).fetchone()[0]

    # Transaction size distribution (percentiles approximated via sorted sampling)
    sizes = [row[0] for row in conn.execute("SELECT size FROM transactions ORDER BY size").fetchall()]
    if sizes:
        r["tx_size_min"] = sizes[0]
        r["tx_size_max"] = sizes[-1]
        r["tx_size_median"] = sizes[len(sizes) // 2]

    conn.close()
    return r


def print_report(r: dict) -> None:
    print("=== Data Quality Report: SQLite ===")
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

    parser = argparse.ArgumentParser(description="Data quality report on a SQLite database")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    print_report(report(args.db))
