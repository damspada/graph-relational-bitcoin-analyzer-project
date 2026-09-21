"""
Local web interface to explore the SQLite database of the demo.

It is used to show live, during the presentation:
  - the tables of the schema with the real data inside,
  - the Q1-Q8 queries of the project, runnable with one click,
  - a free field to write SQL on the fly and answer questions.

On the relational side it is the equivalent of what Neo4j Browser
already offers for the graph (localhost:7474), so the two models are
shown symmetrically.

Security: it accepts ONLY read queries (SELECT / WITH / EXPLAIN) and
opens the database read-only, so a query typed by mistake cannot modify
the demo data. It listens on 127.0.0.1: it is not reachable from the
network.

Usage:
    python3 demo/serve_sqlite.py --db demo/demo.sqlite
    (then open http://127.0.0.1:8000)
"""
import argparse
import html
import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DB_PATH = None
QUERIES = {}

# Read-only: anything else is rejected before touching the database.
READ_ONLY = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|pragma)\b",
                       re.IGNORECASE)


def connect():
    """Opens the database read-only (URI mode)."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def load_project_queries():
    """Loads Q1-Q8 from queries/queries.sql, so the demo shows exactly
    the queries of the project and not copies that could drift apart
    over time.

    The parsing is done line by line instead of with a single regular
    expression: the headers in the file span several comment lines (some
    queries have design notes dozens of lines long between the title and
    the code), and a "title then body" regex only captured 3 out of
    8."""
    path = Path(__file__).parent.parent / "queries" / "queries.sql"
    if not path.exists():
        return {}

    header = re.compile(r"^--\s*(Q\d)\s*:\s*(.*)$")
    out, qid, title_parts, sql_lines = {}, None, [], []

    def flush():
        if qid and sql_lines:
            sql = "\n".join(sql_lines).strip().rstrip(";")
            if sql:
                title = " ".join(" ".join(title_parts).split())
                out[qid] = {"title": title, "sql": sql}

    for line in path.read_text().splitlines():
        m = header.match(line)
        if m:
            flush()
            qid, title_parts, sql_lines = m.group(1), [m.group(2).strip()], []
            continue
        if qid is None:
            continue
        stripped = line.strip()
        if stripped.startswith("--"):
            # comment lines: until the code starts, they are still part
            # of the title/description (excluding the '=' rule lines)
            if not sql_lines and set(stripped.replace("-", "").strip()) not in ({"="}, set()):
                title_parts.append(stripped.lstrip("- ").strip())
            continue
        if stripped:
            sql_lines.append(line)
    flush()

    # the full title can be very long: the first sentence is enough for
    # the interface
    for q in out:
        t = out[q]["title"]
        cut = t.find(". ")
        out[q]["title"] = (t[:cut] if 0 < cut < 160 else t[:160]).strip()

    # Q4 is parametric (:start_address). In the project file the
    # parameter is supplied by the benchmark Python code; here the query
    # is run as it is, so without substitution it would fail with
    # "Incorrect number of bindings supplied". We insert a real address
    # from the dataset: the one with the most actually spent outputs,
    # that is the one producing the most interesting chain to follow
    # live, while leaving it editable by hand in the editor.
    start = pick_tracing_address()
    if start:
        for q in out:
            if ":start_address" in out[q]["sql"]:
                out[q]["sql"] = out[q]["sql"].replace(":start_address", f"'{start}'")
    return out


def pick_tracing_address():
    """The address in the dataset with the most spent outputs: it is
    the starting point that makes fund tracing visible."""
    try:
        conn = connect()
        row = conn.execute("""
            SELECT o.address
            FROM tx_outputs o
            JOIN tx_inputs i ON i.prev_txid = o.txid
                            AND i.prev_output_index = o.output_index
            WHERE o.address IS NOT NULL
            GROUP BY o.address
            ORDER BY COUNT(DISTINCT i.txid) DESC, o.address
            LIMIT 1
        """).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def table_info(conn):
    tables = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        cols = [(r[1], r[2], bool(r[5])) for r in conn.execute(f"PRAGMA table_info({name})")]
        n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        tables.append({"name": name, "columns": cols, "rows": n})
    return tables


def run_query(sql, limit=200):
    if not READ_ONLY.match(sql) or FORBIDDEN.search(sql):
        return {"error": "Only read queries are allowed (SELECT / WITH / EXPLAIN)."}
    conn = connect()
    try:
        t0 = time.perf_counter()
        cur = conn.execute(sql)
        rows = cur.fetchmany(limit)
        elapsed = time.perf_counter() - t0
        cols = [d[0] for d in cur.description] if cur.description else []
        more = len(cur.fetchmany(1)) > 0
        return {"columns": cols,
                "rows": [list(r) for r in rows],
                "elapsed_ms": elapsed * 1000,
                "truncated": more,
                "limit": limit}
    except sqlite3.Error as e:
        return {"error": f"SQL error: {e}"}
    finally:
        conn.close()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NodeCoin: SQLite</title>
<style>
 :root{
   --bg:#0d1210; --surface:#161d1a; --surface2:#1c2420; --border:#263029;
   --text:#e8ece9; --dim:#a8b4ac; --faint:#6f7d75; --sql:#d99a4e;
   --sqlbg:rgba(217,154,78,.10); --graph:#4ecdb0; --bad:#c1594a;
 }
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);
      font:14px/1.5 'IBM Plex Sans',system-ui,sans-serif}
 code,pre,.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
 header{padding:18px 26px;border-bottom:1px solid var(--border);
        display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
 header h1{margin:0;font-size:19px;font-weight:600}
 header .sub{color:var(--faint);font-size:12.5px}
 .wrap{display:grid;grid-template-columns:270px minmax(0,1fr);min-height:calc(100vh - 59px)}
 .side{border-right:1px solid var(--border);padding:18px 14px;overflow-y:auto;max-height:calc(100vh - 59px)}
 .side h2{font-size:11px;letter-spacing:.07em;text-transform:uppercase;
          color:var(--faint);margin:0 0 10px;font-weight:600}
 .tbl{margin-bottom:8px;border:1px solid var(--border);border-radius:8px;overflow:hidden}
 .tbl-head{padding:8px 11px;background:var(--sqlbg);color:var(--sql);
           font-family:'JetBrains Mono',monospace;font-size:12.5px;font-weight:700;
           cursor:pointer;display:flex;justify-content:space-between;align-items:center}
 .tbl-head .cnt{color:var(--faint);font-weight:400;font-size:11px}
 .cols{display:none;padding:6px 11px 9px;font-size:11.5px}
 .cols.open{display:block}
 .cols div{display:flex;justify-content:space-between;padding:2px 0;
           font-family:'JetBrains Mono',monospace;color:var(--dim)}
 .cols .pk{color:var(--sql)}
 .cols .ty{color:var(--faint)}
 .main{padding:20px 26px 60px;min-width:0}
 .qbar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
 .qb{font-family:'JetBrains Mono',monospace;font-size:12px;padding:6px 11px;
     border-radius:999px;border:1px solid var(--border);background:var(--surface);
     color:var(--dim);cursor:pointer}
 .qb:hover{border-color:var(--sql);color:var(--text)}
 .qb.act{border-color:var(--sql);color:var(--sql);background:var(--sqlbg)}
 textarea{width:100%;min-height:150px;background:var(--surface);color:var(--text);
          border:1px solid var(--border);border-radius:9px;padding:13px;
          font-family:'JetBrains Mono',monospace;font-size:12.5px;line-height:1.6;resize:vertical}
 textarea:focus{outline:none;border-color:var(--sql)}
 .run{margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 button.go{background:var(--sql);color:#17130c;border:none;border-radius:7px;
           padding:9px 20px;font-weight:600;cursor:pointer;font-size:13px}
 button.go:disabled{opacity:.5;cursor:default}
 .hint{color:var(--faint);font-size:11.5px}
 .qtitle{color:var(--dim);font-size:13px;margin:0 0 10px}
 .res{margin-top:18px}
 .meta{color:var(--faint);font-size:11.5px;margin-bottom:8px;
       font-family:'JetBrains Mono',monospace}
 .err{background:rgba(193,89,74,.12);border:1px solid var(--bad);color:var(--bad);
      border-radius:8px;padding:12px 14px;font-size:13px}
 .scroll{overflow-x:auto;border:1px solid var(--border);border-radius:9px}
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th{position:sticky;top:0;text-align:left;padding:9px 12px;background:var(--surface2);
    color:var(--faint);font-family:'JetBrains Mono',monospace;font-size:11px;
    text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border);
    white-space:nowrap}
 td{padding:7px 12px;border-bottom:1px solid var(--border);
    font-family:'JetBrains Mono',monospace;color:var(--dim);white-space:nowrap}
 tr:last-child td{border-bottom:none}
 tr:hover td{background:var(--surface)}
 td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--text)}
 td.null{color:var(--faint);font-style:italic}
 @media(max-width:860px){.wrap{grid-template-columns:1fr}.side{max-height:none}}
</style></head><body>
<header>
  <h1>NodeCoin · SQLite</h1>
  <span class="sub">__DBNAME__: demo dataset, real Bitcoin data</span>
</header>
<div class="wrap">
  <aside class="side">
    <h2>Tables</h2>
    <div id="tables"></div>
  </aside>
  <main class="main">
    <h2 style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);margin:0 0 10px;font-weight:600">Project queries</h2>
    <div class="qbar" id="qbar"></div>
    <p class="qtitle" id="qtitle">Write a query, or pick one above.</p>
    <textarea id="sql" spellcheck="false">SELECT * FROM blocks ORDER BY timestamp;</textarea>
    <div class="run">
      <button class="go" id="go">Run</button>
      <span class="hint">Ctrl/Cmd + Enter · read queries only · max 200 rows</span>
    </div>
    <div class="res" id="res"></div>
  </main>
</div>
<script>
let TABLES=[], QUERIES={};

function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

function drawTables(){
  document.getElementById('tables').innerHTML = TABLES.map((t,i)=>`
    <div class="tbl">
      <div class="tbl-head" onclick="document.getElementById('c${i}').classList.toggle('open')">
        <span>${esc(t.name)}</span><span class="cnt">${t.rows}</span>
      </div>
      <div class="cols" id="c${i}">
        ${t.columns.map(c=>`<div><span class="${c[2]?'pk':''}">${esc(c[0])}</span><span class="ty">${esc(c[1])}</span></div>`).join('')}
        <div style="margin-top:7px"><a href="#" onclick="peek('${esc(t.name)}');return false;"
           style="color:var(--sql);font-size:11px;text-decoration:none">see the data →</a></div>
      </div>
    </div>`).join('');
}
function peek(name){
  document.getElementById('sql').value = `SELECT * FROM ${name} LIMIT 50;`;
  document.getElementById('qtitle').textContent = `Contents of table ${name}`;
  document.querySelectorAll('.qb').forEach(b=>b.classList.remove('act'));
  run();
}
function drawQueries(){
  const bar=document.getElementById('qbar');
  bar.innerHTML = Object.keys(QUERIES).sort().map(q=>
    `<button class="qb" data-q="${q}">${q}</button>`).join('');
  bar.querySelectorAll('.qb').forEach(b=>b.addEventListener('click',()=>{
    const q=QUERIES[b.dataset.q];
    document.querySelectorAll('.qb').forEach(x=>x.classList.remove('act'));
    b.classList.add('act');
    document.getElementById('sql').value=q.sql+';';
    document.getElementById('qtitle').textContent=b.dataset.q+': '+q.title;
    run();
  }));
}
async function run(){
  const btn=document.getElementById('go'), res=document.getElementById('res');
  const sql=document.getElementById('sql').value;
  btn.disabled=true; res.innerHTML='<p class="meta">running…</p>';
  try{
    const r=await fetch('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},
                                     body:JSON.stringify({sql})});
    const d=await r.json();
    if(d.error){res.innerHTML=`<div class="err">${esc(d.error)}</div>`;}
    else if(!d.rows.length){res.innerHTML='<p class="meta">no rows returned</p>';}
    else{
      res.innerHTML=`<p class="meta">${d.rows.length} rows${d.truncated?' (truncated to '+d.limit+')':''} · ${d.elapsed_ms.toFixed(1)} ms</p>
      <div class="scroll"><table><thead><tr>${d.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${d.rows.map(row=>'<tr>'+row.map(v=>{
        if(v===null) return '<td class="null">NULL</td>';
        if(typeof v==='number') return `<td class="num">${v.toLocaleString('en-US')}</td>`;
        const s=String(v);
        return `<td title="${esc(s)}">${esc(s.length>34?s.slice(0,31)+'…':s)}</td>`;
      }).join('')+'</tr>').join('')}</tbody></table></div>`;
    }
  }catch(e){res.innerHTML=`<div class="err">${esc(e.message)}</div>`;}
  btn.disabled=false;
}
document.getElementById('go').addEventListener('click',run);
document.getElementById('sql').addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();run();}
});
(async()=>{
  const d=await (await fetch('/api/schema')).json();
  TABLES=d.tables; QUERIES=d.queries;
  drawTables(); drawQueries(); run();
})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                       # no noise in the terminal during the demo

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.replace("__DBNAME__", html.escape(Path(DB_PATH).name)),
                       "text/html")
        elif path == "/api/schema":
            conn = connect()
            try:
                self._send(200, json.dumps({"tables": table_info(conn), "queries": QUERIES}))
            finally:
                conn.close()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path != "/api/query":
            self._send(404, json.dumps({"error": "not found"}))
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._send(400, json.dumps({"error": "JSON non valido"}))
            return
        self._send(200, json.dumps(run_query(payload.get("sql", "")), default=str))


def main():
    global DB_PATH, QUERIES
    ap = argparse.ArgumentParser(description="Web explorer for the SQLite database of the demo")
    ap.add_argument("--db", default="demo/demo.sqlite")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    DB_PATH = str(Path(args.db).resolve())
    if not Path(DB_PATH).exists():
        raise SystemExit(f"Database non trovato: {DB_PATH}")
    QUERIES = load_project_queries()

    conn = connect()
    tables = table_info(conn)
    conn.close()

    print(f"Database:  {DB_PATH}")
    print(f"Tables:    " + ", ".join(f"{t['name']}({t['rows']})" for t in tables))
    print(f"Queries:   {', '.join(sorted(QUERIES)) or 'none found'}")
    print(f"\n  →  http://127.0.0.1:{args.port}\n")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
