// ============================================================
// Ready-made queries for the live demo in Neo4j Browser
// (http://localhost:7474: user neo4j, password secretgraph)
//
// These are meant TO BE SHOWN, not to be benchmarked: they start from
// the simple case ("let us look at the graph") and build up to the 8
// queries of the project, with a comment explaining what happens.
//
// The official Q1-Q8 queries stay in queries/queries.cypher: these are
// the same ones, trimmed down to what projects well on screen.
// ============================================================


// ------------------------------------------------------------
// 0. STARTING POINT: the whole demo graph, at a glance
//    522 nodes, 498 relationships: the entire dataset fits on one
//    screen. Useful to say "this is the model, now let us walk it".
// ------------------------------------------------------------
MATCH (n) RETURN n LIMIT 300;


// ------------------------------------------------------------
// 1. THE SCHEMA, which node types and relationships exist
//    Show this first: it answers "what does the model look like?"
// ------------------------------------------------------------
CALL db.schema.visualization();


// ------------------------------------------------------------
// 2. THE CHAIN OF BLOCKS: PREV_BLOCK in a row
//    Six consecutive blocks: the chain is visible, and Q6 becomes
//    clear.
// ------------------------------------------------------------
MATCH path = (b:Block)-[:PREV_BLOCK*]->(:Block)
RETURN path
ORDER BY length(path) DESC
LIMIT 1;


// ------------------------------------------------------------
// 3. ANATOMY OF A TRANSACTION: the heart of the model
//    A single Transaction node with everything around it: the block
//    that contains it, its inputs, its outputs and the addresses they
//    are locked to. This is the picture that shows why Output and
//    Input are nodes of their own and not properties.
// ------------------------------------------------------------
MATCH (b:Block)-[:CONTAINS]->(t:Transaction)-[:HAS_OUTPUT]->(o:Output)
OPTIONAL MATCH (o)-[:LOCKED_TO]->(a:Address)
OPTIONAL MATCH (t)-[:HAS_INPUT]->(i:Input)
OPTIONAL MATCH (i)-[:SPENDS]->(prev:Output)
WITH t, COUNT(DISTINCT o) AS n_out
ORDER BY n_out DESC
LIMIT 1
MATCH p = (blk:Block)-[:CONTAINS]->(t)-[:HAS_OUTPUT]->(:Output)-[:LOCKED_TO]->(:Address)
RETURN p LIMIT 25;


// ------------------------------------------------------------
// 4. A COINBASE: money created out of nothing
//    The only transaction with no predecessor: its Input has no
//    outgoing SPENDS edge. This is why, on the SQL side, tx_inputs
//    cannot have a strict FK on prev_txid.
// ------------------------------------------------------------
MATCH (t:Transaction)-[:HAS_INPUT]->(i:Input)
WHERE i.prevIndex = 4294967295
MATCH p = (t)-[:HAS_OUTPUT]->(:Output)
RETURN p, i
LIMIT 20;


// ------------------------------------------------------------
// 5. Q2, UTXO: the outputs nobody has spent yet
//    The question "by absence": no incoming SPENDS edge.
// ------------------------------------------------------------
MATCH (t:Transaction)-[:HAS_OUTPUT]->(o:Output)
WHERE NOT (:Input)-[:SPENDS]->(o)
OPTIONAL MATCH (o)-[:LOCKED_TO]->(a:Address)
RETURN t.txid AS transaction, o.outputIndex AS output_index,
       a.address AS address_str, o.value AS satoshis
ORDER BY o.value DESC
LIMIT 15;


// ------------------------------------------------------------
// 6. Q2 in VISUAL form: spent (red) against unspent
//    Same question as the previous one, but as a graph: you can see
//    the Outputs with and without an incoming edge.
// ------------------------------------------------------------
MATCH (o:Output)
OPTIONAL MATCH (i:Input)-[s:SPENDS]->(o)
WITH o, s LIMIT 60
OPTIONAL MATCH p = (o)-[:LOCKED_TO]->(:Address)
RETURN o, s, p;


// ------------------------------------------------------------
// 7. Q3, balance of an address
//    We start from the Address node and only look at its neighbours:
//    no aggregation over the whole table, which is what SQL needs.
// ------------------------------------------------------------
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
WITH a, o, EXISTS { (o)<-[:SPENDS]-(:Input) } AS spent
WITH a,
     SUM(o.value) AS received,
     SUM(CASE WHEN spent THEN o.value ELSE 0 END) AS already_spent
RETURN a.address AS address_str, received, already_spent,
       received - already_spent AS balance
ORDER BY balance DESC
LIMIT 15;


// ------------------------------------------------------------
// 8. Q4, FUND TRACING: follow the money
//    The flagship case for the graph model. One "hop" goes from an
//    output to the input that spends it, then to the outputs of that
//    transaction (3 edges), up to 8 times: the same walk as the
//    recursive CTE in SQL. The address of each output reached is shown
//    at the end. The demo dataset contains a real 6-hop chain.
// ------------------------------------------------------------
MATCH (start:Address)<-[:LOCKED_TO]-(o:Output)<-[:SPENDS]-(:Input)
WITH start, COUNT(*) AS spent_count
ORDER BY spent_count DESC
LIMIT 1
MATCH (start)<-[:LOCKED_TO]-(first:Output)
MATCH path = (first)
  ((:Output)<-[:SPENDS]-(:Input)<-[:HAS_INPUT]-(:Transaction)
   -[:HAS_OUTPUT]->(:Output)){1,8}
  (reached:Output)
OPTIONAL MATCH owner = (reached)-[:LOCKED_TO]->(:Address)
RETURN path, owner
LIMIT 25;


// ------------------------------------------------------------
// 9. Q5, hub detection: who receives the most
//    Counting the incoming edges of a node = counting its degree.
// ------------------------------------------------------------
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
RETURN a.address AS address_str,
       COUNT(o) AS outputs_received,
       SUM(o.value) AS total_satoshis
ORDER BY outputs_received DESC
LIMIT 10;


// ------------------------------------------------------------
// 10. Q7, CLUSTERING: the addresses belonging to the same wallet
//     The COOWNED_WITH edge is DERIVED, it is not present in the raw
//     data: the clustering script creates it by applying the
//     common-input-ownership heuristic. Here you see the real clusters
//     of the demo dataset (95 addresses → 91 clusters).
// ------------------------------------------------------------
MATCH p = (a:Address)-[:COOWNED_WITH]-(b:Address)
RETURN p;


// ------------------------------------------------------------
// 11. Q7, balance aggregated per wallet instead of per address
//     Same question as Q3, but on the cluster: this is the query that
//     the clustering makes possible.
// ------------------------------------------------------------
MATCH (a:Address)<-[:LOCKED_TO]-(o:Output)
WHERE a.clusterId IS NOT NULL
WITH a.clusterId AS wallet, o, EXISTS { (o)<-[:SPENDS]-(:Input) } AS spent
WITH wallet,
     COUNT(DISTINCT o) AS n_outputs,
     SUM(o.value) AS received,
     SUM(CASE WHEN spent THEN o.value ELSE 0 END) AS already_spent
RETURN wallet, n_outputs, received, received - already_spent AS wallet_balance
ORDER BY wallet_balance DESC
LIMIT 10;


// ------------------------------------------------------------
// 12. Q8, how much it was worth in dollars at transaction time
//     The PriceSnapshot nodes have no edges to the rest of the graph:
//     the link is temporal, not structural. This shows clearly why the
//     graph has no structural advantage on this query.
// ------------------------------------------------------------
MATCH (b:Block)-[:CONTAINS]->(t:Transaction)-[:HAS_OUTPUT]->(o:Output)
WITH t, b, SUM(o.value) AS satoshis
MATCH (p:PriceSnapshot) WHERE p.dateUnix <= b.timestamp
WITH t, satoshis, p
ORDER BY p.dateUnix DESC
WITH t, satoshis, COLLECT(p)[0] AS price
RETURN t.txid AS transaction,
       satoshis,
       price.priceUsd AS btc_usd_price,
       round((satoshis / 100000000.0) * price.priceUsd, 2) AS usd_value
ORDER BY usd_value DESC
LIMIT 10;


// ------------------------------------------------------------
// 13. CROSS-CHECK: the same numbers as the quality check
//     Useful if the professor asks "how do you know the two databases
//     really contain the same things?"
// ------------------------------------------------------------
MATCH (b:Block)        WITH COUNT(b) AS blocks
MATCH (t:Transaction)  WITH blocks, COUNT(t) AS transactions
MATCH (o:Output)       WITH blocks, transactions, COUNT(o) AS output
MATCH (i:Input)        WITH blocks, transactions, output, COUNT(i) AS input
MATCH (a:Address)      RETURN blocks, transactions, output, input,
                              COUNT(a) AS addresses;
