# NodeCoin

SQLite vs Neo4j on real Bitcoin blockchain data. Same domain modelled
twice, 8 equivalent queries in SQL and Cypher, measured with the same
protocol.

Dataset: 977 blocks (13-20 May 2026), 4,310,870 transactions, 2,552,839
addresses.

Fee, spent status and orphan blocks are not stored: both models compute
them at query time, so the comparison is about the queries and not about
who precomputed more.

## Results

| Query | SQLite | Neo4j | Faster |
|---|---|---|---|
| Q1 transaction fee | 61.5 s | 24.6 s | Neo4j 2.5x |
| Q2 unspent outputs | 12.5 s | 9.4 s | Neo4j 1.3x |
| Q3 balance per address | 40.3 s | 10.8 s | Neo4j 3.7x |
| Q4 fund tracing | 9.7 s | timeout | SQLite |
| Q5 hub detection | 9.6 s | 9.0 s | tie |
| Q6 main chain vs orphans | 2.1 ms | 17.7 ms | SQLite 8.4x |
| Q7 balance per wallet | n/a | 12.3 s | Neo4j only |
| Q8 value in USD | 16.7 s | 12.4 s | Neo4j 1.3x |

Loading: SQLite 9.4 min / 6.2 GB, Neo4j 40.2 min / 16.3 GB with 8 GB heap.
Q7 also needs the clustering step, which takes 84 min on Neo4j and does
not converge on SQLite at this scale.

Method: warm-up discarded, median of 3 runs, 120 s timeout on SQLite and
90 s on Neo4j, four dataset sizes (50/200/500/977 blocks). Raw results in
`benchmark/results/`.

## Setup

Python dependencies are declared inline with
[uv](https://docs.astral.sh/uv/), so there is no virtualenv to create:
`uv run --with neo4j --with requests <script>` fetches them on first use.
With pip instead, `pip install -r requirements.txt` and plain `python3`
work the same.

```shell
cd go-parser && go build -o nodecoin-parser . && cd ..

docker run -d --name nodecoin-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/secretgraph \
  -e NEO4J_PLUGINS='["apoc","graph-data-science"]' \
  -e NEO4J_dbms_security_procedures_unrestricted='gds.*,apoc.*' \
  neo4j:latest
```

## Demo

A 50-transaction slice of the same data, small enough to read on screen.
It is already in the repo, no download needed.

```shell
docker start nodecoin-neo4j
uv run --with neo4j --with requests demo/serve_sqlite.py --db demo/demo.sqlite
```

SQLite at http://127.0.0.1:8000, Neo4j Browser at http://localhost:7474
(`neo4j` / `secretgraph`). Ready-made Cypher queries in
`demo/demo_queries.cypher`. Details in `demo/README.md`.

If the demo database is missing, rebuild it:

```shell
UV="uv run --with neo4j --with requests"

$UV demo/make_demo_dataset.py --target-tx 50
$UV loader/load_sqlite.py demo/demo_dataset.jsonl --db demo/demo.sqlite
$UV loader/load_neo4j.py demo/demo_dataset.jsonl
$UV loader/load_prices.py prices_may.jsonl --target sqlite --db demo/demo.sqlite
$UV loader/load_prices.py prices_may.jsonl --target neo4j
$UV clustering/cluster_sqlite.py --db demo/demo.sqlite
$UV clustering/cluster_neo4j.py
```

## Full pipeline

Download, parse, load, check, cluster, query, benchmark.

```shell
UV="uv run --with neo4j --with requests"

# 1. download blocks and prices
$UV scripts/download_blocks.py --start-height 949242 --end-height 950218 --out blocks_data
$UV scripts/download_prices.py --days 120 --out prices.jsonl

# 2. parse into JSONL
go-parser/nodecoin-parser -blocks-dir blocks_data -out dataset.jsonl

# 3. load both databases
$UV loader/load_sqlite.py dataset.jsonl --db nodecoin_full.sqlite
$UV loader/load_neo4j.py dataset.jsonl
$UV loader/load_prices.py prices.jsonl --target sqlite --db nodecoin_full.sqlite
$UV loader/load_prices.py prices.jsonl --target neo4j

# 4. check the two databases hold the same data (must exit 0)
$UV quality/compare.py --db nodecoin_full.sqlite

# 5. clustering (needed by Q7)
$UV clustering/cluster_sqlite.py --db nodecoin_full.sqlite
$UV clustering/cluster_neo4j.py

# 6. queries and execution plans
$UV queries/explain_sqlite.py --db nodecoin_full.sqlite

# 7. the numbers in the results table above
$UV benchmark/bench_queries.py --backend sqlite --db nodecoin_full.sqlite --runs 3 --timeout 120
$UV benchmark/bench_queries.py --backend neo4j --runs 3 --timeout 90 --has-clustering

# 8. the scalability curves, four sizes per engine
$UV benchmark/make_subsets.py go-parser/full_run.jsonl --sizes 50 200 500 977
$UV benchmark/run_scaling_sqlite.py --cluster-sizes 50,200
$UV benchmark/run_scaling_neo4j.py  --cluster-sizes 50,200
```

Step 8 rebuilds each database from scratch for every size, so it takes
hours: about one for SQLite, two for Neo4j. `--cluster-sizes` limits the
clustering to the sizes where it is affordable (on 200 blocks it already
costs 29 minutes on SQLite against 53 s of loading, because it works on
42 million co-ownership edges). Where it did not run, Q7 is reported as
skipped. Results in `benchmark/results/scaling_*.csv`.

Step 1 takes hours and needs ~7 GB. `download_blocks.py` resumes if
interrupted and supports `--provider mempool` when Blockstream hits its
rate limit of 700 requests/hour.

## Tests

```shell
cd go-parser && go test ./... && cd ..

UV="uv run --with neo4j --with requests"
$UV test/test_load_sqlite.py
$UV test/test_cluster_sqlite.py
$UV test/test_load_neo4j.py
$UV test/test_cluster_neo4j.py
```

The Neo4j tests wipe the database, so reload the demo dataset afterwards
if you need it.

## Layout

```
go-parser/    binary blocks to JSONL
loader/       JSONL to SQLite and Neo4j
schema/       both schemas
clustering/   address clustering
quality/      consistency check between the two databases
queries/      Q1-Q8 in SQL and Cypher
benchmark/    timings and scalability curves
demo/         reduced dataset and web UI
```

Requires Go 1.21+, Python 3.10+, Docker.
