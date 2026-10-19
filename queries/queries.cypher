// Analytical queries (Phase 3) for the graph schema. Same numbering as
// queries/queries.sql, same meaning for every query, for direct
// comparison. Model: (:Transaction)-[:HAS_OUTPUT]->(:Output)-[:LOCKED_TO]->(:Address),
// (:Transaction)-[:HAS_INPUT]->(:Input)-[:SPENDS]->(:Output).
// See schema/schema.cypher.

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
// SPENDS relationship).
// ============================================================
MATCH (t:Transaction)-[:HAS_OUTPUT]->(o:Output)
WHERE NOT (:Input)-[:SPENDS]->(o)
OPTIONAL MATCH (o)-[:LOCKED_TO]->(a:Address)
RETURN t.txid, o.outputIndex, a.address, o.value
ORDER BY o.value DESC
LIMIT 100;

// ============================================================
// Q3: Balance per address (received - spent), positive balances only.
// ============================================================
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
WITH a, o, EXISTS { (o)<-[:SPENDS]-(:Input) } AS is_spent
WITH a,
     SUM(o.value) AS total_received,
     SUM(CASE WHEN is_spent THEN o.value ELSE 0 END) AS total_spent
WITH a, total_received, total_spent, total_received - total_spent AS balance
WHERE balance > 0
RETURN a.address, total_received, total_spent, balance
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
CALL {
    MATCH (b:Block) RETURN MAX(b.timestamp) AS maxTimestamp
}
MATCH (tip:Block {timestamp: maxTimestamp})
MATCH path = (tip)<-[:PREV_BLOCK*0..]-(ancestor:Block)
WITH COLLECT(DISTINCT ancestor.hash) AS mainChainHashes
MATCH (b:Block)
RETURN b.hash, b.timestamp, NOT (b.hash IN mainChainHashes) AS is_orphan;

// ============================================================
// Q7: Balance per WALLET (cluster of addresses), not per single address.
// Requires clusterId to be already written by clustering/cluster_neo4j.py (gds.wcc).
// ============================================================
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
WHERE a.clusterId IS NOT NULL
WITH a.clusterId AS clusterId, o, EXISTS { (o)<-[:SPENDS]-(:Input) } AS is_spent
WITH clusterId,
     SUM(o.value) AS total_received,
     SUM(CASE WHEN is_spent THEN o.value ELSE 0 END) AS total_spent
RETURN clusterId, total_received, total_spent, total_received - total_spent AS wallet_balance
ORDER BY wallet_balance DESC
LIMIT 20;

// ============================================================
// Q8: USD value of each transaction at the time it happened
// (time-interval join with PriceSnapshot).
// ============================================================
MATCH (b:Block)-[:CONTAINS]->(t:Transaction)-[:HAS_OUTPUT]->(o:Output)
WITH t, b, SUM(o.value) AS total_output_satoshi
MATCH (p:PriceSnapshot) WHERE p.dateUnix <= b.timestamp
WITH t, b, total_output_satoshi, p
ORDER BY p.dateUnix DESC
WITH t, b, total_output_satoshi, COLLECT(p)[0] AS closest_price
RETURN t.txid, b.timestamp, closest_price.priceUsd,
       total_output_satoshi,
       (total_output_satoshi / 100000000.0) * closest_price.priceUsd AS total_output_usd
ORDER BY total_output_usd DESC
LIMIT 20;
