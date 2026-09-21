"""
Address clustering (common-input-ownership heuristic) on SQLite.

Heuristic: if two addresses appear as outputs spent by inputs of the same
transaction, they very likely belong to the same owner (no standard
protocol signs a transaction combining keys of different owners without an
explicit multisig scheme). The resulting problem is a connected-components
on an Address-Address graph.

SQLite has no declarative way to compute connected components in a single
query: a recursive CTE with more than one reference to itself in the
recursive body is not supported ("multiple recursive references"). This
script therefore implements the propagation as a sequence of SQL rounds
(label propagation), orchestrated by Python, until convergence or a
maximum number of rounds. Compare with the Cypher equivalent
(clustering/cluster_neo4j.py), which solves the same problem with a single
native call.

WARNING (measured, not theoretical): on a sample of 50 real Bitcoin
blocks (~181k addresses, ~7.2M co-ownership edges) the first round alone
takes more than 90 seconds. The cost grows super-linearly with the
density of the graph, not with the number of addresses: a few very large
clusters (e.g. exchanges consolidating funds from thousands of addresses)
are enough to make every round extremely expensive. On datasets of this
scale, this script is the weak point of the comparison by construction,
not because of a bug.
"""
import sqlite3
import sys
import time

MAX_ROUNDS = 50  # explicit limit: without it, a very connected cluster
                  # (e.g. an exchange with thousands of addresses) could
                  # require many rounds before converging. In practice, on
                  # dense graphs the per-round cost (not the number of
                  # rounds) is the real bottleneck: see the note above.


def build_coowned_table(conn: sqlite3.Connection) -> None:
    """Materializes the pairs of co-owned addresses (symmetric edge) into
    a temporary table, with an index to make the lookup of neighbors
    efficient during the propagation."""
    conn.executescript("""
        DROP TABLE IF EXISTS coowned_sym;
        CREATE TEMP TABLE coowned_sym AS
        SELECT
            CASE WHEN o1.address < o2.address THEN o1.address ELSE o2.address END AS address,
            CASE WHEN o1.address < o2.address THEN o2.address ELSE o1.address END AS neighbor
        FROM tx_inputs i1
        JOIN tx_inputs i2 ON i1.txid = i2.txid AND i1.input_index < i2.input_index
        JOIN tx_outputs o1 ON o1.txid = i1.prev_txid AND o1.output_index = i1.prev_output_index
        JOIN tx_outputs o2 ON o2.txid = i2.prev_txid AND o2.output_index = i2.prev_output_index
        WHERE o1.address IS NOT NULL AND o2.address IS NOT NULL AND o1.address != o2.address;

        INSERT INTO coowned_sym SELECT neighbor, address FROM coowned_sym;

        CREATE INDEX idx_coowned_sym_address ON coowned_sym(address);
    """)


def build_clusters_table(conn: sqlite3.Connection) -> None:
    """Initializes every address as its own cluster (cluster_id = the
    address itself): the subsequent propagation merges the clusters via
    the minimum."""
    conn.executescript("""
        DROP TABLE IF EXISTS clusters_working;
        CREATE TEMP TABLE clusters_working AS
        SELECT DISTINCT address, address AS cluster_id
        FROM tx_outputs WHERE address IS NOT NULL;

        CREATE UNIQUE INDEX idx_clusters_working_address ON clusters_working(address);
    """)


def propagate(conn: sqlite3.Connection, max_rounds: int = MAX_ROUNDS) -> int:
    """Runs label propagation rounds until there are no more updates or
    max_rounds is reached. Each round is a single UPDATE (not recursive):
    every address takes the minimum between its own cluster_id and that of
    its direct neighbors. Returns the number of rounds actually executed."""
    for round_n in range(1, max_rounds + 1):
        changed = conn.execute("""
            UPDATE clusters_working
            SET cluster_id = (
                SELECT MIN(c2.cluster_id)
                FROM coowned_sym cs
                JOIN clusters_working c2 ON c2.address = cs.neighbor
                WHERE cs.address = clusters_working.address
                UNION ALL
                SELECT clusters_working.cluster_id
            )
            WHERE EXISTS (
                SELECT 1 FROM coowned_sym cs
                JOIN clusters_working c2 ON c2.address = cs.neighbor
                WHERE cs.address = clusters_working.address
                  AND c2.cluster_id < clusters_working.cluster_id
            )
        """).rowcount
        conn.commit()
        print(f"  round {round_n}: {changed} addresses updated", file=sys.stderr)
        if changed == 0:
            return round_n
    print(f"  WARNING: limit of {max_rounds} rounds reached without convergence "
          f"(possible on very large/dense clusters, e.g. exchange addresses)", file=sys.stderr)
    return max_rounds


def materialize_clusters(conn: sqlite3.Connection) -> int:
    """Writes the final result into the address_clusters table (schema.sql).
    Returns the number of rows written."""
    conn.execute("DELETE FROM address_clusters")
    conn.execute("""
        INSERT INTO address_clusters (address, cluster_id)
        SELECT address, cluster_id FROM clusters_working
    """)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM address_clusters").fetchone()[0]


def run(db_path: str, max_rounds: int = MAX_ROUNDS) -> None:
    conn = sqlite3.connect(db_path)

    t0 = time.perf_counter()
    print("Building co-ownership graph...", file=sys.stderr)
    build_coowned_table(conn)
    n_edges = conn.execute("SELECT COUNT(*) FROM coowned_sym").fetchone()[0]
    print(f"  {n_edges} (symmetric) edges found", file=sys.stderr)

    build_clusters_table(conn)

    print("Label propagation (connected components)...", file=sys.stderr)
    n_rounds = propagate(conn, max_rounds)

    n_addresses = materialize_clusters(conn)
    n_clusters = conn.execute("SELECT COUNT(DISTINCT cluster_id) FROM address_clusters").fetchone()[0]

    elapsed = time.perf_counter() - t0
    print(f"Completed in {elapsed:.1f}s, {n_rounds} rounds: "
          f"{n_addresses} addresses grouped into {n_clusters} clusters", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Address clustering on SQLite (common-input-ownership)")
    parser.add_argument("--db", required=True, help="Path of the already populated SQLite database")
    parser.add_argument("--max-rounds", type=int, default=MAX_ROUNDS)
    args = parser.parse_args()

    run(args.db, args.max_rounds)
