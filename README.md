# 🌊 Tributary

**Shared, persistent, conflict-safe memory for AI agents — built on CockroachDB and AWS.**

Every agent's learnings flow into one shared river of memory. When one agent learns a lesson, every agent — current or future, related or not — knows it instantly. Agents are born knowing what the tribe knows, and die leaving the tribe smarter.

> Built for the CockroachDB × AWS Hackathon.

## The problem

AI agents are amnesiacs. Every agent process re-learns the same painful lessons — the API that rate-limits without a magic header, the config key that's silently deprecated — burning steps, tokens, and time. Passing context between a parent and its subagents doesn't fix this: that memory dies with the session and only flows down the process tree.

## What Tributary does

Tributary is a memory layer, not a framework. Agents call four functions:

| Call | What happens |
|---|---|
| `recall(query)` | Semantic search (CockroachDB **vector index**) over the tribe's active lessons |
| `learn(content, situation)` | One **serializable transaction**: embed → find similar lessons → LLM classifies *duplicate / contradicts / novel* → reinforce, supersede, or insert |
| `reinforce(id)` | A recalled lesson actually helped — confidence goes up |
| `retire(id)` | Curation (agents, the Gardener, or a human via the **MCP Server**) |
| `recall_as_of(query, ts)` | 🕰️ **Time travel**: what would the tribe have recalled at a past instant? (CockroachDB `AS OF SYSTEM TIME`) |

Because every write is a serializable transaction, two agents learning contradictory facts *at the same instant* resolve deterministically — one lesson stays active, the other is superseded with a provenance chain. No lost updates, no split brain. That's why shared agent memory needs a real database, not a JSON file.

## Architecture

```
              Claude Code CLI (headless)  +  local embeddings (1024-d)
                                      │
        agent-a ──┐                   │
        agent-b ──┤── tributary lib: recall() / learn()
        agent-c ──┘        │
   (separate processes,    ▼
    days apart, no IPC)  CockroachDB Cloud ◄── MCP Server ── Claude Code
                         ├ lessons (VECTOR(1024) + vector index)   (human curation)
                         ├ agents
                         └ memory_audit
                           ▲                 ▲
                 Lambda "Gardener"       Dashboard (App Runner)
                 (EventBridge: decay,    live memory feed + lesson browser
                  retire stale lessons)
```

## Quickstart

```bash
git clone https://github.com/aaditya-diwan/tributary && cd tributary
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[embeddings,dashboard,dev]"
cp .env.example .env                             # fill in DATABASE_URL
# agents use the `claude` CLI for reasoning — install Claude Code and log in

python scripts/init_db.py                        # create schema + vector index
python scripts/run_demo.py                       # the A-then-B demo
uvicorn dashboard.app:app --reload               # dashboard at localhost:8000
```

### The demo

`run_demo.py` drops two *unrelated* agent processes into **the Gauntlet** — a simulated ops environment with deterministic traps (a build that fails without a cache clear, a silently deprecated config key, a deploy API that 429s without `X-Batch: true`).

- **agent-a** (empty memory) hits every trap, figures them out, and distills lessons into Tributary.
- **agent-b** (fresh process, seconds later) recalls those lessons and sails through, citing them: *"Tribal knowledge says the deploy API needs X-Batch: true — applying it."*

Measured result (see the verified run below): **~20% fewer tokens and 2 fewer steps**, with agent-b citing each lesson by ID as it applies it. Kill everything and run agent-c tomorrow — it still knows.

### Join the tribe from Claude Code (or any MCP client)

Tributary is itself an MCP server — one config line gives *any* coding agent
shared memory with every other agent on your team:

```bash
pip install -e ".[mcp]"
claude mcp add tributary \
    -e DATABASE_URL=<your-crdb-url> \
    -e TRIBUTARY_AGENT_NAME=alice-claude-code \
    -- python -m mcp_server.server
```

Now Alice's Claude Code session learns a gotcha (`tribal_learn`), and Bob's
session — different machine, different repo — already knows it
(`tribal_recall`). Tools exposed: `tribal_recall`, `tribal_learn`,
`tribal_reinforce`, `tribal_retire`, `tribal_recall_as_of`, `tribal_stats`.

### Time-travel memory 🕰️

CockroachDB can read any table as it existed at a past instant — no
snapshots, one SQL clause. Tributary uses it for belief forensics:

```python
memory.recall_as_of("deploy api rate limits", "2026-07-10T15:42:00")
# → what the tribe believed BEFORE agent-b's discovery superseded it
```

The dashboard has a time slider for this, and MCP clients get it as
`tribal_recall_as_of`. Try faking that with a JSON file.

### The memory immune system 🛡️

Shared memory's scary failure mode: one agent learns something *wrong* and
poisons the tribe. Watch the tribe heal itself:

```bash
python scripts/poison_demo.py
```

It injects a deliberately false lesson, then runs an agent whose reality
contradicts it — the agent fails, discovers the truth, and its corrected
lesson transactionally supersedes the poison (with the full provenance chain
preserved for the autopsy).

### The generational learning curve 📉

```bash
python scripts/run_generations.py --generations 6
```

Six generations of fresh agents, task phrasing varied so recall has to work
semantically. Tokens-per-task falls as the tribe's memory accumulates — the
dashboard plots the curve. The species gets smarter.

### Tests

The conflict guarantees are tested against a real cluster (no AWS needed — offline mode uses deterministic embeddings):

```bash
TRIBUTARY_OFFLINE=1 pytest tests/ -v
```

Tests run in their own `tributary_test` database (created automatically by
`tests/conftest.py`), so fixture lessons — which carry fake offline
embeddings — can never leak into the tribe's real memory.

## Verified end-to-end run (2026-07-22)

The full pipeline was exercised against a real CockroachDB Cloud serverless
cluster (v26.2.1, AWS us-east-1) with real `claude -p` reasoning and real
local embeddings. Observations:

**Conflict tests: 4/4 passed** against the live cluster — concurrent
contradiction resolution, sequential supersede chains, duplicate-reinforce,
and paraphrase recall.

**The A-then-B demo, from empty memory:**

| agent | outcome | steps | tokens | lessons recalled |
|---|---|---|---|---|
| agent-a | SUCCESS | 11 | 2869 | 0 (learned 3) |
| agent-b | SUCCESS | 9 | 2305 | 3 (reinforced all 3) |

agent-b cited each tribal lesson by ID as it applied it (cache-clear on
linker 137, `db_url_v2` on the empty config key, `X-Batch: true` on the
first 429 — skipping the blind retry agent-a needed). Net: **20% fewer
tokens, 2 fewer steps**, and confidence scores went up on all three lessons.

**Gotchas found while testing** (all fixed in this repo):

1. *TLS*: serverless clusters use Cockroach's own CA — system trust roots
   aren't enough. Download the cluster cert to
   `%APPDATA%\postgresql\root.crt` (libpq's default lookup path) or append
   `&sslrootcert=<path>` to `DATABASE_URL`.
2. *Test contamination*: the offline tests originally wrote fixture lessons
   (fake embeddings) into the same database the demo reads — agent-a started
   with 4 recalled lessons and the A-then-B comparison collapsed to
   "-1 fewer steps". Fixed by isolating tests in `tributary_test`
   (`tests/conftest.py`).
3. *Windows console encoding*: the agent's reasoning text can contain
   characters cp1252 can't print (`→`), which crashed a run mid-flight —
   *after* solving the traps but *before* distilling lessons, losing them.
   Fixed by reconfiguring stdout to UTF-8 in `agents/runner.py`.
4. *First-run download*: the embedding model is ~1.3 GB; the first
   `run_demo.py` invocation takes a few extra minutes.

## How the sponsor tools are used

**CockroachDB** (hackathon requires ≥2 — we use all four):

1. **Distributed Vector Indexing** — the core of `recall()`. Lessons are embedded (1024-d, local sentence-transformers) and stored in a `VECTOR(1024)` column with a `VECTOR INDEX`; agents retrieve tribal knowledge by semantic similarity (`<=>` cosine distance), so a paraphrased situation still finds the right lesson. Vectors live in the same transactional database as the lesson metadata — no sync gap between embeddings and truth. `AS OF SYSTEM TIME` on the same table gives time-travel recall for free.
2. **Managed MCP Server** — humans supervise the tribe's memory from Claude Code: *"What has the tribe learned about the deploy API?"*, *"Which agent contributed the most helpful lessons?"*, *"Retire lesson X, it's outdated."* Configured from the Cloud Console (read-only mode + audit logging for safety). Tributary also ships its own MCP server (`mcp_server/`) so any MCP client can join the tribe as a first-class memory participant.
3. **ccloud CLI** — used to provision the cluster, create the service account, and pull connection info (`ccloud cluster create`, `ccloud cluster sql --connection-url`).
4. **Agent Skills Repo** — the schema and query patterns (enum status columns, vector index design, retry-on-40001) were built using the CockroachDB agent skills for schema/query design.

**AWS:**

1. **AWS Lambda + EventBridge** — the *Gardener* (`gardener/handler.py`) runs on a schedule: decays confidence of stale lessons and retires the withered ones, keeping shared memory trustworthy.
2. **AWS App Runner** — hosts the public dashboard (live memory feed, lesson browser, conflict counter) from `dashboard/Dockerfile`.

(Agent reasoning + the lesson classifier run on the headless Claude Code CLI —
`claude -p` — so agents use your existing Claude auth; embeddings come from a
local sentence-transformers model.)

## Repo layout

```
tributary/           the memory library (the product)
  memory.py            recall / learn / reinforce / retire
  db.py                CockroachDB connection + serializable-retry
  embeddings.py        local sentence-transformers wrapper (+ offline mode)
  llm.py               headless Claude Code CLI + lesson classifier
  schema.sql
agents/              tool-use agent runner (headless Claude Code)
gauntlet/            the trap environment for the demo
mcp_server/          Tributary's own MCP server — any agent can join the tribe
dashboard/           FastAPI dashboard (App Runner): feed, lessons, curve, time travel
gardener/            Lambda memory gardener
scripts/             init_db, run_demo, run_generations, poison_demo
tests/               concurrent-contradiction tests
```

## Deploying on AWS

One command, via the CDK app in `infra/` (builds and pushes both container
images, stands up Lambda + EventBridge + App Runner):

```powershell
cd infra && pip install -r requirements.txt && cdk bootstrap
$env:DATABASE_URL = "<your-crdb-url>"; cdk deploy   # outputs the dashboard URL
```

Full walkthrough (cluster via ccloud CLI, manual
equivalents) in [docs/DEPLOY.md](docs/DEPLOY.md).

## License

MIT — see [LICENSE](LICENSE).
