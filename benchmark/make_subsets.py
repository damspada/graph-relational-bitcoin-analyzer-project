"""
Generates JSONL sub-datasets of increasing size by truncating the
complete file produced by the Go parser. Every sub-dataset is a prefix of the
complete file (the first N blocks), so that the larger datasets
contain exactly the smaller ones plus further blocks: this
makes the scalability curves monotone and comparable (no
variability due to different blocks from one size to another).

Note: the complete file (full_run.jsonl) is itself a PARTIAL
dataset until the block download is finished. The script adapts:
it uses as many lines as are available and skips the sizes that
exceed the number of blocks actually present, reporting it.
"""
import os
import sys


def make_subsets(source_jsonl: str, sizes: list[int], out_dir: str) -> list[tuple[int, str]]:
    """Creates one JSONL file for each size in `sizes` (number of blocks).
    Returns the list (effective_n_blocks, path) of the datasets created."""
    os.makedirs(out_dir, exist_ok=True)

    # Count the available blocks only once.
    with open(source_jsonl) as f:
        available = sum(1 for _ in f)
    print(f"Blocks available in the source: {available}", file=sys.stderr)

    created = []
    for size in sorted(sizes):
        effective = min(size, available)
        out_path = os.path.join(out_dir, f"subset_{effective:05d}.jsonl")

        with open(source_jsonl) as fin, open(out_path, "w") as fout:
            for i, line in enumerate(fin):
                if i >= effective:
                    break
                fout.write(line)

        created.append((effective, out_path))
        note = "" if effective == size else f"  ({size} requested, only {available} available)"
        print(f"  {effective} blocks -> {out_path}{note}", file=sys.stderr)

        if effective < size:
            # The following sizes would exceed the available amount anyway:
            # no point generating duplicates of the same truncation.
            break

    # Deduplicate by effective number of blocks (in case several sizes were
    # truncated to the same value).
    seen = set()
    unique = []
    for n, path in created:
        if n not in seen:
            seen.add(n)
            unique.append((n, path))
    return unique


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generates JSONL sub-datasets at increasing sizes")
    parser.add_argument("source", help="Path to the complete JSONL (e.g. dataset.jsonl)")
    parser.add_argument("--sizes", type=int, nargs="+", default=[50, 200, 500, 1000, 2000, 4320],
                        help="Number of blocks for each size (default: 50 200 500 1000 2000 4320)")
    parser.add_argument("--out-dir", default="benchmark/subsets", help="Output folder")
    args = parser.parse_args()

    subsets = make_subsets(args.source, args.sizes, args.out_dir)
    print(f"\nCreated {len(subsets)} sub-datasets:", file=sys.stderr)
    for n, path in subsets:
        print(f"  {n} blocks: {path}", file=sys.stderr)
