"""
Reading of the JSONL produced by go-parser, in streaming mode and in chunks.

No interpretation of the data in here (no fee computation, no deduction of
coinbase/spent): this module only provides the raw blocks to the loaders
(SQLite/Neo4j) in fixed-size groups, so that the insertion can happen in
batches instead of row by row.
"""
import json
from typing import Iterator, List, Dict, Any


def read_jsonl_chunks(path: str, chunk_size: int = 500) -> Iterator[List[Dict[str, Any]]]:
    """Reads a .jsonl file and produces lists of blocks of length <= chunk_size.

    Streaming: it never holds more than chunk_size blocks in memory at a time,
    consistently with the streaming approach already used by the Go parser.
    """
    chunk = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chunk.append(json.loads(line))
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
    if chunk:
        yield chunk


def normalize_address(address: str) -> str | None:
    """Maps the empty string (output without a resolvable address, e.g.
    OP_RETURN or non-standard script) to None, so the loaders write NULL
    instead of an empty string."""
    return address if address else None
