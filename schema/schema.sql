-- Relational schema (SQLite) for NodeCoin.
-- Derived from the JSONL produced by go-parser/. The rationale of every
-- choice (normalization, absence of strict FKs on tx_inputs, no derived
-- columns such as fee/spent) is explained in the comments below.

PRAGMA foreign_keys = ON;

-- A block exactly as read from the .blk file, in physical reading order.
-- No "height" field (not present in the raw format, it can only be derived
-- by following prev_hash) nor "is_orphan" (it depends on the comparison with
-- the whole dataset, it has to be computed at query time).
CREATE TABLE blocks (
    hash        CHAR(64) PRIMARY KEY,
    prev_hash   CHAR(64),
    timestamp   INTEGER NOT NULL       -- Unix epoch
);

-- No FK on prev_hash -> blocks.hash: the predecessor of the first block of
-- the downloaded window does not exist in the dataset by construction
-- (a time window was downloaded, not the whole chain from the genesis).
CREATE INDEX idx_blocks_prev_hash ON blocks(prev_hash);
CREATE INDEX idx_blocks_timestamp ON blocks(timestamp);

-- A transaction. txid is the natural key (cryptographic hash, unique by
-- construction). No fee column: derived at query time
-- (Sum(input value) - Sum(output value)).
CREATE TABLE transactions (
    txid        CHAR(64) PRIMARY KEY,
    block_hash  CHAR(64) NOT NULL,
    size        INTEGER NOT NULL,
    FOREIGN KEY (block_hash) REFERENCES blocks(hash)
);

CREATE INDEX idx_transactions_block_hash ON transactions(block_hash);

-- Address as a separate entity (normalization): the same address string
-- appears as the recipient of many different outputs, keeping it as a free
-- column in tx_outputs would violate 3NF.
CREATE TABLE addresses (
    address     TEXT PRIMARY KEY
);

-- A transaction output. Composite key (txid, output_index): an output has
-- no identifier of its own in the Bitcoin domain, it is a weak entity with
-- respect to transactions.
-- address is NULL (not an empty string) when the script does not match any
-- standard pattern (OP_RETURN, custom scripts).
-- No "spent" column: derived at query time by checking whether a tx_inputs
-- row referencing (txid, output_index) exists.
CREATE TABLE tx_outputs (
    txid          CHAR(64) NOT NULL,
    output_index  INTEGER NOT NULL,
    address       TEXT,               -- NULL if the script is non-standard
    value         BIGINT NOT NULL,    -- satoshi, never float (avoids rounding errors)
    PRIMARY KEY (txid, output_index),
    FOREIGN KEY (txid) REFERENCES transactions(txid),
    FOREIGN KEY (address) REFERENCES addresses(address)
);

CREATE INDEX idx_tx_outputs_address ON tx_outputs(address);

-- A transaction input: only the reference to the output it is spending
-- (prev_txid, prev_output_index), no resolution of the value (that is the
-- job of the queries, not of the loader).
--
-- NO FOREIGN KEY on (prev_txid, prev_output_index) -> tx_outputs:
-- two legitimate categories of inputs do not resolve to an output present
-- in the dataset:
--   1. coinbase inputs (prev_txid = 64 zeros, prev_output_index = 4294967295)
--   2. inputs that spend an output preceding the downloaded time window
--      (out of range, not a loading error)
-- A strict FK here would make the loading fail systematically.
-- The logical link stays expressed via JOIN in the queries.
CREATE TABLE tx_inputs (
    txid                TEXT NOT NULL,
    input_index         INTEGER NOT NULL,
    prev_txid           CHAR(64) NOT NULL,
    prev_output_index   INTEGER NOT NULL,
    PRIMARY KEY (txid, input_index),
    FOREIGN KEY (txid) REFERENCES transactions(txid)
);

-- Composite index: essential to efficiently resolve "which output is this
-- input spending" (fee, UTXO, fund tracing queries).
CREATE INDEX idx_tx_inputs_prev ON tx_inputs(prev_txid, prev_output_index);

-- RESULT table (not raw data): it materializes the address -> cluster
-- assignment computed by the address clustering query
-- (common-input-ownership heuristic).
-- It is not populated by the loader: it is written by a separate script
-- that runs the clustering query on the already loaded database and
-- materializes its output, so that subsequent queries (e.g. "wallet
-- balance") can do a JOIN instead of recomputing the clustering every
-- time. cluster_id is the smallest address (lexicographically) of the
-- group: a deterministic representative, not an arbitrary surrogate id.
CREATE TABLE address_clusters (
    address     TEXT PRIMARY KEY,
    cluster_id  TEXT NOT NULL,
    FOREIGN KEY (address) REFERENCES addresses(address)
);

CREATE INDEX idx_address_clusters_cluster ON address_clusters(cluster_id);

-- Second data source, heterogeneous with respect to the Bitcoin blocks:
-- BTC/USD price history at daily granularity (scripts/download_prices.py).
-- It is not derived from the blockchain dataset: it is an independent
-- external source, with its own time granularity (one point per day)
-- different from that of the transactions (timestamp at the second), which
-- is what makes the join between the two an interval join.
CREATE TABLE price_history (
    date_unix   INTEGER PRIMARY KEY,   -- UTC midnight of the day, Unix epoch
    price_usd   REAL NOT NULL          -- the only REAL value in the schema: a market price, not a Bitcoin domain datum
);
