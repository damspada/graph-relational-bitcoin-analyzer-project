# Live demo

A 50-transaction slice of the same Bitcoin data, small enough to read on
screen. Real data: the sampler picks *which* transactions to keep, it
does not alter them.

| | full | demo |
|---|---:|---:|
| blocks | 977 | 6 |
| transactions | 4,310,870 | 50 |
| outputs | 9,893,313 | 164 |
| addresses | 2,552,839 | 95 |
| Neo4j load | 40 min | 2 s |
| slowest query | 61 s | < 1 ms |

## Run it

```shell
docker start nodecoin-neo4j
python3 demo/serve_sqlite.py --db demo/demo.sqlite
```

- SQLite: http://127.0.0.1:8000
- Neo4j Browser: http://localhost:7474 (`neo4j` / `secretgraph`)

The web UI lists the tables, runs Q1-Q8 read straight from
`queries/queries.sql`, and has a free SQL editor (`Ctrl/Cmd + Enter`).
The database is opened read-only and only accepts SELECT/WITH/EXPLAIN.

The Cypher versions of Q1-Q8 are in `queries/queries.cypher`, ready to
paste into Neo4j Browser.

## Suggested order

1. Both tabs side by side: same data, two models.
2. One transaction: the graph shows in one picture what SQL needs three
   joined tables for.
3. A coinbase: an input with no predecessor, which is why `tx_inputs`
   has no strict FK on `prev_txid`.
4. Q2 asked by absence: `NOT EXISTS` vs `NOT (:Input)-[:SPENDS]->(o)`.
5. Q4: in Cypher the traversal is visible; in SQL it is a recursive CTE.
6. Q7: 95 addresses become 91 clusters, same result on both models with
   two different algorithms (label propagation vs `gds.wcc`).

## Rebuild

```shell
rm -f demo/demo.sqlite
cd loader && python3 load_sqlite.py ../demo/demo_dataset.jsonl --db ../demo/demo.sqlite && cd ..
python3 loader/load_neo4j.py demo/demo_dataset.jsonl
python3 loader/load_prices.py prices_may.jsonl --target sqlite --db demo/demo.sqlite
python3 loader/load_prices.py prices_may.jsonl --target neo4j
python3 clustering/cluster_sqlite.py --db demo/demo.sqlite
python3 clustering/cluster_neo4j.py
cd quality && python3 compare.py --db ../demo/demo.sqlite && cd ..
```

`load_neo4j.py` merges onto the existing graph, so empty Neo4j first
(`MATCH (n) DETACH DELETE n` is enough on the demo). The sampling is deterministic, so the same blocks give
the same dataset and the prepared queries always return the same rows.

## Why the sample is built this way

A random sample would break the spending chains: inputs would point
outside the sample, so Q2, Q4 and Q7 would have nothing to show.
`make_demo_dataset.py` selects a connected subgraph and deliberately
includes a coinbase, the deepest available spending chain (6 real hops)
and transactions whose inputs come from distinct addresses.

That last point matters: the most common multi-input transactions
consolidate UTXOs of the *same* address, and the common-input-ownership
heuristic then produces no edges at all. An early sample had 6
multi-input transactions and still generated 0 co-ownership edges.

Result: 40% of non-coinbase inputs resolve inside the sample.
