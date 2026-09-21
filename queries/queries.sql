-- Analytical queries (Phase 3) for the relational schema.
-- They all compute derived data at query time (fee, UTXO, main chain),
-- rather than storing it in the schema.
-- Cypher equivalents in queries/queries.cypher, same order and numbering.

-- ============================================================
-- Q1: Fee of each transaction (excludes coinbase transactions, which have
-- no fee by protocol construction: they do not spend a previous output).
-- ============================================================
SELECT
    tx.txid,
    (SELECT SUM(o.value) FROM tx_inputs i
     JOIN tx_outputs o ON o.txid = i.prev_txid AND o.output_index = i.prev_output_index
     WHERE i.txid = tx.txid AND i.prev_output_index != 4294967295) AS total_input,
    (SELECT SUM(value) FROM tx_outputs WHERE txid = tx.txid) AS total_output,
    (SELECT SUM(o.value) FROM tx_inputs i
     JOIN tx_outputs o ON o.txid = i.prev_txid AND o.output_index = i.prev_output_index
     WHERE i.txid = tx.txid AND i.prev_output_index != 4294967295)
    - (SELECT SUM(value) FROM tx_outputs WHERE txid = tx.txid) AS fee
FROM transactions tx
WHERE EXISTS (
    SELECT 1 FROM tx_inputs i
    WHERE i.txid = tx.txid AND i.prev_output_index != 4294967295
)
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
-- Q3: Balance per address (received - spent), positive balances only.
-- ============================================================
WITH received AS (
    SELECT address, SUM(value) AS total_received
    FROM tx_outputs WHERE address IS NOT NULL GROUP BY address
),
spent AS (
    SELECT o.address, SUM(o.value) AS total_spent
    FROM tx_outputs o
    JOIN tx_inputs i ON i.prev_txid = o.txid AND i.prev_output_index = o.output_index
    WHERE o.address IS NOT NULL GROUP BY o.address
)
SELECT
    r.address,
    r.total_received,
    COALESCE(s.total_spent, 0) AS total_spent,
    r.total_received - COALESCE(s.total_spent, 0) AS balance
FROM received r
LEFT JOIN spent s ON r.address = s.address
WHERE r.total_received - COALESCE(s.total_spent, 0) > 0
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
-- Q7: Balance per WALLET (cluster of addresses), not per single address.
-- Requires address_clusters to be already populated (clustering/cluster_sqlite.py).
-- ============================================================
WITH received AS (
    SELECT ac.cluster_id, SUM(o.value) AS total_received
    FROM tx_outputs o
    JOIN address_clusters ac ON ac.address = o.address
    GROUP BY ac.cluster_id
),
spent AS (
    SELECT ac.cluster_id, SUM(o.value) AS total_spent
    FROM tx_outputs o
    JOIN tx_inputs i ON i.prev_txid = o.txid AND i.prev_output_index = o.output_index
    JOIN address_clusters ac ON ac.address = o.address
    GROUP BY ac.cluster_id
)
SELECT
    r.cluster_id,
    r.total_received,
    COALESCE(s.total_spent, 0) AS total_spent,
    r.total_received - COALESCE(s.total_spent, 0) AS wallet_balance
FROM received r
LEFT JOIN spent s ON r.cluster_id = s.cluster_id
ORDER BY wallet_balance DESC
LIMIT 20;

-- ============================================================
-- Q8: USD value of each transaction at the time it happened
-- (time-interval join with price_history, "closest price that is not
-- later").
-- ============================================================
SELECT
    tx.txid,
    b.timestamp,
    p.price_usd,
    (SELECT SUM(value) FROM tx_outputs WHERE txid = tx.txid) AS total_output_satoshi,
    (SELECT SUM(value) FROM tx_outputs WHERE txid = tx.txid) / 100000000.0 * p.price_usd AS total_output_usd
FROM transactions tx
JOIN blocks b ON b.hash = tx.block_hash
JOIN price_history p ON p.date_unix = (
    SELECT MAX(date_unix) FROM price_history WHERE date_unix <= b.timestamp
)
ORDER BY total_output_usd DESC
LIMIT 20;
