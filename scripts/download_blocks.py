"""
Downloads real Bitcoin blocks from the public Blockstream API and packs them
into blk*.dat files in Bitcoin Core's native format (magic bytes + size +
payload), so that they can be read by the Go parser exactly like the files
produced by a local Bitcoin Core node.
"""
import requests
import struct
import time
import sys
import os

# Both providers expose the same API (same URL scheme, same response
# format: verified that they return identical hashes for the same height).
# Configurable via --provider, so that if one of the two hits its rate
# limit (verified: Blockstream limits to 700 requests/hour per IP without
# an API key, since July 2025) you can switch to the other without sharing
# its counter, since they are separate domains/infrastructures.
PROVIDERS = {
    "blockstream": "https://blockstream.info/api",
    "mempool": "https://mempool.space/api",
}
API_BASE = PROVIDERS["blockstream"]  # overridden by --provider in __main__
MAGIC_MAINNET = bytes.fromhex("f9beb4d9")
MAX_BLK_FILE_SIZE = 128 * 1024 * 1024  # 128 MB, like Bitcoin Core
MAX_RETRIES = 8
MAX_RATE_LIMIT_RETRIES = 15  # the blockstream.info rate limit can last a few minutes

session = requests.Session()


def _get_with_retry(url: str):
    rate_limit_attempt = 0
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 429:
                rate_limit_attempt += 1
                if rate_limit_attempt > MAX_RATE_LIMIT_RETRIES:
                    raise RuntimeError(f"Persistent rate limit on {url}, too many attempts")
                # Rate limit: respect Retry-After if present, otherwise use an
                # increasing backoff, longer than for a normal network error
                # (blockstream.info can block the IP for several minutes).
                retry_after = int(r.headers.get("Retry-After", 0))
                wait = max(retry_after, min(20 * rate_limit_attempt, 180))
                print(f"  rate limited (429) on {url}, waiting {wait}s (attempt {rate_limit_attempt}/{MAX_RATE_LIMIT_RETRIES})", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt >= MAX_RETRIES:
                raise
            wait = min(2 ** attempt, 60)
            print(f"  retry {attempt}/{MAX_RETRIES} for {url} ({e}), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)


def get_tip_height() -> int:
    r = _get_with_retry(f"{API_BASE}/blocks/tip/height")
    return int(r.text)


def get_hash_at_height(height: int) -> str:
    r = _get_with_retry(f"{API_BASE}/block-height/{height}")
    return r.text.strip()


def get_block_raw(block_hash: str) -> bytes:
    r = _get_with_retry(f"{API_BASE}/block/{block_hash}/raw")
    return r.content


def download_range(start_height: int, end_height: int, out_dir: str, resume: bool = True):
    os.makedirs(out_dir, exist_ok=True)
    checkpoint_path = os.path.join(out_dir, ".checkpoint")

    # Resume: if a checkpoint from a previous run exists in the same
    # directory, restart from the block after the last one downloaded.
    resume_height = start_height
    if resume and os.path.exists(checkpoint_path):
        with open(checkpoint_path) as f:
            last_done = int(f.read().strip())
        resume_height = max(start_height, last_done + 1)
        if resume_height > start_height:
            print(f"Checkpoint found: resuming from height={resume_height}", file=sys.stderr)

    # Find the last existing blk*.dat file to keep appending to it,
    # instead of overwriting what has already been downloaded.
    existing = sorted(f for f in os.listdir(out_dir) if f.startswith("blk") and f.endswith(".dat"))
    if existing:
        file_index = len(existing) - 1
        last_path = os.path.join(out_dir, existing[-1])
        current_size = os.path.getsize(last_path)
        current_file = open(last_path, "ab")
    else:
        file_index = 0
        current_file = open(os.path.join(out_dir, f"blk{file_index:05d}.dat"), "wb")
        current_size = 0

    total = end_height - start_height + 1
    done_before = resume_height - start_height
    for i, height in enumerate(range(resume_height, end_height + 1), done_before + 1):
        block_hash = get_hash_at_height(height)
        raw = get_block_raw(block_hash)

        record = MAGIC_MAINNET + struct.pack("<I", len(raw)) + raw

        if current_size + len(record) > MAX_BLK_FILE_SIZE:
            current_file.close()
            file_index += 1
            current_file = open(os.path.join(out_dir, f"blk{file_index:05d}.dat"), "wb")
            current_size = 0

        current_file.write(record)
        current_size += len(record)

        with open(checkpoint_path, "w") as f:
            f.write(str(height))

        if i % 50 == 0 or i == total:
            print(f"[{i}/{total}] height={height} hash={block_hash} size={len(raw)}B", file=sys.stderr)

        time.sleep(0.25)  # do not hammer the public API (rate limit observed at 0.05s)

    current_file.close()
    os.remove(checkpoint_path)
    range_path = os.path.join(out_dir, ".range")
    if os.path.exists(range_path):
        os.remove(range_path)
    print(f"Done. {file_index + 1} files written to {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download real Bitcoin blocks and generate blk*.dat files")
    parser.add_argument("--days", type=int, default=30, help="How many days back from the tip (default 30, ignored when --start-height is used)")
    parser.add_argument("--start-height", type=int, help="Explicit start height (alternative to --days)")
    parser.add_argument("--end-height", type=int, help="Explicit end height, inclusive (requires --start-height)")
    parser.add_argument("--out", type=str, default="blocks_data", help="Output directory")
    parser.add_argument("--provider", choices=sorted(PROVIDERS.keys()), default="blockstream",
                        help="Source API (default: blockstream). Use 'mempool' if blockstream is rate limited: same data format, separate request counter.")
    args = parser.parse_args()

    API_BASE = PROVIDERS[args.provider]
    print(f"Provider: {args.provider} ({API_BASE})", file=sys.stderr)

    range_path = os.path.join(args.out, ".range")

    if args.start_height is not None:
        # Explicit range by height: useful to download a precise historical
        # date interval (for example a specific month), instead of "N days
        # back from the tip". end_height defaults to the tip if not given.
        if os.path.exists(range_path):
            with open(range_path) as f:
                start, end = (int(x) for x in f.read().split())
            print(f"Range already fixed previously: {start}-{end}", file=sys.stderr)
        else:
            start = args.start_height
            end = args.end_height if args.end_height is not None else get_tip_height()
            os.makedirs(args.out, exist_ok=True)
            with open(range_path, "w") as f:
                f.write(f"{start} {end}")
            print(f"Downloading blocks from height {start} to {end} inclusive ({end - start + 1} blocks)", file=sys.stderr)
    else:
        blocks_per_day = 144  # ~10 min/block
        n_blocks = args.days * blocks_per_day

        # The range (start, end) must stay fixed from one run to the next,
        # otherwise at every resume the tip moves and the window of blocks
        # being downloaded changes. A .range file next to the checkpoint fixes
        # the two ends at the first start; later runs reuse it as it is.
        if os.path.exists(range_path):
            with open(range_path) as f:
                start, end = (int(x) for x in f.read().split())
            print(f"Range already fixed previously: {start}-{end}", file=sys.stderr)
        else:
            tip = get_tip_height()
            start, end = tip - n_blocks + 1, tip
            os.makedirs(args.out, exist_ok=True)
            with open(range_path, "w") as f:
                f.write(f"{start} {end}")
            print(f"Current tip: {tip}. Downloading blocks from {start} to {end} inclusive ({n_blocks} blocks, ~{args.days} days)", file=sys.stderr)

    download_range(start, end, args.out)
