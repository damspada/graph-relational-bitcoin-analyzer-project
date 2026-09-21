// nodecoin-parser: translates Bitcoin Core's binary blk*.dat files into
// JSON Lines (one block per line), in streaming, with no domain
// computation (no fees, no spent/unspent state, no identification of the
// main chain or orphans: all of this is delegated to the downstream
// SQL/Cypher queries, so that the relational-vs-graph comparison stays as
// fair as possible).
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"

	"nodecoin-parser/pkg"
)

func main() {
	blocksDir := flag.String("blocks-dir", "", "Directory containing the blk*.dat files (required)")
	outPath := flag.String("out", "", "Output .jsonl file (default: stdout)")
	maxBlocks := flag.Int("max-blocks", 0, "Maximum number of blocks to process (0 = no limit)")
	flag.Parse()

	if *blocksDir == "" {
		fmt.Fprintln(os.Stderr, "error: -blocks-dir is required")
		flag.Usage()
		os.Exit(1)
	}

	var out io.Writer = os.Stdout
	if *outPath != "" {
		f, err := os.Create(*outPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error creating output file: %v\n", err)
			os.Exit(1)
		}
		defer f.Close()
		out = f
	}
	writer := bufio.NewWriter(out)
	defer writer.Flush()
	encoder := json.NewEncoder(writer)

	files, err := findBlkFiles(*blocksDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error reading blocks directory: %v\n", err)
		os.Exit(1)
	}
	if len(files) == 0 {
		fmt.Fprintf(os.Stderr, "no blk*.dat file found in %s\n", *blocksDir)
		os.Exit(1)
	}

	processed := 0
	for _, path := range files {
		if *maxBlocks > 0 && processed >= *maxBlocks {
			break
		}

		n, err := processFile(path, encoder, *maxBlocks, processed)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error processing %s: %v\n", path, err)
			os.Exit(1)
		}
		processed += n
	}

	fmt.Fprintf(os.Stderr, "Done: %d blocks written.\n", processed)
}

// findBlkFiles returns the paths of the blk*.dat files sorted by name
// (increasing numeric order: blk00000.dat, blk00001.dat, ...).
func findBlkFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}

	var files []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		matched, _ := filepath.Match("blk*.dat", e.Name())
		if matched {
			files = append(files, filepath.Join(dir, e.Name()))
		}
	}
	sort.Strings(files)
	return files, nil
}

// processFile reads all the blocks in a file, converts them and writes
// them as JSON lines. It stops if it reaches maxBlocks (0 = no limit).
func processFile(path string, encoder *json.Encoder, maxBlocks, alreadyProcessed int) (int, error) {
	reader, err := pkg.NewBlockReader(path)
	if err != nil {
		return 0, err
	}
	defer reader.Close()

	count := 0
	for {
		if maxBlocks > 0 && alreadyProcessed+count >= maxBlocks {
			break
		}

		msgBlock, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return count, err
		}

		block := pkg.ConvertBlock(msgBlock)
		if err := encoder.Encode(block); err != nil {
			return count, fmt.Errorf("error writing JSON: %w", err)
		}

		count++
		if count%500 == 0 {
			fmt.Fprintf(os.Stderr, "  ...%d blocks processed (file: %s)\n", alreadyProcessed+count, filepath.Base(path))
		}
	}

	return count, nil
}
