"""Tributary dashboard — live view of the tribe's shared memory.

Run locally:  uvicorn dashboard.app:app --reload
Deployed on AWS App Runner via dashboard/Dockerfile.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tributary import memory, runs
from tributary.db import run_txn

app = FastAPI(title="Tributary")


@app.get("/api/feed")
def feed(limit: int = 50):
    def txn(cur):
        cur.execute(
            """
            SELECT a.at, COALESCE(ag.name, 'system'), a.action,
                   a.lesson_id::STRING, a.detail
            FROM memory_audit a
            LEFT JOIN agents ag ON ag.id = a.agent_id
            ORDER BY a.at DESC LIMIT %s
            """,
            (limit,),
        )
        return [{"at": str(r[0]), "agent": r[1], "action": r[2],
                 "lesson_id": r[3], "detail": r[4]} for r in cur.fetchall()]
    return run_txn(txn)


@app.get("/api/lessons_as_of")
def lessons_as_of(ts: str):
    """Time-travel: the tribe's belief set at a past instant, via
    CockroachDB AS OF SYSTEM TIME."""
    try:
        found = memory.lessons_as_of(ts)
    except ValueError:
        return {"error": f"bad timestamp: {ts}"}
    return [{"id": l.id, "situation": l.situation, "content": l.content,
             "confidence": l.confidence, "times_helpful": l.times_helpful,
             "created_at": l.created_at} for l in found]


@app.get("/api/runs")
def api_runs():
    return runs.list_runs()


@app.get("/api/lessons")
def lessons(status: str = "active"):
    def txn(cur):
        cur.execute(
            """
            SELECT l.id::STRING, l.situation, l.content, ag.name, l.confidence,
                   l.times_recalled, l.times_helpful, l.status::STRING,
                   l.superseded_by::STRING, l.created_at
            FROM lessons l JOIN agents ag ON ag.id = l.agent_id
            WHERE %s = 'all' OR l.status::STRING = %s
            ORDER BY l.created_at DESC LIMIT 200
            """,
            (status, status),
        )
        return [{"id": r[0], "situation": r[1], "content": r[2], "learned_by": r[3],
                 "confidence": r[4], "times_recalled": r[5], "times_helpful": r[6],
                 "status": r[7], "superseded_by": r[8], "created_at": str(r[9])}
                for r in cur.fetchall()]
    return run_txn(txn)


@app.get("/api/stats")
def stats():
    def txn(cur):
        cur.execute(
            """
            SELECT (SELECT count(*) FROM lessons WHERE status = 'active'),
                   (SELECT count(*) FROM agents),
                   (SELECT count(*) FROM memory_audit WHERE action = 'recall'),
                   (SELECT count(*) FROM memory_audit WHERE action = 'supersede')
            """
        )
        r = cur.fetchone()
        return {"active_lessons": r[0], "agents": r[1],
                "recalls": r[2], "supersessions": r[3]}
    return run_txn(txn)


PAGE = """<!doctype html><html><head><title>Tributary</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font-family:ui-sans-serif,system-ui;margin:0;background:#0b1220;color:#dbe4f0}
  header{padding:20px 28px;border-bottom:1px solid #1e2a3f}
  h1{margin:0;font-size:20px} h1 span{color:#57b3ff}
  .sub{color:#7d8ca3;font-size:13px;margin-top:4px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:20px 28px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  .card{background:#101a2c;border:1px solid #1e2a3f;border-radius:10px;padding:16px}
  h2{font-size:14px;margin:0 0 12px;color:#9fb3cc;text-transform:uppercase;letter-spacing:.05em}
  .stats{display:flex;gap:24px;padding:16px 28px 0}
  .stat b{font-size:22px;color:#57b3ff;display:block}
  .stat{font-size:12px;color:#7d8ca3}
  .item{padding:8px 0;border-bottom:1px solid #17233a;font-size:13px}
  .item .who{color:#57b3ff} .item .act{color:#ffb454}
  .lesson .sit{color:#7d8ca3;font-size:12px}
  .badge{font-size:11px;padding:1px 7px;border-radius:9px;background:#1c2c48;color:#8fb8e8;margin-left:6px}
</style></head><body>
<header><h1>🌊 <span>Tributary</span> — the tribe's shared memory</h1>
<div class="sub">Backed by CockroachDB · lessons flow in from every agent, and never dry up</div></header>
<div class="stats" id="stats"></div>
<div class="grid">
  <div class="card"><h2>Live memory feed</h2><div id="feed"></div></div>
  <div class="card"><h2>Active lessons</h2><div id="lessons"></div></div>
  <div class="card"><h2>The species gets smarter — tokens per generation</h2>
    <svg id="curve" viewBox="0 0 400 150" style="width:100%"></svg></div>
  <div class="card"><h2>🕰️ Time travel — what did the tribe believe at…</h2>
    <input type="datetime-local" id="ts" step="1"
      style="background:#1c2c48;color:#dbe4f0;border:1px solid #2a3c5c;border-radius:6px;padding:6px">
    <button onclick="travel()"
      style="background:#57b3ff;color:#0b1220;border:0;border-radius:6px;padding:6px 14px;cursor:pointer;font-weight:600">
      Look</button>
    <div class="sub" style="margin-top:6px">Powered by CockroachDB AS OF SYSTEM TIME — no snapshots, just SQL.</div>
    <div id="past"></div></div>
</div>
<script>
function lessonHtml(l){
  return `<div class="item lesson"><div class=sit>When ${l.situation}</div>${l.content}`+
    `<span class=badge>conf ${l.confidence.toFixed(2)}</span></div>`;
}
async function travel(){
  const ts = document.getElementById('ts').value;
  if(!ts) return;
  const past = await fetch('/api/lessons_as_of?ts='+encodeURIComponent(ts)).then(r=>r.json());
  document.getElementById('past').innerHTML = past.error ? past.error :
    (past.length ? past.map(lessonHtml).join('') :
     '<div class=item>The tribe knew nothing yet.</div>');
}
async function drawCurve(){
  const rs = await fetch('/api/runs').then(r=>r.json());
  const gens = {};
  rs.filter(r=>r.generation).forEach(r=>{(gens[r.generation]=gens[r.generation]||[]).push(r.tokens)});
  const pts = Object.keys(gens).sort((a,b)=>a-b)
    .map(g=>[+g, gens[g].reduce((s,x)=>s+x,0)/gens[g].length]);
  if(pts.length<2){document.getElementById('curve').innerHTML=
    '<text x=10 y=30 fill="#7d8ca3" font-size=12>Run scripts/run_generations.py to draw the curve</text>';return;}
  const maxT=Math.max(...pts.map(p=>p[1])), maxG=Math.max(...pts.map(p=>p[0]));
  const X=g=>20+(g-1)*(360/Math.max(maxG-1,1)), Y=t=>135-(t/maxT)*115;
  const line=pts.map((p,i)=>`${i?'L':'M'}${X(p[0])},${Y(p[1])}`).join(' ');
  document.getElementById('curve').innerHTML =
    `<path d="${line}" fill="none" stroke="#57b3ff" stroke-width="2"/>`+
    pts.map(p=>`<circle cx=${X(p[0])} cy=${Y(p[1])} r=3 fill="#ffb454"/>`+
      `<text x=${X(p[0])-8} y=148 fill="#7d8ca3" font-size=9>g${p[0]}</text>`).join('');
}

async function tick(){
  const [feed, lessons, stats] = await Promise.all([
    fetch('/api/feed').then(r=>r.json()),
    fetch('/api/lessons').then(r=>r.json()),
    fetch('/api/stats').then(r=>r.json())]);
  document.getElementById('stats').innerHTML =
    `<div class=stat><b>${stats.active_lessons}</b>active lessons</div>`+
    `<div class=stat><b>${stats.agents}</b>agents</div>`+
    `<div class=stat><b>${stats.recalls}</b>recalls</div>`+
    `<div class=stat><b>${stats.supersessions}</b>conflicts resolved</div>`;
  document.getElementById('feed').innerHTML = feed.map(f=>
    `<div class=item><span class=who>${f.agent}</span> <span class=act>${f.action}</span> `+
    `${f.detail||''} <span class=sub>${f.at.slice(11,19)}</span></div>`).join('');
  document.getElementById('lessons').innerHTML = lessons.map(l=>
    `<div class="item lesson"><div class=sit>When ${l.situation}</div>${l.content}`+
    `<span class=badge>by ${l.learned_by}</span><span class=badge>conf ${l.confidence.toFixed(2)}</span>`+
    `<span class=badge>helped ${l.times_helpful}x</span></div>`).join('');
}
tick(); drawCurve(); setInterval(tick, 3000); setInterval(drawCurve, 10000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
