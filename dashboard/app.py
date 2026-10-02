"""Tributary dashboard — live view of the tribe's shared memory.

Run locally:  uvicorn dashboard.app:app --reload
Deployed on AWS App Runner via dashboard/Dockerfile.

Read-only unless DASHBOARD_CURATOR_TOKEN is set. With it, the curator panels
(disputes, mistake reports, the eval review queue) accept actions from
requests carrying that token in an X-Curator-Token header, acting as the
curator agent DASHBOARD_CURATOR_NAME. Two things deliberately stay out of the
dashboard: releasing a quarantined lesson re-learns it, which needs the
embedding model and a classifier this image doesn't have; and accepting a
golden candidate writes git-tracked eval files and checks the CI baseline,
which belongs in a checkout (`python -m evals.review`).

Lesson text is untrusted (any agent can write it), so the page escapes every
value it renders; it never interpolates data into HTML unescaped.
"""

import hmac
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from tributary import golden, log, memory, runs
from tributary.db import run_readonly, run_txn


@asynccontextmanager
async def _lifespan(app):
    # At startup, not import: importing the app (e.g. in tests) shouldn't
    # reconfigure logging for the whole process.
    log.setup()
    yield


app = FastAPI(title="Tributary", lifespan=_lifespan)


@app.get("/api/feed")
def feed(limit: int = 50):
    def txn(cur):
        cur.execute(
            """
            SELECT a.at, COALESCE(ag.name, 'system'), a.action,
                   a.lesson_id::TEXT, a.detail
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
    """Time-travel: the tribe's belief set at a past instant, from each
    lesson's [activated_at, deactivated_at) validity interval."""
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
            SELECT l.id::TEXT, l.situation, l.content, ag.name, l.confidence,
                   l.times_recalled, l.times_helpful, l.status::TEXT,
                   l.superseded_by::TEXT, l.created_at
            FROM lessons l JOIN agents ag ON ag.id = l.agent_id
            WHERE %s = 'all' OR l.status::TEXT = %s
            ORDER BY l.created_at DESC LIMIT 200
            """,
            (status, status),
        )
        return [{"id": r[0], "situation": r[1], "content": r[2], "learned_by": r[3],
                 "confidence": r[4], "times_recalled": r[5], "times_helpful": r[6],
                 "status": r[7], "superseded_by": r[8], "created_at": str(r[9])}
                for r in cur.fetchall()]
    return run_txn(txn)


def _pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))]


@app.get("/api/costs")
def costs_api():
    """Cost/latency rollup from llm_calls, plus the model-tiering split."""
    def txn(cur):
        cur.execute("SELECT count(*), COALESCE(sum(cost_usd),0), "
                    "COALESCE(sum(escalated::INT),0) FROM llm_calls")
        n, total_cost, escalations = cur.fetchone()
        cur.execute("SELECT model, count(*), COALESCE(sum(cost_usd),0), "
                    "COALESCE(avg(ms),0) FROM llm_calls GROUP BY model ORDER BY 3 DESC")
        by_model = [{"model": r[0], "calls": int(r[1]), "cost_usd": round(float(r[2]), 4),
                     "avg_ms": int(r[3])} for r in cur.fetchall()]
        cur.execute("SELECT purpose, count(*), COALESCE(sum(cost_usd),0) "
                    "FROM llm_calls GROUP BY purpose ORDER BY 2 DESC")
        by_purpose = [{"purpose": r[0], "calls": int(r[1]), "cost_usd": round(float(r[2]), 4)}
                      for r in cur.fetchall()]
        cur.execute("SELECT ms FROM llm_calls ORDER BY at DESC LIMIT 1000")
        ms = [int(r[0]) for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM llm_calls WHERE purpose LIKE 'classify%'")
        classify_calls = cur.fetchone()[0]
        return {
            "calls": int(n), "total_cost_usd": round(float(total_cost), 4),
            "escalations": int(escalations),
            "escalation_rate": round(float(escalations) / int(classify_calls), 3) if classify_calls else 0.0,
            "p50_ms": _pct(ms, 50), "p95_ms": _pct(ms, 95),
            "by_model": by_model, "by_purpose": by_purpose,
        }
    return run_txn(txn)


@app.get("/api/eval_history")
def eval_history(suite: str = "classification"):
    """Metric-over-time from the eval harness — the demo's 'moving metric'."""
    def txn(cur):
        cur.execute(
            "SELECT git_sha, tier, metrics, at FROM eval_results "
            "WHERE suite = %s ORDER BY at ASC LIMIT 200",
            (suite,),
        )
        return [{"git_sha": r[0], "tier": r[1], "metrics": r[2], "at": str(r[3])}
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


# ---------------------------------------------------------------- curator ---

def _curator(x_curator_token: str | None = Header(default=None)) -> str:
    """The curator agent id, if the request carries the configured token.

    Read from the environment per request, so the gate can't be bypassed by
    import order and tests can toggle it. Constant-time comparison.
    """
    expected = os.environ.get("DASHBOARD_CURATOR_TOKEN", "")
    if not expected:
        raise HTTPException(403, "curator actions are disabled on this dashboard "
                                 "(set DASHBOARD_CURATOR_TOKEN on the server)")
    if not x_curator_token or not hmac.compare_digest(x_curator_token.encode(),
                                                      expected.encode()):
        raise HTTPException(403, "wrong or missing curator token")
    # ensure_agent upserts the role: pick a name no writer agent uses.
    return memory.ensure_agent(os.environ.get("DASHBOARD_CURATOR_NAME", "dashboard-curator"),
                               role="curator")


def _refused(e: Exception) -> JSONResponse:
    status = 403 if isinstance(e, memory.PrivilegeError) else 400
    return JSONResponse({"error": str(e)}, status_code=status)


@app.get("/api/curator")
def curator_status():
    return {"enabled": bool(os.environ.get("DASHBOARD_CURATOR_TOKEN")),
            "curator": os.environ.get("DASHBOARD_CURATOR_NAME", "dashboard-curator")}


@app.get("/api/disputes")
def disputes_api():
    return memory.disputes(limit=100)


class Resolution(BaseModel):
    accept: bool
    note: str = ""


@app.post("/api/disputes/{lesson_id}/resolve")
def resolve_dispute_api(lesson_id: str, body: Resolution, curator: str = Depends(_curator)):
    try:
        out = memory.resolve_dispute(lesson_id, curator, body.accept, note=body.note)
    except (memory.PrivilegeError, ValueError) as e:
        return _refused(e)
    return {"action": out["action"], "lesson": out["lesson"].id,
            "superseded": out["superseded"]}


@app.get("/api/quarantine")
def quarantine_api(limit: int = 50):
    """Quarantined lessons with the screen's reasons (from the audit line)."""
    rows = run_readonly(
        """
        SELECT l.id::TEXT, l.situation, l.content, ag.name, l.created_at,
               (SELECT a.detail FROM memory_audit a
                WHERE a.lesson_id = l.id AND a.action = 'quarantine'
                ORDER BY a.at DESC LIMIT 1)
        FROM lessons l JOIN agents ag ON ag.id = l.agent_id
        WHERE l.status = 'quarantined'
        ORDER BY l.created_at DESC LIMIT %s
        """, (min(limit, 200),))
    out = []
    for r in rows:
        detail = (r[5] or "").removeprefix("injection screen: ")
        out.append({"id": r[0], "situation": r[1], "content": r[2], "by": r[3],
                    "created_at": str(r[4]),
                    "reasons": [x for x in detail.split(",") if x]})
    return out


@app.get("/api/decisions")
def decisions_api(limit: int = 30):
    """Recent learn() decisions: the new lesson, what it was compared with,
    and the verdict. Candidates are snapshots, so the list can be long."""
    rows = run_readonly(
        """
        SELECT d.id::TEXT, d.at, d.op, COALESCE(ag.name, '?'), d.situation, d.content,
               d.candidates, d.verdict, d.action, d.lesson_id::TEXT
        FROM decisions d LEFT JOIN agents ag ON ag.id = d.agent_id
        ORDER BY d.at DESC LIMIT %s
        """, (min(limit, 100),))
    return [{"id": r[0], "at": str(r[1]), "op": r[2], "agent": r[3], "situation": r[4],
             "content": r[5], "candidates": r[6], "verdict": r[7], "action": r[8],
             "lesson_id": r[9]} for r in rows]


class Report(BaseModel):
    relation: str
    target_id: str | None = None
    note: str = ""


@app.post("/api/decisions/{decision_id}/report")
def report_api(decision_id: str, body: Report, curator: str = Depends(_curator)):
    try:
        out = memory.report_mistake(decision_id, body.relation, body.target_id or None,
                                    note=body.note, reporter_id=curator)
    except (memory.PrivilegeError, ValueError) as e:
        return _refused(e)
    return out


@app.get("/api/golden")
def golden_api():
    return golden.pending()


@app.post("/api/golden/{candidate_id}/reject")
def reject_candidate_api(candidate_id: str, curator: str = Depends(_curator)):
    c = golden.get(candidate_id)
    if c is None or c["status"] != "pending":
        return _refused(ValueError("no pending candidate with that id"))
    golden.mark(candidate_id, "rejected")
    return {"rejected": candidate_id}


PAGE = """<!doctype html><html><head><title>Tributary</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font-family:ui-sans-serif,system-ui;margin:0;background:#0b1220;color:#dbe4f0}
  header{padding:20px 28px;border-bottom:1px solid #1e2a3f}
  h1{margin:0;font-size:20px} h1 span{color:#57b3ff}
  .sub{color:#7d8ca3;font-size:13px;margin-top:4px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:20px 28px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  .card{background:#101a2c;border:1px solid #1e2a3f;border-radius:10px;padding:16px;min-width:0}
  h2{font-size:14px;margin:0 0 12px;color:#9fb3cc;text-transform:uppercase;letter-spacing:.05em}
  h2 .count{color:#ffb454;margin-left:6px}
  .stats{display:flex;flex-wrap:wrap;gap:24px;padding:16px 28px 0}
  .stat b{font-size:22px;color:#57b3ff;display:block}
  .stat{font-size:12px;color:#7d8ca3}
  .item{padding:8px 0;border-bottom:1px solid #17233a;font-size:13px;overflow-wrap:anywhere}
  .item .who{color:#57b3ff} .item .act{color:#ffb454}
  .lesson .sit,.muted{color:#7d8ca3;font-size:12px}
  .badge{font-size:11px;padding:1px 7px;border-radius:9px;background:#1c2c48;color:#8fb8e8;margin-left:6px;white-space:nowrap}
  .badge.warn{background:#3a2a12;color:#ffb454} .badge.bad{background:#3a1620;color:#ff8a9a}
  .badge.good{background:#12321f;color:#6ee7a0}
  .side{border-left:2px solid #2a3c5c;padding:4px 0 4px 10px;margin:6px 0}
  .side.new{border-color:#57b3ff} .side.old{border-color:#ffb454}
  button.btn{background:#57b3ff;color:#0b1220;border:0;border-radius:6px;padding:5px 12px;cursor:pointer;font-weight:600;font-size:12px}
  button.btn.alt{background:#1c2c48;color:#dbe4f0}
  button.btn:disabled{opacity:.4;cursor:not-allowed}
  input.f,select.f{background:#1c2c48;color:#dbe4f0;border:1px solid #2a3c5c;border-radius:6px;padding:5px 8px;font-size:12px;max-width:100%}
  .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
  .msg{font-size:12px;margin-top:6px;min-height:1em} .msg.err{color:#ff8a9a} .msg.ok{color:#6ee7a0}
  code{background:#0b1220;border:1px solid #1e2a3f;border-radius:4px;padding:1px 5px;font-size:12px}
  details summary{cursor:pointer}
  #curatorbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 28px 0;font-size:12px;color:#7d8ca3}
</style></head><body>
<header><h1>🌊 <span>Tributary</span> — the tribe's shared memory</h1>
<div class="sub">Backed by PostgreSQL + pgvector · lessons flow in from every agent, and never dry up</div></header>
<div class="stats" id="stats"></div>
<div id="curatorbar"></div>
<div class="grid">
  <div class="card"><h2>Live memory feed</h2><div id="feed"></div></div>
  <div class="card"><h2>Active lessons</h2><div id="lessons"></div></div>
  <div class="card"><h2>⚖️ Open disputes<span class=count id="n-disputes"></span></h2>
    <div class="sub" style="margin-bottom:6px">A writer contradicted another agent's lesson. Accept supersedes the original; reject keeps it.</div>
    <div id="disputes"></div><div class=msg id="msg-disputes"></div></div>
  <div class="card"><h2>🧭 Classification decisions</h2>
    <div class="sub" style="margin-bottom:6px">Every learn(): what it was compared with, and the verdict. Report a wrong one to queue it as an eval case.</div>
    <div id="decisions"></div><div class=msg id="msg-decisions"></div></div>
  <div class="card"><h2>🧪 Eval review queue<span class=count id="n-golden"></span></h2>
    <div class="sub" style="margin-bottom:6px">Likely mistakes waiting to become golden eval rows. Accept from a checkout (it writes eval files); reject here or there.</div>
    <div id="golden"></div><div class=msg id="msg-golden"></div></div>
  <div class="card"><h2>🚧 Quarantine<span class=count id="n-quarantine"></span></h2>
    <div class="sub" style="margin-bottom:6px">Lessons the injection screen held back. To release one (it is re-learned, so it needs the embedding model): <code>tribal_release</code> from a curator MCP session, or <code>memory.release</code> from the venv.</div>
    <div id="quarantine"></div></div>
  <div class="card"><h2>The species gets smarter — tokens per generation</h2>
    <svg id="curve" viewBox="0 0 400 150" style="width:100%"></svg></div>
  <div class="card"><h2>🕰️ Time travel — what did the tribe believe at…</h2>
    <input type="datetime-local" id="ts" step="1" class=f>
    <button class=btn onclick="travel()">Look</button>
    <div class="sub" style="margin-top:6px">Every lesson carries its validity interval, so the past is one WHERE clause away.</div>
    <div id="past"></div></div>
  <div class="card"><h2>💸 Cost &amp; latency — model tiering</h2>
    <div id="costs"><div class=sub>No LLM calls logged yet (run online, not offline).</div></div></div>
  <div class="card"><h2>📈 Classification accuracy over commits (eval harness)</h2>
    <svg id="evalcurve" viewBox="0 0 400 150" style="width:100%"></svg>
    <div class="sub">The metric that moves — each point is one eval run recorded to Postgres.</div></div>
</div>
<script>
// Everything rendered below comes from the database, and lesson text is
// written by agents: untrusted. Every value goes through esc(); ids for
// actions travel in data-* attributes, never inside inline handlers.
function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
const num = (x, d=2) => (typeof x === 'number' ? x.toFixed(d) : '?');

// ------------------------------------------------------------- curator ---
let curator = {enabled:false, curator:''};
function token(){ try { return sessionStorage.getItem('curatorToken') || ''; } catch(e){ return ''; } }
function setToken(v){ try { v ? sessionStorage.setItem('curatorToken', v) : sessionStorage.removeItem('curatorToken'); } catch(e){} }
const canAct = () => curator.enabled && !!token();
function renderCuratorBar(){
  const bar = document.getElementById('curatorbar');
  if(!curator.enabled){
    bar.innerHTML = '<span>Read-only dashboard. Curator actions are off (the server has no DASHBOARD_CURATOR_TOKEN).</span>';
    return;
  }
  bar.innerHTML = token()
    ? `<span class="badge good">curator: ${esc(curator.curator)}</span><button class="btn alt" id="logout">Forget token</button>`
    : `<span>Curator actions available.</span><input class=f type=password id="tok" placeholder="curator token" autocomplete="off"><button class=btn id="login">Use token</button>`;
  const login = document.getElementById('login'), logout = document.getElementById('logout');
  if(login) login.onclick = () => { setToken(document.getElementById('tok').value.trim()); renderCuratorBar(); refreshPanels(true); };
  if(logout) logout.onclick = () => { setToken(''); renderCuratorBar(); refreshPanels(true); };
}
async function act(url, body, msgId){
  const msg = document.getElementById(msgId);
  msg.className = 'msg'; msg.textContent = 'Working…';
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-Curator-Token':token()},
                                body: JSON.stringify(body || {})});
    const j = await r.json().catch(() => ({}));
    if(!r.ok){ msg.className = 'msg err'; msg.textContent = j.error || j.detail || ('HTTP ' + r.status); return null; }
    msg.className = 'msg ok'; return j;
  } catch(e){ msg.className = 'msg err'; msg.textContent = String(e); return null; }
}

// --------------------------------------------------------------- panels ---
function busy(el){  // don't re-render under the user's cursor or an open row
  return el.contains(document.activeElement) || !!el.querySelector('details[open]');
}
function lessonHtml(l){
  return `<div class="item lesson"><div class=sit>When ${esc(l.situation)}</div>${esc(l.content)}`+
    `<span class=badge>conf ${num(l.confidence)}</span></div>`;
}
async function renderDisputes(force){
  const el = document.getElementById('disputes');
  if(!force && busy(el)) return;
  const ds = await fetch('/api/disputes').then(r => r.json());
  document.getElementById('n-disputes').textContent = ds.length || '';
  el.innerHTML = ds.length ? ds.map(d => {
    const o = d.disputes;
    return `<div class=item><div class=muted>When ${esc(d.situation)}</div>`+
      `<div class="side new"><span class=who>${esc(d.by)}</span> says: ${esc(d.content)}</div>`+
      (o ? `<div class="side old"><span class=who>${esc(o.by)}</span> said: ${esc(o.content)}`+
           `<span class="badge ${o.status==='active'?'':'warn'}">${esc(o.status)}</span></div>`
         : `<div class="side old muted">(the disputed lesson isn't recorded)</div>`)+
      (canAct() ? `<div class=row><input class=f data-note="${esc(d.id)}" placeholder="note (optional)">`+
        `<button class=btn data-resolve="${esc(d.id)}" data-accept="1">Accept</button>`+
        `<button class="btn alt" data-resolve="${esc(d.id)}" data-accept="0">Reject</button></div>` : '')+
      `</div>`;
  }).join('') : '<div class="item muted">No open disputes.</div>';
}
async function renderDecisions(force){
  const el = document.getElementById('decisions');
  if(!force && busy(el)) return;
  const ds = await fetch('/api/decisions?limit=25').then(r => r.json());
  el.innerHTML = ds.length ? ds.map(d => {
    const v = d.verdict || {}, cands = d.candidates || [];
    const idx = id => { const i = cands.findIndex(c => c.id === id); return i < 0 ? '?' : 'c' + (i + 1); };
    const verdict = esc(v.relation || '?') + (v.target_id ? ' ' + idx(v.target_id) : '');
    const cls = d.action === 'superseded' || d.action === 'disputed' ? 'warn' : '';
    return `<div class=item><span class="badge ${cls}" style="margin-left:0">${esc(d.action)}</span>`+
      `<span class=badge>${verdict}</span><span class=badge>${esc(v.model || '')}</span>`+
      (typeof v.confidence === 'number' ? `<span class=badge>conf ${num(v.confidence)}</span>` : '')+
      (v.escalated ? `<span class="badge warn">escalated</span>` : '')+
      `<span class=muted> ${esc(d.agent)} · ${esc(d.at.slice(11,19))}</span>`+
      `<div>${esc(d.content)}</div>`+
      `<details><summary class=muted>${cands.length} candidate${cands.length===1?'':'s'}`+
      (canAct() ? ' · report a mistake' : '') + `</summary>`+
      cands.map((c, i) => `<div class=side><b>c${i+1}</b> ${esc(c.content)}</div>`).join('')+
      (canAct() ? `<div class=row>`+
        `<select class=f data-rel="${esc(d.id)}"><option value="">it should have been…</option>`+
        `<option value="novel">novel</option><option value="duplicate">duplicate of</option>`+
        `<option value="contradicts">contradicts</option></select>`+
        `<select class=f data-tgt="${esc(d.id)}"><option value="">target</option>`+
        cands.map((c, i) => `<option value="${esc(c.id)}">c${i+1}</option>`).join('')+`</select>`+
        `<input class=f data-tgtid="${esc(d.id)}" placeholder="or another lesson id">`+
        `<input class=f data-rnote="${esc(d.id)}" placeholder="note">`+
        `<button class=btn data-report="${esc(d.id)}">Report</button></div>` : '')+
      `</details></div>`;
  }).join('') : '<div class="item muted">No decisions recorded yet.</div>';
}
async function renderGolden(force){
  const el = document.getElementById('golden');
  if(!force && busy(el)) return;
  const cs = await fetch('/api/golden').then(r => r.json());
  document.getElementById('n-golden').textContent = cs.length || '';
  el.innerHTML = cs.length ? cs.map(c => {
    const p = c.payload || {}, g = c.got || {};
    let body;
    if(c.kind === 'classification'){
      const ex = p.existing || [], idx = id => { const i = ex.findIndex(e => e.id === id); return i < 0 ? '' : ' e' + (i + 1); };
      body = `<div class="side new">${esc(p.new && p.new.content)}</div>`+
        ex.map((e, i) => `<div class=side><b>e${i+1}</b> ${esc(e.content)}</div>`).join('')+
        `<div class=muted>proposed: <b>${esc(p.expected && p.expected.relation)}${esc(idx(p.expected && p.expected.target))}</b>`+
        ` · system said: ${esc(g.relation || '?')}${esc(idx(g.target_id))}${g.model ? ' (' + esc(g.model) + ')' : ''}</div>`;
    } else {
      body = `<div class="side new">${esc(p.content)}</div>`+
        `<div class=muted>proposed: <b>${p.expect_blocked ? 'should be blocked' : 'should pass'}</b></div>`;
    }
    return `<div class=item><span class=badge style="margin-left:0">${esc(c.kind)}</span>`+
      `<span class=badge>${esc(c.source)}</span>${body}`+
      (c.note ? `<div class=muted>note: ${esc(c.note)}</div>` : '')+
      `<div class=row><code>python -m evals.review --accept ${esc(c.id.slice(0,8))}</code>`+
      (canAct() ? `<button class="btn alt" data-reject="${esc(c.id)}">Reject</button>` : '')+
      `</div></div>`;
  }).join('') : '<div class="item muted">Queue is empty.</div>';
}
async function renderQuarantine(force){
  const el = document.getElementById('quarantine');
  if(!force && busy(el)) return;
  const qs = await fetch('/api/quarantine').then(r => r.json());
  document.getElementById('n-quarantine').textContent = qs.length || '';
  el.innerHTML = qs.length ? qs.map(q =>
    `<div class=item><div class=muted>When ${esc(q.situation)} · ${esc(q.by)} · ${esc(q.created_at.slice(0,19))}</div>`+
    `${esc(q.content)}<div>`+q.reasons.map(r => `<span class="badge bad" style="margin-left:0;margin-right:4px">${esc(r)}</span>`).join('')+
    `</div><div class=muted>id ${esc(q.id)}</div></div>`).join('')
    : '<div class="item muted">Nothing quarantined.</div>';
}
function refreshPanels(force){
  renderDisputes(force); renderDecisions(force); renderGolden(force); renderQuarantine(force);
}

// One delegated click handler: ids come from data-* attributes.
document.addEventListener('click', async ev => {
  const b = ev.target.closest('button'); if(!b) return;
  if(b.dataset.resolve){
    const id = b.dataset.resolve, note = (document.querySelector(`[data-note="${CSS.escape(id)}"]`) || {}).value || '';
    if(await act(`/api/disputes/${encodeURIComponent(id)}/resolve`, {accept: b.dataset.accept === '1', note}, 'msg-disputes')){
      document.getElementById('msg-disputes').textContent = b.dataset.accept === '1' ? 'Accepted: the original is superseded.' : 'Rejected: the original stands.';
      renderDisputes(true); tick();
    }
  } else if(b.dataset.report){
    const id = b.dataset.report, q = s => document.querySelector(`[data-${s}="${CSS.escape(id)}"]`);
    const relation = q('rel').value, target = q('tgtid').value.trim() || q('tgt').value;
    if(!relation){ const m = document.getElementById('msg-decisions'); m.className='msg err'; m.textContent='Pick what it should have been.'; return; }
    const j = await act(`/api/decisions/${encodeURIComponent(id)}/report`,
                        {relation, target_id: relation === 'novel' ? null : (target || null), note: q('rnote').value}, 'msg-decisions');
    if(j){ document.getElementById('msg-decisions').textContent = j.queued ? 'Queued for review as an eval case.' : 'Already queued for review.';
           renderDecisions(true); renderGolden(true); }
  } else if(b.dataset.reject){
    if(await act(`/api/golden/${encodeURIComponent(b.dataset.reject)}/reject`, {}, 'msg-golden')){
      document.getElementById('msg-golden').textContent = 'Rejected.'; renderGolden(true);
    }
  }
});

// ----------------------------------------------------- original panels ---
async function travel(){
  const local = document.getElementById('ts').value;
  if(!local) return;
  // datetime-local is naive browser-local time; send an absolute UTC instant.
  const ts = new Date(local).toISOString();
  const past = await fetch('/api/lessons_as_of?ts='+encodeURIComponent(ts)).then(r=>r.json());
  document.getElementById('past').innerHTML = past.error ? esc(past.error) :
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
async function drawCosts(){
  const c = await fetch('/api/costs').then(r=>r.json());
  if(!c.calls){return;}
  const models = c.by_model.map(m=>
    `<div class=item><span class=who>${esc(m.model)}</span> `+
    `<span class=badge>${m.calls} calls</span>`+
    `<span class=badge>$${num(m.cost_usd, 4)}</span>`+
    `<span class=badge>${m.avg_ms}ms avg</span></div>`).join('');
  document.getElementById('costs').innerHTML =
    `<div class=stats style="padding:0 0 10px">`+
    `<div class=stat><b>$${num(c.total_cost_usd, 4)}</b>total</div>`+
    `<div class=stat><b>${c.calls}</b>LLM calls</div>`+
    `<div class=stat><b>${(c.escalation_rate*100).toFixed(0)}%</b>escalated</div>`+
    `<div class=stat><b>${c.p95_ms}ms</b>p95 latency</div></div>`+models;
}
async function drawEval(){
  const h = await fetch('/api/eval_history?suite=classification').then(r=>r.json());
  const pts = h.filter(e=>e.tier==='live' && e.metrics && e.metrics.accuracy!=null)
    .map((e,i)=>[i, e.metrics.accuracy, e.git_sha]);
  const el = document.getElementById('evalcurve');
  if(pts.length<2){el.innerHTML=
    '<text x=10 y=30 fill="#7d8ca3" font-size=12>Run: python -m evals.run_eval --tier live</text>';return;}
  const X=i=>20+i*(360/Math.max(pts.length-1,1)), Y=a=>135-a*115;
  const line=pts.map((p,i)=>`${i?'L':'M'}${X(p[0])},${Y(p[1])}`).join(' ');
  el.innerHTML=`<line x1=20 y1=${Y(1)} x2=380 y2=${Y(1)} stroke="#1e2a3f"/>`+
    `<text x=2 y=${Y(1)+3} fill="#3a4a63" font-size=8>1.0</text>`+
    `<path d="${line}" fill="none" stroke="#4ade80" stroke-width="2"/>`+
    pts.map(p=>`<circle cx=${X(p[0])} cy=${Y(p[1])} r=3 fill="#ffb454"><title>${esc(p[2])}: ${p[1]}</title></circle>`).join('');
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
    `<div class=item><span class=who>${esc(f.agent)}</span> <span class=act>${esc(f.action)}</span> `+
    `${esc(f.detail)} <span class=sub>${esc(f.at.slice(11,19))}</span></div>`).join('');
  document.getElementById('lessons').innerHTML = lessons.map(l=>
    `<div class="item lesson"><div class=sit>When ${esc(l.situation)}</div>${esc(l.content)}`+
    `<span class=badge>by ${esc(l.learned_by)}</span><span class=badge>conf ${num(l.confidence)}</span>`+
    `<span class=badge>helped ${l.times_helpful}x</span></div>`).join('');
}
fetch('/api/curator').then(r=>r.json()).then(c=>{ curator = c; renderCuratorBar(); refreshPanels(true); });
tick(); drawCurve(); drawCosts(); drawEval();
setInterval(tick, 3000); setInterval(drawCurve, 10000);
setInterval(drawCosts, 5000); setInterval(drawEval, 10000);
setInterval(() => refreshPanels(false), 10000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
