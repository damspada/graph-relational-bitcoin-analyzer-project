"""
Address clustering (common-input-ownership heuristic) on Neo4j.

Same heuristic as clustering/cluster_sqlite.py (see that file for the full
rationale): addresses that appear as outputs spent together by inputs of
the same transaction very likely belong to the same owner.

Unlike SQLite, here the connected components problem has a direct, native
solution: an auxiliary COOWNED_WITH relationship is built between the
pairs of co-owned addresses (reached through
Transaction-[:HAS_INPUT]->Input-[:SPENDS]->Output-[:LOCKED_TO]->Address,
consistent with the schema with explicit Output/Input nodes in
schema.cypher), then the Weakly Connected Components (WCC) algorithm of
Neo4j's Graph Data Science library is used: a single call, designed for
large graphs, without the manual rounds needed on the SQL side.

Requires the Neo4j GDS plugin installed in the instance (included in the
official Docker image via APOC/GDS, it must be enabled explicitly if not
present).
"""
import sys
import time

from neo4j import GraphDatabase

# Construction of the COOWNED_WITH relationships between addresses
# co-spent in the same transaction (common-input-ownership).
#
# The naive version did a double MATCH on the same Transaction in a single
# query/transaction: for a transaction with N distinct input addresses it
# generates N*(N-1) pairs, and some real transactions reach ~230 input
# addresses (>26,000 pairs from a single transaction). Over millions of
# transactions, in a single Neo4j transaction without intermediate
# commits, this saturates memory and times out (verified on the 977-block
# dataset).
#
# This version: 1) iterates PER TRANSACTION, not per pair of addresses;
# 2) collects the distinct input addresses of each transaction and
# generates the pairs only once (i<j via UNWIND RANGE); 3) uses
# CALL { ... } IN TRANSACTIONS to commit in batches, so that memory does
# not accumulate the whole result but only one batch of transactions at a
# time.
CREATE_COOWNED_EDGES = """
MATCH (t:Transaction)
CALL (t) {
    MATCH (t)-[:HAS_INPUT]->(:Input)-[:SPENDS]->(:Output)-[:LOCKED_TO]->(a:Address)
    WITH COLLECT(DISTINCT a) AS addrs
    WHERE SIZE(addrs) > 1
    UNWIND RANGE(0, SIZE(addrs) - 2) AS i
    UNWIND RANGE(i + 1, SIZE(addrs) - 1) AS j
    WITH addrs[i] AS a1, addrs[j] AS a2
    MERGE (a1)-[:COOWNED_WITH]-(a2)
} IN TRANSACTIONS OF 500 ROWS
"""

PROJECT_GRAPH = """
CALL gds.graph.project(
    'coowned_graph',
    'Address',
    {COOWNED_WITH: {orientation: 'UNDIRECTED'}}
)
"""

RUN_WCC = """
CALL gds.wcc.write('coowned_graph', {writeProperty: 'clusterId'})
YIELD nodePropertiesWritten, componentCount
RETURN nodePropertiesWritten, componentCount
"""

# YIELD only the name: without it the call returns every column, including
# `schema`, which GDS has deprecated and warns about.
DROP_PROJECTION = "CALL gds.graph.drop('coowned_graph', false) YIELD graphName"


def build_coowned_edges(driver) -> int:
    with driver.session() as session:
        # CALL { ... } IN TRANSACTIONS requires auto-commit mode (it cannot
        # run inside a managed transaction): session.run() without an
        # explicit transaction block provides it. The consume() forces the
        # full execution before continuing.
        session.run(CREATE_COOWNED_EDGES).consume()
        # directed pattern: the undirected one would count every edge twice
        result = session.run("MATCH ()-[r:COOWNED_WITH]->() RETURN COUNT(r) AS n")
        return result.single()["n"]


def run_wcc(driver) -> tuple[int, int]:
    with driver.session() as session:
        session.run(DROP_PROJECTION)  # idempotent: ignores it if it does not exist
    with driver.session() as session:
        try:
            session.run(PROJECT_GRAPH)
            record = session.run(RUN_WCC).single()
            return record["nodePropertiesWritten"], record["componentCount"]
        finally:
            session.run(DROP_PROJECTION)


def run(uri: str, user: str, password: str) -> None:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()

    t0 = time.perf_counter()
    print("Building COOWNED_WITH relationships...", file=sys.stderr)
    n_edges = build_coowned_edges(driver)
    print(f"  {n_edges} relationships created", file=sys.stderr)

    print("Computing connected components (gds.wcc)...", file=sys.stderr)
    n_nodes, n_components = run_wcc(driver)

    elapsed = time.perf_counter() - t0
    print(f"Completed in {elapsed:.1f}s: {n_nodes} addresses grouped into {n_components} clusters", file=sys.stderr)

    driver.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Address clustering on Neo4j (common-input-ownership)")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    args = parser.parse_args()

    run(args.uri, args.user, args.password)
