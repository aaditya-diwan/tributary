# 🌊 Tributary

**Shared, persistent, conflict-safe memory for AI agents — built on CockroachDB and AWS Bedrock.**

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
                     Amazon Bedrock (Claude + Titan Embeddings V2)
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
pip install -e ".[dashboard,dev]"
cp .env.example .env                             # fill in DATABASE_URL + AWS creds

python scripts/init_db.py                        # create schema + vector index
python scripts/run_demo.py                       # the A-then-B demo
uvicorn dashboard.app:app --reload               # dashboard at localhost:8000
```

### The demo

`run_demo.py` drops two *unrelated* agent processes into **the Gauntlet** — a simulated ops environment with deterministic traps (a build that fails without a cache clear, a silently deprecated config key, a deploy API that 429s without `X-Batch: true`).

- **agent-a** (empty memory) hits every trap, figures them out, and distills lessons into Tributary.
- **agent-b** (fresh process, seconds later) recalls those lessons and sails through, citing them: *"Tribal knowledge says the deploy API needs X-Batch: true — applying it."*

Typical result: **~50–70% fewer tokens and half the steps.** Kill everything and run agent-c tomorrow — it still knows.

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

## How the sponsor tools are used

**CockroachDB** (hackathon requires ≥2 — we use all four):

1. **Distributed Vector Indexing** — the core of `recall()`. Lessons are embedded (Titan V2, 1024-d) and stored in a `VECTOR(1024)` column with a `VECTOR INDEX`; agents retrieve tribal knowledge by semantic similarity (`<=>` cosine distance), so a paraphrased situation still finds the right lesson. Vectors live in the same transactional database as the lesson metadata — no sync gap between embeddings and truth. `AS OF SYSTEM TIME` on the same table gives time-travel recall for free.
2. **Managed MCP Server** — humans supervise the tribe's memory from Claude Code: *"What has the tribe learned about the deploy API?"*, *"Which agent contributed the most helpful lessons?"*, *"Retire lesson X, it's outdated."* Configured from the Cloud Console (read-only mode + audit logging for safety). Tributary also ships its own MCP server (`mcp_server/`) so any MCP client can join the tribe as a first-class memory participant.
3. **ccloud CLI** — used to provision the cluster, create the service account, and pull connection info (`ccloud cluster create`, `ccloud cluster sql --connection-url`).
4. **Agent Skills Repo** — the schema and query patterns (enum status columns, vector index design, retry-on-40001) were built using the CockroachDB agent skills for schema/query design.

**AWS:**

1. **Amazon Bedrock** — Claude (agent reasoning + the lesson classifier + lesson distillation) and Titan Text Embeddings V2 (1024-d embeddings), via the Converse and InvokeModel APIs.
2. **AWS Lambda + EventBridge** — the *Gardener* (`gardener/handler.py`) runs on a schedule: decays confidence of stale lessons and retires the withered ones, keeping shared memory trustworthy.
3. **AWS App Runner** — hosts the public dashboard (live memory feed, lesson browser, conflict counter) from `dashboard/Dockerfile`.

## Repo layout

```
tributary/           the memory library (the product)
  memory.py            recall / learn / reinforce / retire
  db.py                CockroachDB connection + serializable-retry
  embeddings.py        Titan wrapper (+ offline mode)
  llm.py               Bedrock Converse + lesson classifier
  schema.sql
agents/              Bedrock Converse tool-use agent runner
gauntlet/            the trap environment for the demo
mcp_server/          Tributary's own MCP server — any agent can join the tribe
dashboard/           FastAPI dashboard (App Runner): feed, lessons, curve, time travel
gardener/            Lambda memory gardener
scripts/             init_db, run_demo, run_generations, poison_demo
tests/               concurrent-contradiction tests
```

## License

MIT — see [LICENSE](LICENSE).
