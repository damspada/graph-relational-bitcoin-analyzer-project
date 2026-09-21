"""
Loads the JSONL produced by go-parser into a SQLite database, following the
schema in schema/schema.sql. Batch insertion (executemany) for each chunk of
blocks read by common.read_jsonl_chunks, not row by row: this reduces the
round-trips to the cursor, measurable in Phase 4 - benchmark.
"""
import sqlite3
import sys
import time
from pathlib import Path

from common import read_jsonl_chunks, normalize_address

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "schema.sql"

# Non-PK indexes declared in schema/schema.sql: they are created AFTER the
# bulk-load, not by the schema.sql script executed at the start of loading.
# Rationale: keeping a secondary index updated at every single INSERT during
# a load of millions of rows is much more expensive than building it once on
# the complete dataset (standard bulk-loading pattern). The definitions stay
# in schema.sql as a reference for the logical schema; here they are
# extracted and executed only at the end of the load.
SECONDARY_INDEXES = [
    "CREATE INDEX idx_blocks_prev_hash ON blocks(prev_hash)",
    "CREATE INDEX idx_blocks_timestamp ON blocks(timestamp)",
    "CREATE INDEX idx_transactions_block_hash ON transactions(block_hash)",
    "CREATE INDEX idx_tx_outputs_address ON tx_outputs(address)",
    "CREATE INDEX idx_tx_inputs_prev ON tx_inputs(prev_txid, prev_output_index)",
]


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # PRAGMAs for bulk-load: WAL instead of rollback-journal, reduced sync,
    # foreign_keys OFF during loading. The per-row FK check is the real
    # bottleneck on millions of inserts (every row requires a lookup on the
    # index of the referenced table); it is re-enabled and validated in one
    # go at the end of the load (see load()).
    # These PRAGMAs are engine tuning, they have nothing to do with the
    # modelling decisions already motivated in schema/schema.sql.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -262144")  # 256 MB of page cache
    conn.execute("PRAGMA foreign_keys = OFF")
    with open(SCHEMA_PATH) as f:
        statements = f.read().split(";")
    for stmt in statements:
        # Removes the comment lines to isolate the actual SQL statement
        # (a block can start with "-- ..." followed by the real CREATE on
        # the next line).
        code_lines = [l for l in stmt.splitlines() if not l.strip().startswith("--")]
        s = "\n".join(code_lines).strip()
        if not s or s.upper().startswith("CREATE INDEX"):
            continue  # secondary indexes created at the end of the load, see SECONDARY_INDEXES
        conn.execute(s)
    return conn


def load_chunk(conn: sqlite3.Connection, blocks: list[dict]) -> None:
    """Inserts a chunk of blocks in a single transaction, with executemany
    for each table (batch, not row by row)."""
    block_rows = []
    tx_rows = []
    address_rows = set()  # set: avoids re-inserting the same address
    output_rows = []
    input_rows = []

    for block in blocks:
        block_rows.append((block["hash"], block["prev_hash"], block["timestamp"]))

        for tx in block["transactions"]:
            tx_rows.append((tx["txid"], block["hash"], tx["size"]))

            for out in tx["outputs"]:
                addr = normalize_address(out["address"])
                if addr is not None:
                    address_rows.add((addr,))
                output_rows.append((tx["txid"], out["index"], addr, out["value"]))

            for i, inp in enumerate(tx["inputs"]):
                input_rows.append((tx["txid"], i, inp["prev_txid"], inp["prev_index"]))

    cur = conn.cursor()
    # Insertion order constrained by the FKs declared in the schema:
    # blocks -> transactions -> addresses -> tx_outputs -> tx_inputs.
    cur.executemany("INSERT OR IGNORE INTO blocks VALUES (?,?,?)", block_rows)
    cur.executemany("INSERT OR IGNORE INTO transactions VALUES (?,?,?)", tx_rows)
    cur.executemany("INSERT OR IGNORE INTO addresses VALUES (?)", address_rows)
    cur.executemany("INSERT OR IGNORE INTO tx_outputs VALUES (?,?,?,?)", output_rows)
    cur.executemany("INSERT OR IGNORE INTO tx_inputs VALUES (?,?,?,?)", input_rows)
    conn.commit()


def load(jsonl_path: str, db_path: str, chunk_size: int = 500) -> None:
    conn = init_db(db_path)

    n_blocks = 0
    t0 = time.perf_counter()
    for chunk in read_jsonl_chunks(jsonl_path, chunk_size):
        load_chunk(conn, chunk)
        n_blocks += len(chunk)
        print(f"  ...{n_blocks} blocks loaded into SQLite", file=sys.stderr)

    elapsed = time.perf_counter() - t0
    print(f"Insertion complete: {n_blocks} blocks in {elapsed:.1f}s ({n_blocks/elapsed:.1f} blocks/s)", file=sys.stderr)

    t1 = time.perf_counter()
    for stmt in SECONDARY_INDEXES:
        conn.execute(stmt)
    conn.commit()
    print(f"Secondary indexes created in {time.perf_counter()-t1:.1f}s", file=sys.stderr)

    # Re-enables the FK checks and validates in one go. The only expected
    # violations are the documented ones (coinbase inputs, inputs outside
    # the downloaded time window) and they do not raise errors because
    # tx_inputs has no FK declared towards tx_outputs (see
    # schema/schema.sql). This check covers blocks/transactions/
    # tx_outputs, where the FKs are actually declared.
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        print(f"WARNING: {len(violations)} referential integrity violations found", file=sys.stderr)
    else:
        print("Referential integrity validation: OK", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Load JSONL into SQLite")
    parser.add_argument("jsonl", help="Path to the .jsonl file produced by go-parser")
    parser.add_argument("--db", default="nodecoin.sqlite", help="Path of the output SQLite database")
    parser.add_argument("--chunk-size", type=int, default=500, help="Blocks per insertion batch")
    args = parser.parse_args()

    load(args.jsonl, args.db, args.chunk_size)
