package pkg

import (
	"bytes"
	"encoding/binary"
	"io"
	"os"
	"testing"
	"time"

	"github.com/btcsuite/btcd/chaincfg/chainhash"
	"github.com/btcsuite/btcd/wire"
)

// buildTestBlock builds a minimal block with a single coinbase
// transaction, to test the parser without depending on real downloaded
// .blk files (which require network connectivity and are not
// deterministic from one run to the next).
func buildTestBlock(t *testing.T) *wire.MsgBlock {
	t.Helper()

	header := wire.BlockHeader{
		Version:   1,
		PrevBlock: chainhash.Hash{},
		Timestamp: time.Unix(1778693452, 0),
	}

	coinbaseTx := wire.NewMsgTx(wire.TxVersion)
	coinbaseTx.AddTxIn(&wire.TxIn{
		PreviousOutPoint: wire.OutPoint{
			Hash:  chainhash.Hash{}, // all zeros: coinbase pattern
			Index: 0xffffffff,
		},
		Sequence: 0xffffffff,
	})
	// Standard P2PKH output: OP_DUP OP_HASH160 <20 bytes> OP_EQUALVERIFY OP_CHECKSIG
	p2pkhScript := append([]byte{0x76, 0xa9, 0x14}, make([]byte, 20)...)
	p2pkhScript = append(p2pkhScript, 0x88, 0xac)
	coinbaseTx.AddTxOut(&wire.TxOut{Value: 5000000000, PkScript: p2pkhScript})

	// OP_RETURN output: unspendable, no address can be derived.
	opReturnScript := append([]byte{0x6a}, []byte("test")...)
	coinbaseTx.AddTxOut(&wire.TxOut{Value: 0, PkScript: opReturnScript})

	block := wire.NewMsgBlock(&header)
	block.AddTransaction(coinbaseTx)
	return block
}

func TestConvertBlock_CoinbaseRecognizedAsRawInput(t *testing.T) {
	mb := buildTestBlock(t)
	block := ConvertBlock(mb)

	if len(block.Transactions) != 1 {
		t.Fatalf("expected 1 transaction, found %d", len(block.Transactions))
	}
	tx := block.Transactions[0]

	// The parser must not mark the coinbase with an explicit flag: it must
	// only translate faithfully the input with the all-zero hash and the
	// maximum index, exactly as it appears in the block. The interpretation
	// "this is a coinbase" is left to the downstream queries.
	if len(tx.Inputs) != 1 {
		t.Fatalf("expected 1 input, found %d", len(tx.Inputs))
	}
	in := tx.Inputs[0]
	if in.PrevIndex != 0xffffffff {
		t.Errorf("expected prev_index 0xffffffff, got %d", in.PrevIndex)
	}
	zeroHash := "0000000000000000000000000000000000000000000000000000000000000000"[:64]
	if in.PrevTxid != zeroHash {
		t.Errorf("expected all-zero prev_txid, got %s", in.PrevTxid)
	}
}

func TestConvertBlock_P2PKHAddressExtracted(t *testing.T) {
	mb := buildTestBlock(t)
	block := ConvertBlock(mb)
	tx := block.Transactions[0]

	if len(tx.Outputs) != 2 {
		t.Fatalf("expected 2 outputs, found %d", len(tx.Outputs))
	}

	// First output: standard P2PKH script, it must resolve to an address.
	if tx.Outputs[0].Address == "" {
		t.Error("P2PKH output: expected a non-empty address")
	}
	if tx.Outputs[0].Value != 5000000000 {
		t.Errorf("expected value 5000000000, got %d", tx.Outputs[0].Value)
	}
}

func TestConvertBlock_OpReturnHasNoAddress(t *testing.T) {
	mb := buildTestBlock(t)
	block := ConvertBlock(mb)
	tx := block.Transactions[0]

	// OP_RETURN is not spendable: no address can be derived, by protocol
	// design, not because of a limitation of the parser.
	if tx.Outputs[1].Address != "" {
		t.Errorf("OP_RETURN output: expected an empty address, got %q", tx.Outputs[1].Address)
	}
	if tx.Outputs[1].Value != 0 {
		t.Errorf("OP_RETURN output: expected value 0, got %d", tx.Outputs[1].Value)
	}
}

// TestBlockReader_RoundTrip checks that a block written in the blk*.dat
// format (magic bytes + size + payload, the same format produced by
// scripts/download_blocks.py) is read back identically by BlockReader.
func TestBlockReader_RoundTrip(t *testing.T) {
	mb := buildTestBlock(t)

	var payload bytes.Buffer
	if err := mb.Serialize(&payload); err != nil {
		t.Fatalf("serialization failed: %v", err)
	}

	tmpFile, err := os.CreateTemp(t.TempDir(), "blk*.dat")
	if err != nil {
		t.Fatalf("could not create temporary file: %v", err)
	}
	defer tmpFile.Close()

	tmpFile.Write(magicMainnet[:])
	binary.Write(tmpFile, binary.LittleEndian, uint32(payload.Len()))
	tmpFile.Write(payload.Bytes())
	tmpFile.Close()

	reader, err := NewBlockReader(tmpFile.Name())
	if err != nil {
		t.Fatalf("opening BlockReader failed: %v", err)
	}
	defer reader.Close()

	got, err := reader.Next()
	if err != nil {
		t.Fatalf("reading the block failed: %v", err)
	}
	if got.BlockHash() != mb.BlockHash() {
		t.Errorf("block hash does not match after the round-trip: expected %s, got %s", mb.BlockHash(), got.BlockHash())
	}

	// A second Next() must return EOF: the file contains only one block.
	if _, err := reader.Next(); err != io.EOF {
		t.Errorf("expected io.EOF after the only block in the file, got %v", err)
	}
}
