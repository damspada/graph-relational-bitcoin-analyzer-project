"""
Loads the BTC/USD price history (scripts/download_prices.py) into SQLite and
Neo4j. Unlike the other loaders, this one does not translate blockchain data:
it populates an independent table/node, which the queries connect to the
transaction timestamps by interval, not with a pre-built edge/FK.
"""
import json
import sqlite3
import sys


def read_prices(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_sqlite(jsonl_path: str, db_path: str) -> None:
    prices = read_prices(jsonl_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT OR IGNORE INTO price_history (date_unix, price_usd) VALUES (?, ?)",
        [(p["date_unix"], p["price_usd"]) for p in prices],
    )
    conn.commit()
    conn.close()
    print(f"Loaded {len(prices)} price points into SQLite", file=sys.stderr)


def load_neo4j(jsonl_path: str, uri: str, user: str, password: str) -> None:
    from neo4j import GraphDatabase

    prices = read_prices(jsonl_path)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    with driver.session() as session:
        session.run(
            """
            UNWIND $rows AS row
            MERGE (p:PriceSnapshot {dateUnix: row.date_unix})
            SET p.priceUsd = row.price_usd
            """,
            rows=prices,
        )
    driver.close()
    print(f"Loaded {len(prices)} price points into Neo4j", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Load BTC/USD price history")
    parser.add_argument("jsonl", help="Path to the prices file (scripts/download_prices.py)")
    parser.add_argument("--target", choices=["sqlite", "neo4j"], required=True)
    parser.add_argument("--db", help="SQLite DB path (required with --target sqlite)")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="secretgraph")
    args = parser.parse_args()

    if args.target == "sqlite":
        if not args.db:
            parser.error("--db is required with --target sqlite")
        load_sqlite(args.jsonl, args.db)
    else:
        load_neo4j(args.jsonl, args.uri, args.user, args.password)
