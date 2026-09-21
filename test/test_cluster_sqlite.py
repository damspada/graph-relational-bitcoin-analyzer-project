"""
Tests for address clustering on SQLite (clustering/cluster_sqlite.py) on
synthetic data. They check the fundamental property of the
common-input-ownership heuristic: addresses transitively connected by
shared inputs end up in the same cluster; addresses never co-spent stay
isolated clusters.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clustering"))
from cluster_sqlite import run  # noqa: E402

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "schema", "schema.sql")


def init_test_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    with open(SCHEMA_PATH) as f:
        for stmt in f.read().split(";"):
            code = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
            if code:
                conn.execute(code)
    return conn


class TestClusterSqlite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.sqlite")
        conn = init_test_db(self.db_path)

        # Scenario: A-B co-owned (tx10 spends both as inputs),
        # B-C co-owned (tx11 spends B and C together) -> A,B,C in the same
        # cluster by transitivity. D and ISOLATO never share an input.
        conn.executescript("""
            INSERT INTO blocks VALUES ('b1', NULL, 1000);
            INSERT INTO transactions VALUES ('tx1','b1',1),('tx3','b1',1),
                ('tx10','b1',1),('tx11','b1',1),('txZ','b1',1);
            INSERT INTO addresses VALUES ('addrA'),('addrB'),('addrC'),('addrD'),('addrISOLATO');
            INSERT INTO tx_outputs VALUES
                ('tx1',0,'addrA',100), ('tx1',1,'addrB',200), ('tx1',2,'addrB',50),
                ('tx3',0,'addrC',400), ('tx3',1,'addrD',500),
                ('txZ',0,'addrISOLATO',999);
            INSERT INTO tx_inputs VALUES
                ('tx10',0,'tx1',0), ('tx10',1,'tx1',1),
                ('tx11',0,'tx1',2), ('tx11',1,'tx3',0);
        """)
        conn.commit()
        conn.close()

        run(self.db_path, max_rounds=50)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()

    def test_transitively_connected_addresses_share_cluster(self):
        rows = dict(self.conn.execute(
            "SELECT address, cluster_id FROM address_clusters "
            "WHERE address IN ('addrA','addrB','addrC')"
        ).fetchall())
        self.assertEqual(len(set(rows.values())), 1, f"expected the same cluster, found {rows}")

    def test_unconnected_addresses_remain_separate(self):
        rows = dict(self.conn.execute(
            "SELECT address, cluster_id FROM address_clusters "
            "WHERE address IN ('addrA','addrD','addrISOLATO')"
        ).fetchall())
        # D and ISOLATO must not end up in the A/B/C cluster nor together
        self.assertNotEqual(rows["addrD"], rows["addrA"])
        self.assertNotEqual(rows["addrISOLATO"], rows["addrA"])
        self.assertNotEqual(rows["addrD"], rows["addrISOLATO"])

    def test_all_addresses_assigned_exactly_one_cluster(self):
        n_addr = self.conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
        n_assigned = self.conn.execute("SELECT COUNT(*) FROM address_clusters").fetchone()[0]
        self.assertEqual(n_addr, n_assigned)

    def test_cluster_id_is_deterministic_min_address(self):
        """The cluster representative must be the lexicographic minimum of
        the group (see schema/schema.sql), not an arbitrary/non-deterministic id."""
        cluster_id = self.conn.execute(
            "SELECT cluster_id FROM address_clusters WHERE address = 'addrA'"
        ).fetchone()[0]
        self.assertEqual(cluster_id, min("addrA", "addrB", "addrC"))


if __name__ == "__main__":
    unittest.main()
