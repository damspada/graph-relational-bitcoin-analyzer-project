"""
Tests for the SQLite loader (loader/load_sqlite.py) on minimal synthetic
data, not on a real downloaded dataset: they check the loader's expected
properties without depending on the network or on download time.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "loader"))
from load_sqlite import load  # noqa: E402


def write_sample_jsonl(path: str) -> None:
    """A synthetic block with: one coinbase, one OP_RETURN output (empty
    address), and one address receiving two different outputs (to check
    deduplication in addresses)."""
    block = {
        "hash": "a" * 64,
        "prev_hash": "0" * 64,
        "timestamp": 1778693452,
        "transactions": [
            {
                "txid": "b" * 64,
                "size": 200,
                "inputs": [{"prev_txid": "0" * 64, "prev_index": 4294967295}],
                "outputs": [
                    {"index": 0, "address": "addr1", "value": 5000000000},
                    {"index": 1, "address": "", "value": 0},  # OP_RETURN
                ],
            },
            {
                "txid": "c" * 64,
                "size": 150,
                "inputs": [{"prev_txid": "b" * 64, "prev_index": 0}],
                "outputs": [
                    {"index": 0, "address": "addr1", "value": 1000},  # same addr1
                    {"index": 1, "address": "addr2", "value": 2000},
                ],
            },
        ],
    }
    with open(path, "w") as f:
        f.write(json.dumps(block) + "\n")


class TestLoadSqlite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl_path = os.path.join(self.tmpdir, "sample.jsonl")
        self.db_path = os.path.join(self.tmpdir, "test.sqlite")
        write_sample_jsonl(self.jsonl_path)
        load(self.jsonl_path, self.db_path, chunk_size=10)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()

    def test_all_blocks_and_transactions_loaded(self):
        n_blocks = self.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        n_tx = self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n_blocks, 1)
        self.assertEqual(n_tx, 2)

    def test_empty_address_mapped_to_null_not_empty_string(self):
        """The OP_RETURN output must have a NULL address, not an empty
        string, otherwise a GROUP BY address would wrongly group all the
        OP_RETURNs together."""
        row = self.conn.execute(
            "SELECT address FROM tx_outputs WHERE txid = ? AND output_index = 1",
            ("b" * 64,),
        ).fetchone()
        self.assertIsNone(row[0])

    def test_address_deduplicated_across_outputs(self):
        """addr1 receives two outputs in two different transactions: it must
        appear only once in addresses."""
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM addresses WHERE address = 'addr1'"
        ).fetchone()
        self.assertEqual(rows[0], 1)

        # but it must still be referenced by both outputs
        outputs = self.conn.execute(
            "SELECT COUNT(*) FROM tx_outputs WHERE address = 'addr1'"
        ).fetchone()
        self.assertEqual(outputs[0], 2)

    def test_coinbase_input_present_without_fk_violation(self):
        """The coinbase input (all-zero prev_txid) must not cause referential
        integrity errors: by design tx_inputs has no declared FK towards
        tx_outputs."""
        row = self.conn.execute(
            "SELECT prev_txid, prev_output_index FROM tx_inputs WHERE txid = ?",
            ("b" * 64,),
        ).fetchone()
        self.assertEqual(row[0], "0" * 64)
        self.assertEqual(row[1], 4294967295)

    def test_secondary_indexes_created(self):
        indexes = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected = {
            "idx_blocks_timestamp",
            "idx_transactions_block_hash",
            "idx_tx_outputs_address",
            "idx_tx_inputs_prev",
        }
        self.assertEqual(indexes, expected)

    def test_foreign_key_integrity_holds(self):
        self.conn.execute("PRAGMA foreign_keys = ON")
        violations = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
