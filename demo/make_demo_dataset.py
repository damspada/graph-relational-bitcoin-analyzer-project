"""
Builds a small, BROWSABLE dataset for the live demo, starting from the
real Bitcoin blocks already downloaded.

Why "just a few blocks" is not enough
-------------------------------------
A single real block from the window used in this project holds about
5,400 transactions on average (measured on the first 10 blocks of
benchmark/subsets/subset_00050.jsonl). Even one whole block would fill
the tables with thousands of rows and make the Neo4j graph unreadable on
screen: useless for a teaching demo.

Why "just take 50 at random" is not enough either
-------------------------------------------------
A random sample of transactions breaks the spending chains: inputs would
almost always point to outputs outside the sample, so
- fund tracing (Q4) would have no path to follow,
- nearly every output would look "unspent" and Q2 would lose its meaning,
- clustering would find no co-occurrences.
The sample would be formally valid but say nothing.

Strategy used: CONNECTED sampling
---------------------------------
A genuinely connected subgraph is selected by following the real
spending chains between transactions. Concretely, the sample includes:
  1. one coinbase transaction, as a teaching case (an input with no
     predecessor: it shows at once why tx_inputs has no strict FK);
  2. the DEEPEST spending chain available in the window, in full, so
     that fund tracing (Q4) really has a multi-hop path to follow;
  3. transactions that open several branches, favouring multi-input ones
     (they create co-ownership: without them the Q7 clustering would
     find nothing to group).
The result therefore contains both spent and unspent outputs (Q2 makes
sense), walkable chains (Q4) and non-trivial clusters (Q7).

The data stays REAL BITCOIN: no invented values, no synthetic addresses.
We choose *which* transactions to keep, we do not alter their content.
"""
import argparse
import json
import sys
from collections import defaultdict

COINBASE_PREV = "0" * 64
COINBASE_INDEX = 4294967295


def is_coinbase(tx):
    return any(i["prev_txid"] == COINBASE_PREV and i["prev_index"] == COINBASE_INDEX
               for i in tx["inputs"])


def read_blocks(path, max_blocks):
    blocks = []
    with open(path) as f:
        for i, line in enumerate(f):
            if i >= max_blocks:
                break
            blocks.append(json.loads(line))
    return blocks


def sample_connected(blocks, target_tx, max_hops):
    """Selects a connected subset of transactions.

    Returns (set of selected txids, statistics). The exploration order is
    deterministic (no randomness): running the script twice on the same
    blocks produces the same sample, so the demo is reproducible and the
    prepared queries always return the same results.

    Seed choice (verified against real data, not assumed): coinbase
    transactions are NOT good seeds, because their outputs are never
    spent inside the same time window: the Bitcoin protocol enforces a
    100-block maturation before a coinbase can be spent. Starting there
    yields a sample of coinbases only, with no chain to follow
    (measured: 6 isolated transactions). So we start from transactions
    whose outputs are actually spent inside the window, favouring those
    that open several branches. One coinbase is still included
    separately, because it is the teaching case to show (an input with
    no predecessor)."""

    all_tx = {}                       # txid -> (block index, tx)
    spenders = defaultdict(list)      # spent txid -> [txids spending it]
    for bi, b in enumerate(blocks):
        for tx in b["transactions"]:
            all_tx[tx["txid"]] = (bi, tx)
    for txid, (bi, tx) in all_tx.items():
        for inp in tx["inputs"]:
            if inp["prev_txid"] in all_tx:
                spenders[inp["prev_txid"]].append(txid)

    selected = []
    seen = set()

    # 1) one coinbase as a teaching case (input with no predecessor)
    for txid, (bi, tx) in sorted(all_tx.items(), key=lambda kv: (kv[1][0], kv[0])):
        if is_coinbase(tx) and len(tx["outputs"]) <= 6:
            seen.add(txid); selected.append(txid)
            break

    # 2) real seeds: transactions with internal chains, preferring the
    #    "slim" ones (few outputs) so the graph stays readable, but that
    #    still open several branches to follow
    candidates = []
    for txid, sp in spenders.items():
        bi, tx = all_tx[txid]
        n_out = len(tx["outputs"])
        if n_out <= 8 and len(sp) >= 2:
            candidates.append((-len(sp), n_out, txid))
    candidates.sort()

    stats = {"hops": defaultdict(int), "seeds": 0, "deep_chain": 0}

    # 2b) one complete DEEP CHAIN: without it the sample would only
    #      have 1-hop jumps and fund tracing (Q4) would have nothing
    #      interesting to follow. We look for the longest chain
    #      available and include it in full.
    def chain_from(start, maxd):
        depth = {start: 0}
        order = [start]
        qi = 0
        while qi < len(order):
            t = order[qi]; qi += 1
            if depth[t] >= maxd:
                continue
            for n in spenders.get(t, []):
                if n not in depth:
                    depth[n] = depth[t] + 1
                    order.append(n)
        return depth, order

    best_chain = None
    for txid in sorted(spenders.keys())[:3000]:
        depth, order = chain_from(txid, max_hops)
        md = max(depth.values()) if depth else 0
        if best_chain is None or md > best_chain[0]:
            best_chain = (md, order, depth)
    if best_chain and best_chain[0] >= 3:
        for t in best_chain[1]:
            if t not in seen and len(selected) < target_tx:
                seen.add(t); selected.append(t)
                stats["deep_chain"] += 1
        stats["deep_depth"] = best_chain[0]

    # 2c) CO-OWNERSHIP for the clustering (Q7).
    #
    # Verified against the data, not assumed: taking "multi-input"
    # transactions is not enough. The most common multi-input
    # transactions consolidate several UTXOs of the *same* address (a
    # wallet gathering its own funds): the common-input-ownership
    # heuristic produces no edge in that case, because it needs
    # DIFFERENT addresses in the same transaction. An early sample held
    # 6 multi-input transactions and still generated 0 co-ownership
    # edges, leaving Q7 with nothing to show.
    #
    # So here we explicitly look for transactions whose inputs resolve
    # to at least 2 DISTINCT addresses inside the sample, preferring
    # small ones: a cluster of 3-4 addresses is readable at a glance, a
    # 111-address one is not.
    out_addr = {}
    for txid, (bi, tx) in all_tx.items():
        for o in tx["outputs"]:
            if o.get("address"):
                out_addr[(txid, o["index"])] = o["address"]

    coown = []
    for txid, (bi, tx) in all_tx.items():
        addrs, srcs = set(), set()
        for inp in tx["inputs"]:
            k = (inp["prev_txid"], inp["prev_index"])
            if k in out_addr:
                addrs.add(out_addr[k]); srcs.add(inp["prev_txid"])
        if 2 <= len(addrs) <= 5 and len(tx["outputs"]) <= 6:
            coown.append((len(addrs), len(tx["outputs"]), txid, sorted(srcs)))
    coown.sort(key=lambda c: (-c[0], c[1], c[2]))

    for n_addr, _, txid, srcs in coown:
        if stats.get("coown_tx", 0) >= 2:
            break
        # The source transactions must be included (without them the
        # join on inputs does not resolve and the co-ownership edge is
        # not formed), but only if they are "slim": a source with 100+
        # outputs would bloat the sample and make the graph unreadable.
        srcs_ok = [t for t in srcs if t in all_tx and len(all_tx[t][1]["outputs"]) <= 12]
        if len(srcs_ok) < 2:
            continue
        for t in [txid] + srcs_ok:
            if t not in seen and len(selected) < target_tx:
                seen.add(t); selected.append(t)
        stats["coown"] = stats.get("coown", 0) + n_addr
        stats["coown_tx"] = stats.get("coown_tx", 0) + 1

    frontier = []
    for _, _, txid in candidates:
        if len(selected) >= target_tx * 3 // 4:
            break
        if txid in seen:
            continue
        seen.add(txid); selected.append(txid)
        frontier.append((txid, 0))
        stats["seeds"] += 1

    # 3) forward BFS along the spending chains, favouring multi-input
    #    transactions (they create co-ownership, needed by Q7)
    qi = 0
    while qi < len(frontier) and len(selected) < target_tx:
        txid, hop = frontier[qi]
        qi += 1
        if hop >= max_hops:
            continue
        nxts = sorted(spenders.get(txid, []),
                      key=lambda t: (-len(all_tx[t][1]["inputs"]),
                                     len(all_tx[t][1]["outputs"]), t))
        for nxt in nxts:
            if nxt in seen or nxt not in all_tx:
                continue
            if len(all_tx[nxt][1]["outputs"]) > 10:
                continue          # skip transactions that are too wide
            seen.add(nxt); selected.append(nxt)
            frontier.append((nxt, hop + 1))
            stats["hops"][hop + 1] += 1
            if len(selected) >= target_tx:
                break

    return set(selected), all_tx, stats


def build_output_blocks(blocks, keep, all_tx):
    """Rebuilds the blocks keeping only the selected transactions.

    Blocks left with no transactions are kept anyway: they are needed to
    show the full PREV_BLOCK chain (Q6 on the main chain needs the
    blocks, not their transactions)."""
    out = []
    for b in blocks:
        kept = [tx for tx in b["transactions"] if tx["txid"] in keep]
        out.append({
            "hash": b["hash"],
            "prev_hash": b["prev_hash"],
            "timestamp": b["timestamp"],
            "transactions": kept,
        })
    return out


def report(out_blocks, keep, stats):
    n_tx = sum(len(b["transactions"]) for b in out_blocks)
    n_out = sum(len(t["outputs"]) for b in out_blocks for t in b["transactions"])
    n_in = sum(len(t["inputs"]) for b in out_blocks for t in b["transactions"])

    # how many inputs point to an output INSIDE the sample: a measure
    # of how connected the subgraph really is
    internal = 0
    for b in out_blocks:
        for t in b["transactions"]:
            for i in t["inputs"]:
                if i["prev_txid"] in keep:
                    internal += 1
    non_coinbase = sum(1 for b in out_blocks for t in b["transactions"]
                       for i in t["inputs"] if i["prev_txid"] != COINBASE_PREV)

    addrs = set()
    for b in out_blocks:
        for t in b["transactions"]:
            for o in t["outputs"]:
                if o.get("address"):
                    addrs.add(o["address"])

    multi_in = sum(1 for b in out_blocks for t in b["transactions"] if len(t["inputs"]) > 1)

    print(f"  blocks:           {len(out_blocks)}", file=sys.stderr)
    print(f"  transactions:     {n_tx}", file=sys.stderr)
    print(f"  outputs:          {n_out}", file=sys.stderr)
    print(f"  inputs:           {n_in}", file=sys.stderr)
    print(f"  unique addresses: {len(addrs)}", file=sys.stderr)
    print(f"  multi-input tx:   {multi_in}  (they create co-ownership for the clustering)", file=sys.stderr)
    if non_coinbase:
        pct = internal / non_coinbase * 100
        print(f"  connectedness:    {internal}/{non_coinbase} non-coinbase inputs "
              f"resolve inside the sample ({pct:.0f}%)", file=sys.stderr)
    if stats.get("coown"):
        print(f"  co-ownership:     {stats['coown']} distinct addresses joined by "
              f"multi-input transactions (for the Q7 clustering)", file=sys.stderr)
    if stats.get("deep_chain"):
        print(f"  deep chain:       {stats['deep_chain']} transactions, "
              f"{stats.get('deep_depth', 0)} hops (for fund tracing)", file=sys.stderr)
    if stats["hops"]:
        chain = ", ".join(f"hop {h}: {c}" for h, c in sorted(stats["hops"].items()))
        print(f"  other chains:     {chain}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Small, connected dataset for the live demo")
    ap.add_argument("--source", default="benchmark/subsets/subset_00050.jsonl",
                    help="Source JSONL (real blocks)")
    ap.add_argument("--blocks", type=int, default=6,
                    help="How many blocks to read from the source")
    ap.add_argument("--target-tx", type=int, default=45,
                    help="How many transactions to keep in the sample")
    ap.add_argument("--max-hops", type=int, default=6,
                    help="Maximum depth of the spending chains to follow")
    ap.add_argument("--out", default="demo/demo_dataset.jsonl")
    args = ap.parse_args()

    print(f"Reading {args.blocks} blocks from {args.source}…", file=sys.stderr)
    blocks = read_blocks(args.source, args.blocks)
    tot = sum(len(b["transactions"]) for b in blocks)
    print(f"  {tot} transactions available, {args.target_tx} needed", file=sys.stderr)

    keep, all_tx, stats = sample_connected(blocks, args.target_tx, args.max_hops)
    out_blocks = build_output_blocks(blocks, keep, all_tx)

    with open(args.out, "w") as f:
        for b in out_blocks:
            f.write(json.dumps(b) + "\n")

    print(f"\nDemo dataset written to {args.out}:", file=sys.stderr)
    report(out_blocks, keep, stats)


if __name__ == "__main__":
    main()
