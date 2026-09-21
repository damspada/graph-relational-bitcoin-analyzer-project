"""
Tests for the Neo4j loader (loader/load_neo4j.py) on minimal synthetic
data, the same dataset as test/test_load_sqlite.py so that the properties
checked on the two models can be compared directly. Requires a running and
reachable Neo4j container (see README.md, Setup); it skips the tests with an
explicit message if the connection is not available, instead of failing in
an unclear way.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "loader"))
from load_neo4j import load  # noqa: E402

NEO4J_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "secretgraph")


def write_sample_jsonl(path: str) -> None:
    """Same synthetic dataset as test_load_sqlite.py: one coinbase, one
    OP_RETURN output (empty address), one address receiving two outputs
    in two different transactions."""
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


def _try_connect():
    from neo4j import GraphDatabase

    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
        return driver
    except Exception:
        return None


class TestLoadNeo4j(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = _try_connect()
        if cls.driver is None:
            raise unittest.SkipTest(
                f"Neo4j not reachable at {NEO4J_URI}: start the container (README.md, Setup) before running this test"
            )

    @classmethod
    def tearDownClass(cls):
        if cls.driver is not None:
            cls.driver.close()

    def setUp(self):
        # Wipes the database completely before every test, so that each
        # test starts from a known state regardless of the execution
        # order.
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            for constraint in session.run("SHOW CONSTRAINTS").data():
                session.run(f"DROP CONSTRAINT {constraint['name']} IF EXISTS")

        self.tmpdir = tempfile.mkdtemp()
        self.jsonl_path = os.path.join(self.tmpdir, "sample.jsonl")
        write_sample_jsonl(self.jsonl_path)
        load(self.jsonl_path, NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, chunk_size=10)

    def _run(self, query, **params):
        with self.driver.session() as session:
            return session.run(query, **params).data()

    def test_all_blocks_and_transactions_loaded(self):
        n_blocks = self._run("MATCH (b:Block) RETURN COUNT(b) AS n")[0]["n"]
        n_tx = self._run("MATCH (t:Transaction) RETURN COUNT(t) AS n")[0]["n"]
        self.assertEqual(n_blocks, 1)
        self.assertEqual(n_tx, 2)

    def test_output_without_address_has_no_locked_to_edge(self):
        """The OP_RETURN Output must exist as a node (faithful
        translation of the raw data) but with no LOCKED_TO edge, the Neo4j
        equivalent of address IS NULL on the SQL side."""
        row = self._run(
            "MATCH (o:Output {outputRef: $ref}) "
            "OPTIONAL MATCH (o)-[:LOCKED_TO]->(a:Address) "
            "RETURN o.value AS value, a IS NULL AS no_address",
            ref=("b" * 64) + ":1",
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["value"], 0)
        self.assertTrue(row[0]["no_address"])

    def test_address_deduplicated_across_outputs(self):
        """addr1 receives two outputs in two different transactions: only
        one Address node must exist, but with two incoming LOCKED_TO
        edges."""
        n_addr1_nodes = self._run(
            "MATCH (a:Address {address: 'addr1'}) RETURN COUNT(a) AS n"
        )[0]["n"]
        self.assertEqual(n_addr1_nodes, 1)

        n_outputs = self._run(
            "MATCH (:Output)-[:LOCKED_TO]->(a:Address {address: 'addr1'}) RETURN COUNT(*) AS n"
        )[0]["n"]
        self.assertEqual(n_outputs, 2)

    def test_coinbase_input_present_without_spends_edge(self):
        """The coinbase input (prevIndex = 4294967295) must exist as an
        Input node with the prevTxid/prevIndex properties saved, but with
        no outgoing SPENDS edge (no Output it could reference). This is
        exactly the case that was fixed in loader/load_neo4j.py after
        finding that prevTxid/prevIndex were not being saved."""
        row = self._run(
            "MATCH (i:Input {inputRef: $ref}) "
            "OPTIONAL MATCH (i)-[:SPENDS]->(o:Output) "
            "RETURN i.prevTxid AS prevTxid, i.prevIndex AS prevIndex, o IS NULL AS no_spends",
            ref=("b" * 64) + ":0",
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["prevTxid"], "0" * 64)
        self.assertEqual(row[0]["prevIndex"], 4294967295)
        self.assertTrue(row[0]["no_spends"])

    def test_all_constraints_created(self):
        """Direct regression check on the bug found during development:
        init_schema() was silently dropping the CREATE CONSTRAINT
        statements preceded by a multi-line comment block. All 6
        constraints declared in schema/schema.cypher must be present after
        loading."""
        names = {c["name"] for c in self._run("SHOW CONSTRAINTS")}
        expected = {
            "block_hash_unique",
            "tx_txid_unique",
            "addr_address_unique",
            "output_ref_unique",
            "input_ref_unique",
            "price_date_unique",
        }
        self.assertEqual(names, expected)

    def test_spends_edge_created_for_resolvable_input(self):
        """The input of the second transaction spends output 0 of the
        first one: the corresponding SPENDS edge must exist (unlike the
        coinbase input, this output IS present in the dataset)."""
        row = self._run(
            "MATCH (i:Input {inputRef: $ref})-[:SPENDS]->(o:Output) RETURN o.outputRef AS outputRef",
            ref=("c" * 64) + ":0",
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["outputRef"], ("b" * 64) + ":0")


if __name__ == "__main__":
    unittest.main()
