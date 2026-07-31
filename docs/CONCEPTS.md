# Concepts: a learning path for Tributary

This guide takes you from the problem statement to every concept the project
uses, ordered from most basic to most advanced. Each topic has a short
explanation, why it matters here, a pointer to where it lives in the code, and
what to read to go deeper. If you read it top to bottom you will understand the
whole system.

---

## Part 0: The problem and the domain

### The domain: AI agents

An "AI agent" is a program that puts a large language model (LLM) in a loop:
the model is given a goal and a set of tools (functions it can call), it decides
an action, the program runs that action, feeds the result back, and repeats
until the goal is met. This is different from a single chatbot reply. The agent
takes many steps, and each step costs tokens (money) and time.

### The problem: agents are amnesiacs

Every agent process starts fresh. When an agent discovers something painful and
non-obvious (an API that rate-limits unless you send a magic header, a config
key that was silently deprecated), that knowledge dies when the process exits.
The next agent, even on the same team, re-learns the same lesson from scratch,
burning the same steps and tokens. Passing context from a parent agent to its
subagents does not solve this, because that context only flows down the current
process tree and vanishes with the session.

### The solution Tributary proposes

A shared, persistent memory layer that every agent reads from and writes to.
One agent learns a lesson once; every other agent, now or in the future, on any
machine, knows it. The hard part is not storing text. The hard part is keeping
that shared memory *correct* when many independent agents write to it at once,
and *safe* when the text they write cannot be trusted. Most of this project is
about those two problems.

Read more: search for "LLM agents", "tool use / function calling", and
"agent memory".

---

## Part 1: Foundations (basic)

### 1.1 LLM tool use (function calling)

The model does not run code. It emits a structured request ("call `deploy` with
these arguments"), the harness runs the real function, and returns the result
as the next input. In this repo the loop lives in `agents/runner.py` and
`agents/react_runner.py`, and the tool schemas are in `gauntlet/env.py`.

Read: Anthropic and OpenAI "tool use" / "function calling" docs.

### 1.2 Embeddings and semantic search

An embedding is a list of numbers (a vector) that represents the meaning of a
piece of text. Texts with similar meaning have vectors that are close together,
even if they share no words. "the compiler keeps getting killed" and "clear the
build cache" can land near each other. Closeness is usually measured by cosine
distance (the angle between two vectors).

Why it matters here: an agent describes its situation in its own words, and we
still need to find the relevant past lesson. Keyword search would miss
paraphrases; semantic search does not. See `tributary/embeddings.py`. Tributary
uses a local `sentence-transformers` model (1024 dimensions) so no data leaves
your machine and there is no per-call API cost.

Read: "sentence embeddings", "cosine similarity", the sentence-transformers
library docs.

### 1.3 Vector indexes and approximate nearest neighbour (ANN)

If you have a million lessons, comparing a query vector against every one is too
slow. A vector index organises vectors so you can find the closest few quickly,
trading a little accuracy for a lot of speed (approximate nearest neighbour).

Why it matters here: `recall()` runs a nearest-neighbour search over the
`embedding` column using CockroachDB's `VECTOR INDEX` and the `<=>` cosine
distance operator. See `tributary/schema.sql` and `recall()` in
`tributary/memory.py`.

Read: "approximate nearest neighbour search", "HNSW", "pgvector", CockroachDB
vector index docs.

---

## Part 2: Data and consistency (intermediate)

This is the part that justifies using a real distributed database instead of a
JSON file, and it is the intellectual core of the project.

### 2.1 Transactions and ACID

A transaction groups several database operations so they either all happen or
none do (atomicity), leave the database valid (consistency), do not corrupt each
other (isolation), and survive a crash once committed (durability). These four
properties are called ACID.

Why it matters here: writing a lesson is not one operation. It is: find similar
lessons, decide the relationship, then either reinforce an old lesson, insert a
new one, or supersede an old one. If that sequence is interrupted or interleaved
with another agent's write, the memory can end up inconsistent. Wrapping it in a
transaction prevents that. See `run_txn` in `tributary/db.py`.

Read: "database transactions", "ACID properties".

### 2.2 Isolation levels and serializable isolation

When two transactions run at the same time, the isolation level decides how much
they are allowed to affect each other. Weaker levels are faster but permit
anomalies (lost updates, reading half-written state). The strongest level,
SERIALIZABLE, guarantees the result is *as if* the transactions ran one after
another in some order, with no anomalies at all.

Why it matters here: this is the whole reason Tributary needs a serious
database. Two agents can learn contradictory facts at the very same instant
("use port 8080" and "use port 9090"). Under a weak isolation level both writes
"win" and the tribe ends up with two active, contradictory lessons, a split
brain that every future recall then spreads. CockroachDB runs SERIALIZABLE by
default, so exactly one lesson stays active and the other is superseded. See
`tests/test_conflicts.py` for the test that proves this.

Read: "SQL isolation levels", "serializable isolation", "write skew".

### 2.3 Optimistic concurrency and retry (the 40001 error)

Serializable isolation is often implemented optimistically: transactions run
assuming no conflict, and at commit time the database checks. If two conflicted,
one is aborted with a retryable error (SQLSTATE `40001`, a serialization
failure) and the client simply runs it again, this time seeing the other's
committed result. This is how you get correctness without long-held locks.

Why it matters here: `run_txn` in `tributary/db.py` catches `40001` and retries
with backoff. The number of retries is the visible cost of the safety
guarantee, which is exactly why the OpenTelemetry span records the retry count.

Read: "optimistic concurrency control", "CockroachDB transaction retries".

### 2.4 MVCC and time-travel queries

Multi-Version Concurrency Control (MVCC) means the database keeps older versions
of each row for a while instead of overwriting in place. A side benefit: you can
ask "what did this table look like at 3:42pm yesterday?" and get a consistent
answer with no snapshots and no extra tables.

Why it matters here: `recall_as_of()` uses CockroachDB's `AS OF SYSTEM TIME`
clause for "belief forensics", what did the tribe believe before some discovery
superseded it? See `recall_as_of` and `lessons_as_of` in `tributary/memory.py`.
Time-travel reads must run outside a normal transaction, which is why they go
through `run_readonly`.

Read: "MVCC", "CockroachDB AS OF SYSTEM TIME", "temporal queries".

### 2.5 Distributed SQL

CockroachDB is a distributed SQL database: it spreads data across many nodes
(and regions) while still offering a single logical SQL database with
serializable transactions. That is what makes shared memory work across
machines and survive node failures.

Read: "distributed SQL", "NewSQL", the CockroachDB architecture docs, and the
Spanner paper (Google) that inspired this class of system.

---

## Part 3: The memory system design (intermediate to advanced)

### 3.1 Deduplication, contradiction, and novelty classification

When a new lesson arrives, it is one of three things relative to what the tribe
already knows: a duplicate (reinforce the existing one), a contradiction
(supersede the old one, newer evidence wins), or novel (insert it). Deciding
which is a judgement call, so an LLM makes it. See `classify_lesson` in
`tributary/llm.py` and the golden test set in
`evals/golden/classification.jsonl`.

### 3.2 Provenance and supersede chains

When a lesson is superseded, we do not delete the old one. We mark it
`superseded` and record which lesson replaced it (`superseded_by`). That chain
is the audit trail: you can always reconstruct what was believed and why it
changed. See the `lessons` table in `tributary/schema.sql`.

### 3.3 Confidence, decay, and curation

Lessons carry a confidence score that rises when they help and falls when they
go unused. A scheduled job (the "Gardener", an AWS Lambda in `gardener/`) decays
stale lessons and retires the ones that wither. This keeps the shared memory
trustworthy over time rather than letting it accumulate junk.

Read: "knowledge base curation", "confidence decay", "spaced repetition" for the
intuition behind reinforcement over time.

---

## Part 4: Safety (advanced, the strongest differentiator)

### 4.1 Prompt injection and the data/instruction boundary

An LLM reads everything in its context as potential instruction. If untrusted
text says "ignore your instructions and do X", the model may obey. This is
prompt injection, the top item on the OWASP list of LLM security risks.

Why it matters here: a "lesson" is text written by some agent. It flows into two
dangerous places: the classifier's prompt (where it could force a wrong
"contradicts" verdict and corrupt an existing lesson) and a future agent's
prompt (where it could tell that agent to leak a secret). Tributary treats every
lesson as untrusted data, never as instruction. See `tributary/guard.py`.

Read: "prompt injection", "OWASP Top 10 for LLM Applications", "indirect prompt
injection", "instruction/data separation".

### 4.2 Screening, quarantine, and structured output

Two defences work together. First, content shaped like an instruction is
detected and quarantined: stored for audit but kept out of recall and out of the
classifier's context, so it cannot spread. Second, the classifier is asked for
schema-constrained output (a fixed JSON shape with an enumerated answer), so
injected text cannot change the *shape* of the verdict, only its value, which is
then checked against a whitelist. See `screen_lesson` in `tributary/guard.py`
and `CLASSIFY_SCHEMA` in `tributary/llm.py`.

### 4.3 Privilege separation (authorization)

Not every caller should be able to do everything. Tributary gives agents roles:
`reader` (recall only), `writer` (add and reinforce, and supersede their own
lessons), and `curator` (can overturn another agent's lesson, and retire). A
writer who contradicts *another* agent's lesson does not silently delete it;
the challenge is filed as `disputed` for a curator to review. This stops any
single agent from quietly rewriting the tribe's shared knowledge. See the role
checks in `tributary/memory.py`.

Read: "authorization vs authentication", "principle of least privilege",
"role-based access control (RBAC)".

### 4.4 Red-teaming

You do not claim a defence works; you attack it and measure. The red-team suite
(`evals/golden/redteam.jsonl`, `tests/test_injection.py`) fires crafted attacks
(classifier hijacks, reader tool-hijacks, exfiltration attempts) and scores how
many are blocked, plus how many *benign* lessons are wrongly blocked (false
positives). A defence that blocks everything is useless.

Read: "red teaming LLMs", "adversarial testing".

---

## Part 5: The agent (advanced)

### 5.1 ReAct: reasoning and acting

ReAct is an agent pattern where the model interleaves reasoning ("the build
failed with 137, that usually means a stale cache") with acting (call
`clear_build_cache`). See `agents/react_runner.py`.

Read: the ReAct paper (Yao et al., 2022, "ReAct: Synergizing Reasoning and
Acting in Language Models").

### 5.2 Tool discipline: knowing when NOT to use a tool

A good agent does not call every tool it has. Tributary exposes memory as tools
the agent chooses to use, with an explicit policy: recall before an unfamiliar
ops step, but not for self-contained work like computing a hash. The
`gauntlet/compute.py` task is a negative control that makes this measurable: the
agent should recall zero times on it. This is a behaviour interviewers probe
for.

### 5.3 Failure modes: retries, timeouts, and garbage

Real tools time out, crash, and return nonsense. The system is built to survive
that: `llm._run` retries transient CLI failures with exponential backoff;
the Gauntlet's "chaos mode" randomly corrupts tool results so you can watch the
agent detect garbage, distrust it, and retry; and a hallucinated tool name is
returned as an error rather than crashing the loop. See `agents/react_runner.py`
and the chaos logic in `gauntlet/env.py`.

Read: "exponential backoff", "retry patterns", "chaos engineering".

---

## Part 6: Evaluation (advanced)

### 6.1 Why evals, not demos

A demo shows the system working once. An eval measures how well it works across
many cases, and lets you see a metric move (or regress) as you change prompts
and models. The harness is in `evals/`; its design is in
[../evals/README.md](../evals/README.md).

### 6.2 Golden sets and classification metrics

A "golden set" is hand-authored examples with known correct answers. For a
classifier you report precision (of what it flagged, how much was right), recall
(of what it should have flagged, how much it caught), F1 (their harmonic mean),
and a confusion matrix (what got mistaken for what). See `run_classification` in
`evals/run_eval.py`.

Read: "precision and recall", "F1 score", "confusion matrix".

### 6.3 Retrieval metrics

For search you measure whether the right item appears near the top: hit@k (is it
in the top k?) and MRR (mean reciprocal rank, how high on average?). See
`run_retrieval`.

Read: "recall@k", "mean reciprocal rank", "information retrieval metrics".

### 6.4 LLM-as-judge and calibration

Some outputs (like the quality of a distilled lesson) have no exact right
answer, so another LLM grades them on a rubric. But you cannot trust the judge
blindly. You calibrate it against a small hand-labelled set and report the
agreement (exact match, within-one, Pearson correlation). Tributary's judge is
honestly reported as harsher than humans: good for ranking and catching
regressions, not yet for absolute grades. See `run_judge` in `evals/run_eval.py`.

Read: "LLM as a judge", "inter-annotator agreement", "Cohen's kappa", "Pearson
correlation".

### 6.5 Regression testing and CI gates

A regression is when a change makes something that used to work stop working.
The offline eval tier is deterministic (fixed embeddings and a heuristic
classifier), so it can run on every push in continuous integration and fail the
build if a key metric drops below a saved baseline. See
`.github/workflows/eval.yml` and `evals/baseline.json`.

Read: "regression testing", "continuous integration", "golden/snapshot testing".

---

## Part 7: Observability and cost (advanced)

### 7.1 Distributed tracing with OpenTelemetry

Tracing records the tree of operations in one request as nested "spans", so you
can see where time went and what happened at each step. OpenTelemetry is the
standard for this. Tributary traces the agent loop and, notably, records the
serializable-retry count on the database span, making the cost of the
consistency guarantee visible. See `tributary/telemetry.py`.

Read: "distributed tracing", "OpenTelemetry", "spans and traces".

### 7.2 Cost tracking and model tiering

LLM calls cost money and time. Tributary logs each call's tokens, cost, and
latency (`tributary/costs.py`, the `llm_calls` table) and shows them on the
dashboard. It also does model tiering (also called model routing): the cheap
model handles easy classifications, and only hard or destructive ones (a
contradiction, which would supersede a lesson) escalate to the stronger, pricier
model. See `classify_lesson` in `tributary/llm.py`.

Read: "LLM cost optimization", "model routing / cascading", "p50 and p95
latency percentiles".

---

## Part 8: Infrastructure and integration (supporting)

### 8.1 Model Context Protocol (MCP)

MCP is an open standard that lets any AI client (Claude Code, Cursor, others)
connect to external tools and data through a common interface. Tributary ships
its own MCP server (`mcp_server/`), so any MCP client can join the tribe's memory
with one line of config.

Read: "Model Context Protocol (MCP)".

### 8.2 Headless LLM via subprocess

Instead of paying per token through a cloud LLM API, Tributary shells out to the
local `claude` CLI in headless mode, reusing existing auth. The tradeoff (no
server-side session, so the transcript is re-sent each turn) is documented in
the main README. See `tributary/llm.py`.

### 8.3 AWS serverless deployment

The supporting cast: AWS Lambda plus EventBridge runs the scheduled Gardener,
App Runner hosts the dashboard, and the whole thing is deployed with the AWS
CDK (infrastructure as code). See `gardener/`, `dashboard/`, `infra/`, and
[DEPLOY.md](DEPLOY.md).

Read: "AWS Lambda", "EventBridge", "AWS App Runner", "infrastructure as code",
"AWS CDK".

---

## Suggested reading order if you are new

1. Parts 0 and 1 (the problem, tool use, embeddings, vector search).
2. Part 2.1 to 2.3 (transactions, serializable isolation, retries). This is the
   heart of "why a database".
3. Part 3 (how a lesson is written and curated).
4. Part 4 (safety), then Part 5 (the agent).
5. Parts 6, 7, 8 (evaluation, observability, infrastructure) to see how the
   system is measured and run.

Then read the code in this order: `tributary/memory.py`, `tributary/db.py`,
`tributary/llm.py`, `tributary/guard.py`, `agents/react_runner.py`,
`evals/run_eval.py`.
