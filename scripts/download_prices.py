"""
Downloads the BTC/USD price history (daily granularity) from a public API
(CoinGecko), as a second heterogeneous data source to reconcile with the
blocks/transactions dataset. Its daily granularity, coarser than the
per-second timestamps of the transactions, is what makes the reconciliation
a temporal join.
"""
import json
import sys
import time

import requests

API_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
MAX_RETRIES = 5


def fetch_daily_prices(days: int = 60) -> list[dict]:
    """Returns a list of {date_unix, price_usd}, one point per day.
    date_unix is UTC midnight of the day the price refers to
    (truncated from the millisecond timestamp returned by the API)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                API_URL,
                params={"vs_currency": "usd", "days": days, "interval": "daily"},
                timeout=30,
            )
            r.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            print(f"  retry {attempt}/{MAX_RETRIES} ({e}), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)

    data = r.json()["prices"]  # [[timestamp_ms, price], ...]
    return [{"date_unix": ts_ms // 1000, "price_usd": price} for ts_ms, price in data]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download the daily BTC/USD price history")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--out", default="prices.jsonl")
    args = parser.parse_args()

    prices = fetch_daily_prices(args.days)
    with open(args.out, "w") as f:
        for p in prices:
            f.write(json.dumps(p) + "\n")

    print(f"Downloaded {len(prices)} daily price points to {args.out}", file=sys.stderr)
