"""
Extraction of the individual queries from the files queries/queries.sql and
queries/queries.cypher, so that they can be executed one at a time in the benchmark.
The queries are delimited by "QN:" markers inside comment blocks;
this module separates them keeping the association with the number.

Q4 (fund tracing) has a parameter, the starting address; Q7 (wallet
balance) requires the clustering to have already been executed. The
benchmark decides which ones to include based on the context (see
bench_queries.py).
"""
import re


def _parse_queries(path: str, comment_prefix: str) -> dict[str, str]:
    """Extracts the numbered queries from a file. Returns {"Q1": "...", ...}.
    comment_prefix is "--" for SQL, "//" for Cypher."""
    with open(path) as f:
        content = f.read()

    # Find the "QN:" headers in the comments and use their position to
    # delimit the blocks. Each query goes from the end of the "===="
    # comment block that follows its header, up to the next "====" block.
    lines = content.splitlines()
    queries: dict[str, list[str]] = {}
    current_q = None
    in_header = False

    for line in lines:
        stripped = line.strip()
        # "QN: ..." header inside a comment
        m = re.match(rf"{re.escape(comment_prefix)}\s*(Q\d+):", stripped)
        if m:
            current_q = m.group(1)
            queries[current_q] = []
            in_header = True
            continue
        # End of the header block (a line made only of "=")
        if in_header and stripped.startswith(comment_prefix) and set(stripped.replace(comment_prefix, "").strip()) == {"="}:
            in_header = False
            continue
        # Code lines of the current query (skip comments and empty lines)
        if current_q is not None and not in_header:
            if stripped.startswith(comment_prefix):
                # A new "====" block marks the start of the next query
                if set(stripped.replace(comment_prefix, "").strip()) == {"="}:
                    current_q = None
                continue
            if stripped:
                queries[current_q].append(line)

    # Joins the lines and removes the trailing ";" for SQL (sqlite3 handles it,
    # but for uniformity we strip it and add it back where needed).
    result = {}
    for q, body_lines in queries.items():
        body = "\n".join(body_lines).strip()
        if body.endswith(";"):
            body = body[:-1].strip()
        if body:
            result[q] = body
    return result


def load_sql_queries(path: str = "queries/queries.sql") -> dict[str, str]:
    return _parse_queries(path, "--")


def load_cypher_queries(path: str = "queries/queries.cypher") -> dict[str, str]:
    return _parse_queries(path, "//")


if __name__ == "__main__":
    import sys

    sql = load_sql_queries()
    cypher = load_cypher_queries()
    print(f"SQL queries extracted: {sorted(sql.keys())}", file=sys.stderr)
    print(f"Cypher queries extracted: {sorted(cypher.keys())}", file=sys.stderr)
    # Show an excerpt of each one for visual checking
    for q in sorted(sql.keys()):
        first_line = sql[q].splitlines()[0]
        print(f"  SQL {q}: {first_line[:70]}", file=sys.stderr)
    for q in sorted(cypher.keys()):
        first_line = cypher[q].splitlines()[0]
        print(f"  Cypher {q}: {first_line[:70]}", file=sys.stderr)
