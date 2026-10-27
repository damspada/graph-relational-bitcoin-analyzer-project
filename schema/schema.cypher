// Graph schema (Neo4j) for NodeCoin.
// Derived from the JSONL produced by go-parser/. The rationale of the
// modelling choices is explained in the comments below.
//
// Model:
//   (:Block)-[:PREV_BLOCK]->(:Block)
//   (:Block)-[:CONTAINS]->(:Transaction)
//   (:Transaction)-[:HAS_OUTPUT]->(:Output)-[:LOCKED_TO]->(:Address)
//   (:Transaction)-[:HAS_INPUT]->(:Input)-[:SPENDS]->(:Output)
//   (:Address)-[:COOWNED_WITH]-(:Address)   -- derived edge, see below
//
// Output and Input are nodes of their own (not collapsed as properties of a
// direct Address-Transaction edge): this gives the graph schema the same
// granularity as the relational schema (tx_outputs/tx_inputs have their own
// composite key in schema.sql). It is a deliberate choice: a direct
// Address->Transaction model would make fund-tracing artificially shorter on
// the graph side, favouring the comparison in an unfair way with respect to
// SQL, which instead keeps the full granularity of inputs/outputs as
// separate entities.
//
// No derived property on the nodes: no Block.height (it is derived by
// following PREV_BLOCK), no Block.is_orphan (it depends on the comparison
// with the whole dataset), no Output.spent (it is derived at query time by
// checking the absence of an Input node with a SPENDS edge towards that
// Output), no Transaction.fee. Everything computed at query time
// (Phase 3), consistently with the SQL schema.

// --- Uniqueness constraints (they automatically create an index) ---

CREATE CONSTRAINT block_hash_unique IF NOT EXISTS
FOR (b:Block) REQUIRE b.hash IS UNIQUE;

CREATE CONSTRAINT tx_txid_unique IF NOT EXISTS
FOR (t:Transaction) REQUIRE t.txid IS UNIQUE;

CREATE CONSTRAINT addr_address_unique IF NOT EXISTS
FOR (a:Address) REQUIRE a.address IS UNIQUE;

// Output and Input are weak entities (identified only together with the
// transaction they belong to, exactly like tx_outputs/tx_inputs in
// schema.sql): the natural key is composite (txid, index), not a surrogate
// id. Neo4j has no native composite uniqueness constraints over multiple
// properties like SQL: a derived property outputRef/inputRef =
// "txid:index" is used, built at load time (loader/load_neo4j.py), on which
// the uniqueness constraint is applied.
CREATE CONSTRAINT output_ref_unique IF NOT EXISTS
FOR (o:Output) REQUIRE o.outputRef IS UNIQUE;

CREATE CONSTRAINT input_ref_unique IF NOT EXISTS
FOR (i:Input) REQUIRE i.inputRef IS UNIQUE;

// --- Additional indexes for analytical queries (Phase 3/4) ---

CREATE INDEX block_timestamp IF NOT EXISTS
FOR (b:Block) ON (b.timestamp);

// --- Note on "external" Input/Output (coinbase, outside the time window) ---
//
// An Input node is always created anyway (it faithfully represents the raw
// data: every non-coinbase transaction has inputs). The SPENDS edge towards
// the referenced Output is however created only if that Output is already a
// node present in the dataset (its originating Transaction has been
// loaded). For coinbase inputs (prev_txid all zeros) and for inputs that
// reference outputs outside the downloaded time window, the Input stays
// without an outgoing SPENDS edge: the graph's natural way of representing
// "no known predecessor", analogous to the absence of a strict FK on the
// SQL side on tx_inputs.

// --- Address clustering (common-input-ownership heuristic) ---
//
// The COOWNED_WITH edge and clusterId (a property written by gds.wcc) are
// NOT populated by the loader: they are computed by
// clustering/cluster_neo4j.py after the loading, running the Weakly
// Connected Components algorithm on the auxiliary Address-Address graph
// built from the co-occurrence as inputs of the same transaction.
CREATE INDEX address_cluster_id IF NOT EXISTS
FOR (a:Address) ON (a.clusterId);

// --- BTC/USD historical price (external, heterogeneous source) ---
//
// PriceSnapshot node, not connected by any edge to Block/Transaction: the
// relationship with the rest of the graph is temporal (interval join on
// timestamp), not structural, so there is no natural edge to create at load
// time. The nearest-in-time join query is executed at query time by
// comparing the timestamp/dateUnix properties, not through a pre-built edge.
CREATE CONSTRAINT price_date_unique IF NOT EXISTS
FOR (p:PriceSnapshot) REQUIRE p.dateUnix IS UNIQUE;
