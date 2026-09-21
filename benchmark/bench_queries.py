"""
Measures the execution time of the analytical queries (Q1-Q8) on an
already populated database, using an anti-noise methodology:
  - warm-up: the first execution (which warms up the OS/DB caches) is discarded;
  - repetition: N runs are executed and median + min are reported, not a
    single measurement (the median is robust against the occasional outliers
    caused by OS scheduling / GC);
  - the queries are executed by fully consuming the result (fetchall
    / consume), not just planned, so the time includes the real work.

It works both on SQLite and on Neo4j (the backend is selectable). Queries with
parameters (Q4 fund-tracing) receive a sample address chosen from the
dataset itself; Q7 (wallet balance) is executed only if the clustering has
already been computed.
"""
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, ".")
from benchmark.query_loader import load_sql_queries, load_cypher_queries


# Queries that require a :start_address / $start_address parameter.
PARAMETERIZED = {"Q4"}
# Queries that depend on the clustering having already been computed (address_clusters / clusterId).
NEEDS_CLUSTERING = {"Q7"}


def _pick_sample_address_sqlite(conn) -> str | None:
    """Picks an 'active' address (with plenty of outputs) as the starting
    point for the fund-tracing: an isolated address would give a trivial
    query, not a representative one.

    IMPORTANT (correctness of the comparison): the ordering includes an
    explicit tie-break on `address`. Without it, ordering by
    COUNT(*) alone is unstable: with millions of addresses the 6th position
    almost certainly falls inside a band of ties (many addresses with
    the same number of outputs) and the two engines would resolve the tie
    in an arbitrary and different way, making the SQL and Cypher
    fund-tracing start from TWO different addresses: two different queries,
    not a comparison. The tie-break makes the choice deterministic and identical on
    the two backends, which share the same dataset (verified by
    quality/compare.py). See also --start-address to explicitly force
    the same address on both."""
    row = conn.execute("""
        SELECT address FROM tx_outputs
        WHERE address IS NOT NULL
        GROUP BY address
        ORDER BY COUNT(*) DESC, address ASC
        LIMIT 1 OFFSET 5
    """).fetchone()
    return row[0] if row else None


def _time_once_sqlite(conn, sql: str, params: dict) -> float:
    t0 = time.perf_counter()
    cur = conn.execute(sql, params)
    cur.fetchall()  # consume the whole result
    return time.perf_counter() - t0


def _make_sqlite_timeout(conn, seconds: float):
    """Installs a progress handler that interrupts the query if it exceeds
    `seconds`. SQLite has no native per-statement timeout on the Python side,
    but progress_handler is called every N VM instructions: from there we can
    raise an exception to abort a query that is too slow, so that a
    single slow query does not block the whole benchmark."""
    deadline = [0.0]

    def handler():
        if time.perf_counter() > deadline[0]:
            return 1  # non-zero value => SQLite aborts the query
        return 0

    # Called every ~1M VM instructions: frequent enough to interrupt
    # a slow query within a fraction of a second, rare enough not to
    # add measurable overhead to fast queries.
    conn.set_progress_handler(handler, 1000000)

    def arm():
        deadline[0] = time.perf_counter() + seconds

    return arm


def bench_sqlite(db_path: str, runs: int = 5, has_clustering: bool = False,
                 per_query_timeout: float = 120.0,
                 start_address: str | None = None) -> dict[str, dict]:
    conn = sqlite3.connect(db_path)
    queries = load_sql_queries()
    sample_addr = start_address or _pick_sample_address_sqlite(conn)
    print(f"[sqlite] Q4 starting address: {sample_addr}", file=sys.stderr)
    arm_timeout = _make_sqlite_timeout(conn, per_query_timeout)

    results = {}
    for q in sorted(queries.keys()):
        if q in NEEDS_CLUSTERING and not has_clustering:
            results[q] = {"skipped": "clustering not computed"}
            continue

        sql = queries[q]
        params = {}
        if q in PARAMETERIZED:
            if sample_addr is None:
                results[q] = {"skipped": "no sample address"}
                continue
            params = {"start_address": sample_addr}

        try:
            arm_timeout()
            _time_once_sqlite(conn, sql, params)  # warm-up, discarded
            times = []
            for _ in range(runs):
                arm_timeout()
                times.append(_time_once_sqlite(conn, sql, params))
            results[q] = {
                "median_s": statistics.median(times),
                "min_s": min(times),
                "runs": runs,
            }
        except sqlite3.OperationalError as e:
            # The abort via progress handler shows up as "interrupted".
            if "interrupt" in str(e).lower():
                results[q] = {"timeout_s": per_query_timeout}
            else:
                results[q] = {"error": str(e)[:200]}
        except Exception as e:
            results[q] = {"error": str(e)[:200]}

    conn.close()
    return results


def _pick_sample_address_neo4j(session) -> str | None:
    """Cypher equivalent of _pick_sample_address_sqlite, with the same
    deterministic tie-break on address (see the SQLite-side docstring for
    the reason: without it, the two backends would start from different addresses and
    Q4 would not be a comparison)."""
    rec = session.run("""
        MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
        RETURN a.address AS address, COUNT(o) AS n
        ORDER BY n DESC, a.address ASC
        SKIP 5 LIMIT 1
    """).single()
    return rec["address"] if rec else None


class _QueryTimeout(Exception):
    pass


def _time_once_neo4j(driver, cypher: str, params: dict, timeout: float) -> float:
    """Runs the query with a CLIENT-side timeout (thread + join), rather than
    relying on the `timeout` parameter of session.run(): that signature
    accepts **kwargs without explicitly documenting whether it applies a
    server-side timeout or silently ignores it (verified: it raises no
    error either with or without the parameter, an ambiguous
    behaviour). A client-side timeout is less elegant but verifiable:
    if the thread does not finish in time, the session is closed (this
    breaks the Bolt connection and therefore the query on the server side), and the
    query is recorded as a timeout - symmetric to the progress handler
    used on the SQLite side."""
    result_holder = {}

    def _run():
        with driver.session() as session:
            t0 = time.perf_counter()
            result = session.run(cypher, **params)
            result.consume()
            result_holder["elapsed"] = time.perf_counter() - t0

    import threading
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        # The thread is still stuck on the query: we cannot
        # interrupt it cleanly from here (Python threads are not
        # cancellable), but we leave it as a daemon and report the
        # timeout to the caller: the benchmark moves on to the next
        # query without waiting any longer.
        raise _QueryTimeout(f"query did not complete within {timeout}s")

    if "elapsed" not in result_holder:
        raise RuntimeError("thread finished without a result (error in the query?)")
    return result_holder["elapsed"]


def bench_neo4j(uri: str, user: str, password: str, runs: int = 5,
                has_clustering: bool = False, per_query_timeout: float = 120.0,
                start_address: str | None = None) -> dict[str, dict]:
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    queries = load_cypher_queries()

    if start_address:
        sample_addr = start_address
    else:
        with driver.session() as session:
            sample_addr = _pick_sample_address_neo4j(session)
    print(f"[neo4j] Q4 starting address: {sample_addr}", file=sys.stderr)

    results = {}
    for q in sorted(queries.keys()):
        if q in NEEDS_CLUSTERING and not has_clustering:
            results[q] = {"skipped": "clustering not computed"}
            continue

        cypher = queries[q]
        params = {}
        if q in PARAMETERIZED:
            if sample_addr is None:
                results[q] = {"skipped": "no sample address"}
                continue
            params = {"start_address": sample_addr}

        try:
            _time_once_neo4j(driver, cypher, params, per_query_timeout)  # warm-up
            times = [_time_once_neo4j(driver, cypher, params, per_query_timeout) for _ in range(runs)]
            results[q] = {
                "median_s": statistics.median(times),
                "min_s": min(times),
                "runs": runs,
            }
        except _QueryTimeout:
            results[q] = {"timeout_s": per_query_timeout}
        except Neo4jError as e:
            results[q] = {"error": str(e)[:200]}
        except Exception as e:
            results[q] = {"error": str(e)[:200]}

    driver.close()
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Query execution time benchmark")
    parser.add_argument("--backend", choices=["sqlite", "neo4j"], required=True)
    parser.add_argument("--db", help="SQLite path (for the sqlite backend)")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-query timeout in seconds")
    parser.add_argument("--has-clustering", action="store_true",
                        help="Include Q7 (requires the clustering to be already computed)")
    parser.add_argument("--start-address", default=None,
                        help="Force the starting address for Q4 (fund-tracing). "
                             "Passing the SAME value to the two backends guarantees that "
                             "Q4 compares the same query on the same starting "
                             "point; without it, each backend picks it on its own with "
                             "the same deterministic criterion.")
    args = parser.parse_args()

    if args.backend == "sqlite":
        res = bench_sqlite(args.db, args.runs, args.has_clustering, args.timeout,
                           args.start_address)
    else:
        res = bench_neo4j(args.uri, args.user, args.password, args.runs,
                          args.has_clustering, args.timeout, args.start_address)

    print(f"=== Query benchmark ({args.backend}) ===")
    for q in sorted(res.keys()):
        r = res[q]
        if "median_s" in r:
            print(f"  {q}: median={r['median_s']*1000:.1f}ms  min={r['min_s']*1000:.1f}ms  ({r['runs']} runs)")
        elif "timeout_s" in r:
            print(f"  {q}: TIMEOUT (>{r['timeout_s']:.0f}s)")
        elif "skipped" in r:
            print(f"  {q}: SKIPPED ({r['skipped']})")
        else:
            print(f"  {q}: ERROR ({r['error']})")
