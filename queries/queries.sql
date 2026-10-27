-- Analytical queries (Phase 3) for the relational schema.
-- They all compute derived data at query time (fee, UTXO, main chain),
-- rather than storing it in the schema.
-- Cypher equivalents in queries/queries.cypher, same order and numbering.
-- No tie-break after the ORDER BY of the rankings: on the full dataset
-- there is no tie at the LIMIT boundary of Q1, Q2, Q3 or Q5 (checked), and
-- a tie-break on txid/address is free for SQLite, which has them in the
-- row, but costs Neo4j a property read for every row before the LIMIT.


-- ============================================================
-- Q1: Fee of each transaction (excludes coinbase transactions, which have
-- no fee by protocol construction: they do not spend a previous output).
-- Same steps as the Cypher version: first the input sum, only for the
-- transactions with at least one input that resolves to an output, then
-- the output sum for those. Each sum is computed once per transaction:
-- the CTEs are MATERIALIZED because otherwise SQLite inlines them and
-- evaluates the correlated subqueries again in every column that uses
-- them (checked with EXPLAIN QUERY PLAN).
-- ============================================================
WITH with_input AS MATERIALIZED (
    SELECT tx.txid,
           (SELECT SUM(o.value) FROM tx_inputs i
            JOIN tx_outputs o ON o.txid = i.prev_txid AND o.output_index = i.prev_output_index
            WHERE i.txid = tx.txid AND i.prev_output_index != 4294967295) AS total_input
    FROM transactions tx
),
amounts AS MATERIALIZED (
    SELECT w.txid, w.total_input,
           (SELECT SUM(value) FROM tx_outputs WHERE txid = w.txid) AS total_output
    FROM with_input w
    WHERE w.total_input IS NOT NULL
)
SELECT txid, total_input, total_output, total_input - total_output AS fee
FROM amounts
ORDER BY fee DESC
LIMIT 100;

-- ============================================================
-- Q2: UTXO: outputs not yet spent (no input references them).
-- ============================================================
SELECT o.txid, o.output_index, o.address, o.value
FROM tx_outputs o
WHERE NOT EXISTS (
    SELECT 1 FROM tx_inputs i
    WHERE i.prev_txid = o.txid AND i.prev_output_index = o.output_index
)
ORDER BY o.value DESC
LIMIT 100;

-- ============================================================
-- Q3: Balance per address: the sum of its unspent outputs.
-- Equivalent to received - spent (every output is either spent or not),
-- but it needs one aggregation and an anti-join instead of two
-- aggregations joined with a LEFT JOIN. Addresses with nothing left
-- unspent have no rows, so only positive balances come out.
-- ============================================================
SELECT o.address, SUM(o.value) AS balance
FROM tx_outputs o
WHERE o.address IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM tx_inputs i
    WHERE i.prev_txid = o.txid AND i.prev_output_index = o.output_index
)
GROUP BY o.address
ORDER BY balance DESC
LIMIT 100;

-- ============================================================
-- Q4: Fund tracing: follows the flow of funds from a starting address,
-- N hops (recursive CTE). Compare with the Cypher equivalent (native
-- traversal): this is the project's central case study on the advantage
-- of the graph for queries of this kind.
--
-- FIX: the previous formulation joined o1 to the transaction
-- (`o1.txid = flow.txid`) but filtered the output_index only in the
-- WHERE clause, separately from the join. SQLite does not recognize the
-- equivalence and evaluates o1 as "any output of the transaction", not
-- specifically the output owned by the flow address: the join therefore
-- produces one row for EVERY output of the transaction before discarding
-- those that do not match in the WHERE clause, instead of producing a
-- single row for the correct join. Measured: on the benchmark's
-- reference address, this produced 76,580 rows at the first hop instead
-- of the correct 56,027 (+37%, verified with an independent counting
-- query). The fix brings output_index into the CTE itself and uses it
-- directly in the join condition (`i.prev_output_index =
-- flow.output_index`), allowing SQLite to exploit the composite index
-- idx_tx_inputs_prev(prev_txid, prev_output_index) instead of just
-- prev_txid.
-- ============================================================
WITH RECURSIVE flow(address, txid, output_index, depth) AS (
    SELECT o.address, o.txid, o.output_index, 0
    FROM tx_outputs o WHERE o.address = :start_address

    UNION ALL

    SELECT o2.address, o2.txid, o2.output_index, flow.depth + 1
    FROM flow
    JOIN tx_inputs i ON i.prev_txid = flow.txid AND i.prev_output_index = flow.output_index
    JOIN tx_outputs o2 ON o2.txid = i.txid
    WHERE flow.depth < 8
)
SELECT DISTINCT address, depth FROM flow ORDER BY depth;

-- ============================================================
-- Q5: Most active addresses (hub detection): by number of outputs received.
-- ============================================================
SELECT address, COUNT(*) AS n_outputs, SUM(value) AS total_value
FROM tx_outputs
WHERE address IS NOT NULL
GROUP BY address
ORDER BY n_outputs DESC
LIMIT 20;

-- ============================================================
-- Q6: Main chain vs orphan blocks: identifies the best chain by
-- following prev_hash backwards from the block of maximum height
-- (approximated here as the maximum timestamp, since there is no
-- explicit height field). As for orphans: with the downloaded time
-- window and a cutoff at sufficient depth, we expect zero or very few
-- orphans in the real dataset.
-- ============================================================
WITH RECURSIVE main_chain(hash, prev_hash) AS (
    SELECT hash, prev_hash FROM blocks
    WHERE timestamp = (SELECT MAX(timestamp) FROM blocks)

    UNION ALL

    SELECT b.hash, b.prev_hash
    FROM blocks b
    JOIN main_chain mc ON b.hash = mc.prev_hash
)
SELECT b.hash, b.timestamp,
       CASE WHEN mc.hash IS NULL THEN 1 ELSE 0 END AS is_orphan
FROM blocks b
LEFT JOIN main_chain mc ON b.hash = mc.hash;

-- ============================================================
-- Q7: Balance per WALLET (cluster of addresses), not per single address:
-- the sum of the unspent outputs of all the addresses of the wallet.
-- Requires address_clusters to be already populated (clustering/cluster_sqlite.py).
-- ============================================================
SELECT ac.cluster_id, SUM(o.value) AS wallet_balance
FROM tx_outputs o
JOIN address_clusters ac ON ac.address = o.address
WHERE NOT EXISTS (
    SELECT 1 FROM tx_inputs i
    WHERE i.prev_txid = o.txid AND i.prev_output_index = o.output_index
)
GROUP BY ac.cluster_id
ORDER BY wallet_balance DESC
LIMIT 20;

-- ============================================================
-- Q8: USD value of each transaction at the time it happened
-- (time-interval join with price_history, "closest price that is not
-- later"). The output sum is computed once per transaction: the CTE is
-- MATERIALIZED because otherwise SQLite inlines it and evaluates the
-- correlated subquery twice (checked with EXPLAIN QUERY PLAN).
-- ============================================================
WITH tx_value AS MATERIALIZED (
    SELECT tx.txid, tx.block_hash,
           (SELECT SUM(value) FROM tx_outputs WHERE txid = tx.txid) AS total_output_satoshi
    FROM transactions tx
)
SELECT
    v.txid,
    b.timestamp,
    p.price_usd,
    v.total_output_satoshi,
    v.total_output_satoshi / 100000000.0 * p.price_usd AS total_output_usd
FROM tx_value v
JOIN blocks b ON b.hash = v.block_hash
JOIN price_history p ON p.date_unix = (
    SELECT MAX(date_unix) FROM price_history WHERE date_unix <= b.timestamp
)
ORDER BY total_output_usd DESC
LIMIT 20;
