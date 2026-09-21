package pkg

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"os"

	"github.com/btcsuite/btcd/chaincfg"
	"github.com/btcsuite/btcd/txscript"
	"github.com/btcsuite/btcd/wire"
)

// magicMainnet are the 4 bytes that precede every block in a blk*.dat file
// (Bitcoin mainnet network). They delimit the records inside the file.
var magicMainnet = [4]byte{0xf9, 0xbe, 0xb4, 0xd9}

// BlockReader sequentially reads the block records from a single blk*.dat file.
type BlockReader struct {
	f *os.File
}

func NewBlockReader(path string) (*BlockReader, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	return &BlockReader{f: f}, nil
}

func (r *BlockReader) Close() error {
	return r.f.Close()
}

// Next reads the next record (magic + size + payload) and returns the
// deserialized block. It returns io.EOF when the file is exhausted.
func (r *BlockReader) Next() (*wire.MsgBlock, error) {
	var magic [4]byte
	if _, err := io.ReadFull(r.f, magic[:]); err != nil {
		if err == io.ErrUnexpectedEOF {
			return nil, io.EOF
		}
		return nil, err
	}
	if magic != magicMainnet {
		return nil, fmt.Errorf("invalid magic bytes: %x (corrupted file or not mainnet)", magic)
	}

	var size uint32
	if err := binary.Read(r.f, binary.LittleEndian, &size); err != nil {
		return nil, err
	}

	payload := make([]byte, size)
	if _, err := io.ReadFull(r.f, payload); err != nil {
		return nil, err
	}

	var msgBlock wire.MsgBlock
	if err := msgBlock.Deserialize(bytes.NewReader(payload)); err != nil {
		return nil, fmt.Errorf("error deserializing block: %w", err)
	}

	return &msgBlock, nil
}

// ConvertBlock translates a wire.MsgBlock (Bitcoin's native P2P format)
// into our output structure. No domain computation: translation only.
func ConvertBlock(mb *wire.MsgBlock) Block {
	b := Block{
		Hash:      mb.BlockHash().String(),
		PrevHash:  mb.Header.PrevBlock.String(),
		Timestamp: mb.Header.Timestamp.Unix(),
	}

	b.Transactions = make([]Transaction, 0, len(mb.Transactions))
	for _, tx := range mb.Transactions {
		b.Transactions = append(b.Transactions, convertTx(tx))
	}

	return b
}

func convertTx(tx *wire.MsgTx) Transaction {
	t := Transaction{
		Txid: tx.TxHash().String(),
		Size: tx.SerializeSize(),
	}

	t.Inputs = make([]Input, 0, len(tx.TxIn))
	for _, in := range tx.TxIn {
		// Coinbase transactions have a "dummy" input that does not
		// spend any real output: it is recognizable by the all-zero
		// hash, and it is represented as-is anyway (pure translation,
		// no interpretation: it is up to the queries to infer that it
		// is a coinbase from an empty Inputs or from this pattern).
		prevHash := in.PreviousOutPoint.Hash.String()
		t.Inputs = append(t.Inputs, Input{
			PrevTxid:  prevHash,
			PrevIndex: int(in.PreviousOutPoint.Index),
		})
	}

	t.Outputs = make([]Output, 0, len(tx.TxOut))
	for i, out := range tx.TxOut {
		addr := extractAddress(out.PkScript)
		t.Outputs = append(t.Outputs, Output{
			Index:   i,
			Address: addr,
			Value:   out.Value,
		})
	}

	return t
}

// extractAddress tries to derive the standard address from a scriptPubKey.
// It covers P2PKH, P2SH, P2WPKH, P2WSH, P2TR (via the btcd library). If the
// script does not match any standard pattern (OP_RETURN, bare multisig,
// custom/non-standard scripts), it returns an empty string: this is a loss
// of information accepted deliberately, not an error to handle.
func extractAddress(pkScript []byte) string {
	_, addrs, _, err := txscript.ExtractPkScriptAddrs(pkScript, &chaincfg.MainNetParams)
	if err != nil || len(addrs) == 0 {
		return ""
	}
	return addrs[0].EncodeAddress()
}
