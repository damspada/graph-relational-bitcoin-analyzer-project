"""
Loads the JSONL produced by go-parser into Neo4j, following the schema in
schema/schema.cypher. Batch insertion (UNWIND) for each chunk of blocks
read by common.read_jsonl_chunks, not one MERGE at a time: this reduces
the round-trips to the database.

Model: (:Transaction)-[:HAS_OUTPUT]->(:Output)-[:LOCKED_TO]->(:Address),
(:Transaction)-[:HAS_INPUT]->(:Input)-[:SPENDS]->(:Output). Output and Input
are nodes of their own, identified by the composite natural key (txid, index)
exposed as the property outputRef/inputRef = "txid:index" (Neo4j has no
native uniqueness constraints over multiple properties like a composite SQL
PK). See schema/schema.cypher for the rationale of this granularity.
"""
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase
from common import read_jsonl_chunks, normalize_address

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "schema.cypher"

CYPHER_BLOCKS = """
UNWIND $rows AS row
MERGE (b:Block {hash: row.hash})
SET b.prev_hash = row.prev_hash, b.timestamp = row.timestamp
"""

CYPHER_PREV_BLOCK = """
UNWIND $rows AS row
MATCH (b:Block {hash: row.hash})
MATCH (p:Block {hash: row.prev_hash})
MERGE (p)-[:PREV_BLOCK]->(b)
"""

CYPHER_TRANSACTIONS = """
UNWIND $rows AS row
MATCH (b:Block {hash: row.block_hash})
MERGE (t:Transaction {txid: row.txid})
SET t.size = row.size
MERGE (b)-[:CONTAINS]->(t)
"""

# One Output node for each output of the transaction (even without a
# resolvable address: the Output exists as an entity anyway, only the
# LOCKED_TO edge is not created in that case, consistently with the NULL
# treatment on the SQL side).
CYPHER_OUTPUTS = """
UNWIND $rows AS row
MATCH (t:Transaction {txid: row.txid})
MERGE (o:Output {outputRef: row.txid + ':' + toString(row.output_index)})
SET o.value = row.value, o.outputIndex = row.output_index
MERGE (t)-[:HAS_OUTPUT]->(o)
WITH o, row
FOREACH (_ IN CASE WHEN row.address IS NOT NULL THEN [1] ELSE [] END |
    MERGE (a:Address {address: row.address})
    MERGE (o)-[:LOCKED_TO]->(a)
)
"""

# One Input node for each input of the transaction (always created, even for
# coinbase: it faithfully mirrors the raw data). The SPENDS edge towards the
# referenced Output is created only if that Output is already present in the
# dataset: see schema/schema.cypher.
#
# prevTxid/prevIndex are saved as properties of the Input node even when the
# SPENDS edge is not created: without these properties it would not be
# possible to distinguish at query time a coinbase input (prevIndex =
# 4294967295) from an input that references an output outside the downloaded
# time window (same structural symptom, no SPENDS edge, but a different
# cause). It is the Neo4j equivalent of the prev_txid/prev_output_index
# columns always present in tx_inputs on the SQL side.
CYPHER_INPUTS = """
UNWIND $rows AS row
MATCH (t:Transaction {txid: row.txid})
MERGE (i:Input {inputRef: row.txid + ':' + toString(row.input_index)})
SET i.inputIndex = row.input_index,
    i.prevTxid = row.prev_txid,
    i.prevIndex = row.prev_index
MERGE (t)-[:HAS_INPUT]->(i)
WITH i, row
OPTIONAL MATCH (prevOut:Output {outputRef: row.prev_txid + ':' + toString(row.prev_index)})
FOREACH (_ IN CASE WHEN prevOut IS NOT NULL THEN [1] ELSE [] END |
    MERGE (i)-[:SPENDS]->(prevOut)
)
"""


def init_schema(driver) -> None:
    # The comment filtering must happen BEFORE discarding the empty
    # statements: a statement can start with a comment block (e.g.
    # output_ref_unique in schema.cypher, preceded by 7 lines of notes),
    # so filtering on statement.strip().startswith("//") would discard the
    # whole statement instead of only the leading comment. Same bug already
    # found and fixed in loader/load_sqlite.py.
    with open(SCHEMA_PATH) as f:
        raw_statements = f.read().split(";")
    with driver.session() as session:
        for stmt in raw_statements:
            code_lines = [l for l in stmt.splitlines() if not l.strip().startswith("//")]
            clean = "\n".join(code_lines).strip()
            if clean:
                session.run(clean)


# Maximum number of rows per single Neo4j transaction (session.run without
# explicit transaction handling = one auto-commit transaction).
#
# Why it is needed, on top of chunk_size on the blocks (measured, not
# theoretical): chunk_size limits how many Bitcoin BLOCKS go into a batch,
# but blocks do not have a constant size. On the 977-block dataset the
# average is ~10.126 outputs and ~7.351 inputs per block: with
# chunk_size=100 (already reduced from the default 500 to keep memory under
# control) a single `session.run(CYPHER_OUTPUTS, rows=output_rows)` still
# ended up processing about a million rows in one auto-commit transaction,
# because MERGE on Output/Address grows in cost with the size of the graph
# already loaded. The observed symptom was `TransactionCommitFailed` with
# over 5 million commands in a single transaction, followed by
# `OutOfMemoryError: Java heap space`: the real control variable for memory
# is the number of ROWS per transaction, not the number of blocks per chunk.
ROWS_PER_TRANSACTION = 20000


def _run_in_batches(session, cypher: str, rows: list[dict], batch_size: int = ROWS_PER_TRANSACTION) -> None:
    """Runs `cypher` on `rows` splitting it into several auto-commit
    transactions of at most `batch_size` rows, instead of a single
    session.run() on all the rows of the chunk. Each batch stays limited in
    size and independent from the number of outputs/inputs actually
    contained in the Bitcoin blocks of the current chunk (which varies a
    lot, unlike the number of blocks itself)."""
    for i in range(0, len(rows), batch_size):
        session.run(cypher, rows=rows[i:i + batch_size]).consume()


def load_chunk(driver, blocks: list[dict]) -> None:
    block_rows = [{"hash": b["hash"], "prev_hash": b["prev_hash"], "timestamp": b["timestamp"]} for b in blocks]

    tx_rows = []
    output_rows = []
    input_rows = []
    for block in blocks:
        for tx in block["transactions"]:
            tx_rows.append({"txid": tx["txid"], "block_hash": block["hash"], "size": tx["size"]})
            for out in tx["outputs"]:
                # Unlike the previous model, the Output is always created as
                # a node (even without a resolvable address): only the
                # LOCKED_TO edge is conditional.
                output_rows.append({
                    "txid": tx["txid"],
                    "output_index": out["index"],
                    "address": normalize_address(out["address"]),
                    "value": out["value"],
                })
            for i, inp in enumerate(tx["inputs"]):
                input_rows.append({
                    "txid": tx["txid"],
                    "input_index": i,
                    "prev_txid": inp["prev_txid"],
                    "prev_index": inp["prev_index"],
                })

    with driver.session() as session:
        # Blocks/PrevBlock: at most chunk_size rows (one Bitcoin block
        # produces exactly one block_rows row), so they always stay small
        # regardless of chunk_size: no batching needed.
        session.run(CYPHER_BLOCKS, rows=block_rows).consume()
        session.run(CYPHER_PREV_BLOCK, rows=block_rows).consume()
        # Transactions, Output and Input can instead be orders of magnitude
        # more numerous than the blocks that contain them (on the real
        # dataset, on average 4.412 transactions/block, 10.126
        # outputs/block, 7.351 inputs/block, so a 500-block chunk brings
        # tx_rows to ~2.2 MILLION rows, not "a few hundred" as an earlier
        # version of this comment wrongly assumed, causing the same
        # OutOfMemoryError here instead of on Output/Input). Batching by
        # number of ROWS, not of blocks, is essential on all three.
        _run_in_batches(session, CYPHER_TRANSACTIONS, tx_rows)
        _run_in_batches(session, CYPHER_OUTPUTS, output_rows)
        _run_in_batches(session, CYPHER_INPUTS, input_rows)


def load(jsonl_path: str, uri: str, user: str, password: str, chunk_size: int = 500) -> None:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    init_schema(driver)

    n_blocks = 0
    t0 = time.perf_counter()
    for chunk in read_jsonl_chunks(jsonl_path, chunk_size):
        load_chunk(driver, chunk)
        n_blocks += len(chunk)
        print(f"  ...{n_blocks} blocks loaded into Neo4j", file=sys.stderr)

    elapsed = time.perf_counter() - t0
    driver.close()
    print(f"Complete: {n_blocks} blocks in {elapsed:.1f}s ({n_blocks/elapsed:.1f} blocks/s)", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Load JSONL into Neo4j")
    parser.add_argument("jsonl", help="Path to the .jsonl file produced by go-parser")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    parser.add_argument("--chunk-size", type=int, default=500, help="Blocks per insertion batch")
    args = parser.parse_args()

    load(args.jsonl, args.uri, args.user, args.password, args.chunk_size)
