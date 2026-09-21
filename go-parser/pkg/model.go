package pkg

// Output represents a single transaction output.
// Address can be an empty string if the script does not match
// any standard pattern (OP_RETURN, non-standard multisig, custom scripts).
type Output struct {
	Index   int    `json:"index"`
	Address string `json:"address"`
	Value   int64  `json:"value"` // satoshis
}

// Input represents a single transaction input: only the reference to the
// previous output it is spending. No value resolution: that is up to the
// downstream queries (SQL/Cypher), not to the parser.
type Input struct {
	PrevTxid  string `json:"prev_txid"`
	PrevIndex int    `json:"prev_index"`
}

// Transaction represents a transaction as it appears in the block,
// with no derived field (no fee, no explicit is_coinbase: a tx is a
// coinbase if Inputs is empty).
type Transaction struct {
	Txid    string  `json:"txid"`
	Size    int     `json:"size"`
	Inputs  []Input `json:"inputs"`
	Outputs []Output `json:"outputs"`
}

// Block represents a block as read from the .blk file, in physical read
// order (not necessarily in height order on the chain).
// No height, no is_orphan: those are derived concepts, not observed
// directly from a single block.
type Block struct {
	Hash         string        `json:"hash"`
	PrevHash     string        `json:"prev_hash"`
	Timestamp    int64         `json:"timestamp"`
	Transactions []Transaction `json:"transactions"`
}
