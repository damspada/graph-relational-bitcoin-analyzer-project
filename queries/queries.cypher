// Analytical queries (Phase 3) for the graph schema. Same numbering as
// queries/queries.sql, same meaning for every query, for direct
// comparison. Model: (:Transaction)-[:HAS_OUTPUT]->(:Output)-[:LOCKED_TO]->(:Address),
// (:Transaction)-[:HAS_INPUT]->(:Input)-[:SPENDS]->(:Output).
// See schema/schema.cypher.
// No tie-break after the ORDER BY of the rankings: on the full dataset
// there is no tie at the LIMIT boundary of Q1, Q2, Q3 or Q5 (checked), and
// a tie-break on txid/address is free for SQLite, which has them in the
// row, but costs Neo4j a property read for every row before the LIMIT.


// ============================================================
// Q1: Fee of each transaction (excludes coinbase: prevIndex = 4294967295,
// no SPENDS relationship).
// ============================================================
MATCH (t:Transaction)-[:HAS_INPUT]->(i:Input)-[:SPENDS]->(prevOut:Output)
WHERE i.prevIndex <> 4294967295
WITH t, SUM(prevOut.value) AS total_input
MATCH (t)-[:HAS_OUTPUT]->(o:Output)
WITH t, total_input, SUM(o.value) AS total_output
RETURN t.txid, total_input, total_output, total_input - total_output AS fee
ORDER BY fee DESC
LIMIT 100;

// ============================================================
// Q2: UTXO: Output nodes with no Input spending them (no incoming
// SPENDS relationship). The txid is read from outputRef ("txid:index"),
// like SQL reads it from the row, without a hop to the Transaction. The
// address is a separate node, so it is read only for the 100 outputs
// that are kept, not for every unspent output.
// ============================================================
MATCH (o:Output)
WHERE NOT (:Input)-[:SPENDS]->(o)
WITH o
ORDER BY o.value DESC
LIMIT 100
OPTIONAL MATCH (o)-[:LOCKED_TO]->(a:Address)
RETURN split(o.outputRef, ':')[0] AS txid, o.outputIndex, a.address, o.value
ORDER BY o.value DESC;

// ============================================================
// Q3: Balance per address: the sum of its unspent outputs (no incoming
// SPENDS relationship). Same formulation as the SQL version.
// ============================================================
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
WHERE NOT (o)<-[:SPENDS]-(:Input)
RETURN a.address, SUM(o.value) AS balance
ORDER BY balance DESC
LIMIT 100;

// ============================================================
// Q4: Fund tracing: follows the flow of funds from a starting address,
// up to 8 hops, output by output, exactly like the recursive CTE of Q4 in
// queries.sql.
//
// One hop is Output <- SPENDS - Input <- HAS_INPUT - Transaction
// - HAS_OUTPUT -> Output (3 relationships): from an output to the input
// that spends it (at most one, an output is spent only once) and from
// there to all the outputs of the spending transaction. The address of
// each output reached is read at the end, with an OPTIONAL MATCH because
// non-standard scripts have no address (NULL, as on the SQL side).
// {0,8} includes depth 0, the outputs of the starting address, like the
// base case of the CTE. Verified: same rows as the SQL query, on every
// address of the demo dataset and on the full dataset.
// ============================================================
MATCH (:Address {address: $start_address})<-[:LOCKED_TO]-(o:Output)
MATCH path = (o)
  ((:Output)<-[:SPENDS]-(:Input)<-[:HAS_INPUT]-(:Transaction)-[:HAS_OUTPUT]->(:Output)){0,8}
  (reached:Output)
OPTIONAL MATCH (reached)-[:LOCKED_TO]->(a:Address)
RETURN DISTINCT a.address AS address, length(path) / 3 AS depth
ORDER BY depth;

// ============================================================
// Q5: Most active addresses (hub detection): by number of outputs received.
// ============================================================
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
RETURN a.address, COUNT(o) AS n_outputs, SUM(o.value) AS total_value
ORDER BY n_outputs DESC
LIMIT 20;

// ============================================================
// Q6: Main chain vs orphan blocks: native traversal along PREV_BLOCK
// from the most recent block. Compare with the equivalent SQL recursive
// CTE (Q6 in queries.sql).
//
// The PREV_BLOCK relationship is created by the loader as (previous_block)-[:PREV_BLOCK]->(next_block),
// that is, it points forward in time (see loader/load_neo4j.py). To walk
// back from the tip it must therefore be traversed in the REVERSE
// direction (<-[:PREV_BLOCK]-), not forward: a first attempt with the
// arrow in the wrong direction wrongly marked 9 blocks out of 10 as
// orphans on a real sample: verified and fixed against Neo4j.
// ============================================================
CALL () {
    MATCH (b:Block) RETURN MAX(b.timestamp) AS maxTimestamp
}
MATCH (tip:Block {timestamp: maxTimestamp})
MATCH (tip)<-[:PREV_BLOCK*0..]-(ancestor:Block)
// the list holds the nodes, not their hashes: checking a node against it
// compares node ids, not 64-character strings for every block
WITH COLLECT(DISTINCT ancestor) AS mainChain
MATCH (b:Block)
RETURN b.hash, b.timestamp, NOT (b IN mainChain) AS is_orphan;

// ============================================================
// Q7: Balance per WALLET (cluster of addresses), not per single address:
// the sum of the unspent outputs of all the addresses of the wallet.
// Requires clusterId to be already written by clustering/cluster_neo4j.py (gds.wcc).
// ============================================================
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
WHERE a.clusterId IS NOT NULL AND NOT (o)<-[:SPENDS]-(:Input)
RETURN a.clusterId AS clusterId, SUM(o.value) AS wallet_balance
ORDER BY wallet_balance DESC
LIMIT 20;

// ============================================================
// Q8: USD value of each transaction at the time it happened
// (time-interval join with PriceSnapshot). PriceSnapshot has no edges,
// so the closest earlier price is found like in SQL: the MAX date not
// later than the block, then a lookup on the unique dateUnix.
// ============================================================
MATCH (b:Block)-[:CONTAINS]->(t:Transaction)-[:HAS_OUTPUT]->(o:Output)
WITH t, b, SUM(o.value) AS total_output_satoshi
MATCH (p:PriceSnapshot) WHERE p.dateUnix <= b.timestamp
WITH t, b, total_output_satoshi, MAX(p.dateUnix) AS closest_date
MATCH (closest:PriceSnapshot {dateUnix: closest_date})
RETURN t.txid, b.timestamp, closest.priceUsd,
       total_output_satoshi,
       (total_output_satoshi / 100000000.0) * closest.priceUsd AS total_output_usd
ORDER BY total_output_usd DESC
LIMIT 20;
