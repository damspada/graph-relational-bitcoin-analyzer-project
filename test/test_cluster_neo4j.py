"""
Tests for address clustering on Neo4j (clustering/cluster_neo4j.py) on
synthetic data. Exactly the same scenario as test/test_cluster_sqlite.py
(A-B-C transitively co-owned, D and ISOLATO separate) so that the
properties checked on the two models can be compared directly. Requires a
running Neo4j container with the APOC and GDS plugins (see README.md, Setup).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clustering"))
from cluster_neo4j import run  # noqa: E402

NEO4J_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "secretgraph")


def _try_connect():
    from neo4j import GraphDatabase

    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
        return driver
    except Exception:
        return None


class TestClusterNeo4j(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = _try_connect()
        if cls.driver is None:
            raise unittest.SkipTest(
                f"Neo4j not reachable at {NEO4J_URI}: start the container with GDS (README.md, Setup) before running this test"
            )
        with cls.driver.session() as session:
            try:
                session.run("CALL gds.list() YIELD name RETURN name LIMIT 1")
            except Exception:
                raise unittest.SkipTest("Graph Data Science (GDS) plugin not available on this Neo4j instance")

    @classmethod
    def tearDownClass(cls):
        if cls.driver is not None:
            cls.driver.close()

    def setUp(self):
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

            # Same scenario as test_cluster_sqlite.py, translated into the
            # model with explicit Output/Input nodes (schema/schema.cypher):
            # tx1 produces outputs for A, B (x2); tx3 produces outputs for C, D;
            # txZ produces an output for ISOLATO.
            # tx10 spends (tx1,0)=A and (tx1,1)=B together -> A-B co-owned.
            # tx11 spends (tx1,2)=B and (tx3,0)=C together -> B-C co-owned.
            session.run("""
                CREATE (a:Address {address:'addrA'}), (b:Address {address:'addrB'}),
                       (c:Address {address:'addrC'}), (d:Address {address:'addrD'}),
                       (iso:Address {address:'addrISOLATO'})
                CREATE (t1:Transaction {txid:'tx1'}), (t3:Transaction {txid:'tx3'}),
                       (t10:Transaction {txid:'tx10'}), (t11:Transaction {txid:'tx11'}),
                       (tz:Transaction {txid:'txZ'})
                CREATE (o1a:Output {outputRef:'tx1:0', value:100}),
                       (o1b:Output {outputRef:'tx1:1', value:200}),
                       (o1b2:Output {outputRef:'tx1:2', value:50}),
                       (o3c:Output {outputRef:'tx3:0', value:400}),
                       (o3d:Output {outputRef:'tx3:1', value:500}),
                       (ozi:Output {outputRef:'txZ:0', value:999})
                CREATE (t1)-[:HAS_OUTPUT]->(o1a)-[:LOCKED_TO]->(a)
                CREATE (t1)-[:HAS_OUTPUT]->(o1b)-[:LOCKED_TO]->(b)
                CREATE (t1)-[:HAS_OUTPUT]->(o1b2)-[:LOCKED_TO]->(b)
                CREATE (t3)-[:HAS_OUTPUT]->(o3c)-[:LOCKED_TO]->(c)
                CREATE (t3)-[:HAS_OUTPUT]->(o3d)-[:LOCKED_TO]->(d)
                CREATE (tz)-[:HAS_OUTPUT]->(ozi)-[:LOCKED_TO]->(iso)
                CREATE (i10a:Input {inputRef:'tx10:0'}), (i10b:Input {inputRef:'tx10:1'})
                CREATE (t10)-[:HAS_INPUT]->(i10a)-[:SPENDS]->(o1a)
                CREATE (t10)-[:HAS_INPUT]->(i10b)-[:SPENDS]->(o1b)
                CREATE (i11a:Input {inputRef:'tx11:0'}), (i11b:Input {inputRef:'tx11:1'})
                CREATE (t11)-[:HAS_INPUT]->(i11a)-[:SPENDS]->(o1b2)
                CREATE (t11)-[:HAS_INPUT]->(i11b)-[:SPENDS]->(o3c)
            """)

        run(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    def _run(self, query, **params):
        with self.driver.session() as session:
            return session.run(query, **params).data()

    def test_transitively_connected_addresses_share_cluster(self):
        rows = self._run(
            "MATCH (a:Address) WHERE a.address IN ['addrA','addrB','addrC'] "
            "RETURN a.address AS address, a.clusterId AS clusterId"
        )
        cluster_ids = {r["clusterId"] for r in rows}
        self.assertEqual(len(cluster_ids), 1, f"expected the same cluster, found {rows}")

    def test_unconnected_addresses_remain_separate(self):
        rows = {
            r["address"]: r["clusterId"]
            for r in self._run(
                "MATCH (a:Address) WHERE a.address IN ['addrA','addrD','addrISOLATO'] "
                "RETURN a.address AS address, a.clusterId AS clusterId"
            )
        }
        self.assertNotEqual(rows["addrD"], rows["addrA"])
        self.assertNotEqual(rows["addrISOLATO"], rows["addrA"])
        self.assertNotEqual(rows["addrD"], rows["addrISOLATO"])

    def test_all_addresses_assigned_a_cluster(self):
        n_addr = self._run("MATCH (a:Address) RETURN COUNT(a) AS n")[0]["n"]
        n_assigned = self._run("MATCH (a:Address) WHERE a.clusterId IS NOT NULL RETURN COUNT(a) AS n")[0]["n"]
        self.assertEqual(n_addr, n_assigned)

    def test_coowned_with_edges_created_for_shared_input_transactions(self):
        """A-B and B-C must have a direct COOWNED_WITH edge (co-spent in
        the same transaction); A-C must not (never co-spent directly,
        only transitively through B)."""
        ab = self._run(
            "MATCH (:Address {address:'addrA'})-[r:COOWNED_WITH]-(:Address {address:'addrB'}) RETURN COUNT(r) AS n"
        )[0]["n"]
        bc = self._run(
            "MATCH (:Address {address:'addrB'})-[r:COOWNED_WITH]-(:Address {address:'addrC'}) RETURN COUNT(r) AS n"
        )[0]["n"]
        ac = self._run(
            "MATCH (:Address {address:'addrA'})-[r:COOWNED_WITH]-(:Address {address:'addrC'}) RETURN COUNT(r) AS n"
        )[0]["n"]
        self.assertGreaterEqual(ab, 1)
        self.assertGreaterEqual(bc, 1)
        self.assertEqual(ac, 0)


if __name__ == "__main__":
    unittest.main()
