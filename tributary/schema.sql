-- Tributary memory schema for PostgreSQL (14+) with the pgvector extension.
--
-- Applied statement-by-statement in autocommit by tributary.db.init_schema,
-- so every statement here must be idempotent (IF NOT EXISTS / ADD VALUE IF
-- NOT EXISTS / DO-block guards). It is also valid input for `psql -f`.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agents (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL DEFAULT 'writer',  -- reader | writer | curator
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ
);

-- Privilege separation for existing databases created before the role column.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'writer';

-- 'quarantined' = failed the injection screen; 'disputed' = a writer challenged
-- another agent's lesson and it awaits curator review. Neither is recalled or
-- shown to the classifier, so poisoned/contested content can't spread.
-- (Postgres has no CREATE TYPE IF NOT EXISTS; the DO block swallows the
-- duplicate_object error so re-applying the schema is a no-op.)
DO $$ BEGIN
    CREATE TYPE lesson_status AS ENUM ('active', 'superseded', 'retired');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TYPE lesson_status ADD VALUE IF NOT EXISTS 'quarantined';
ALTER TYPE lesson_status ADD VALUE IF NOT EXISTS 'disputed';

CREATE TABLE IF NOT EXISTS lessons (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content        TEXT NOT NULL,            -- the lesson itself, one or two crisp sentences
    situation      TEXT NOT NULL,            -- when it applies, e.g. "deploying via the internal deploy API"
    embedding      vector(1024) NOT NULL,    -- 1024-d embedding of situation + content
    agent_id       UUID NOT NULL REFERENCES agents(id),
    task_id        UUID,
    evidence       TEXT,                     -- what happened that taught this (error message, etc.)
    confidence     DOUBLE PRECISION NOT NULL DEFAULT 0.6,
    times_recalled INT NOT NULL DEFAULT 0,
    times_helpful  INT NOT NULL DEFAULT 0,
    status         lesson_status NOT NULL DEFAULT 'active',
    superseded_by  UUID,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ,
    -- Temporal columns for time-travel recall. A lesson is part of the
    -- tribe's belief set on the interval [activated_at, deactivated_at).
    -- activated_at is NULL for lessons that were never active (quarantined,
    -- or disputed-then-rejected); deactivated_at is set on supersede/retire.
    -- Unlike an MVCC "as of" read, this history is never garbage-collected.
    activated_at   TIMESTAMPTZ,
    deactivated_at TIMESTAMPTZ
);

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;

-- For a 'disputed' lesson: the active lesson it contradicts. Accepting the
-- dispute supersedes that lesson; without the link, acceptance left both
-- active (the split brain the conflict handling exists to prevent).
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS disputes UUID;

-- Backfill disputes filed before the column existed, from their audit line
-- ("contradicts <uuid> (curator review)"). Idempotent: only fills NULLs.
UPDATE lessons l
SET disputes = substring(a.detail from 'contradicts ([0-9a-f-]{36})')::uuid
FROM memory_audit a
WHERE l.status = 'disputed' AND l.disputes IS NULL
  AND a.lesson_id = l.id AND a.action = 'dispute'
  AND a.detail ~ 'contradicts [0-9a-f-]{36}';

-- HNSW index with the cosine opclass, so `ORDER BY embedding <=> $q` is an
-- approximate nearest-neighbour scan rather than a full table sort. It is
-- PARTIAL over active lessons: an HNSW scan returns the ef_search nearest
-- rows *before* the WHERE filter runs, so a whole-table index would let
-- accumulated superseded/retired rows crowd active ones out of recall and
-- out of the classifier's candidate set. Every active-lesson query here
-- carries `status = 'active'`, so the planner can use it; time-travel reads
-- (no status filter) fall back to a scan, which is fine for forensics.
CREATE INDEX IF NOT EXISTS lessons_active_embedding_idx
    ON lessons USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS lessons_status_idx ON lessons (status, confidence);

CREATE INDEX IF NOT EXISTS lessons_temporal_idx ON lessons (activated_at, deactivated_at);

-- Benchmark runs, for the generational learning curve.
CREATE TABLE IF NOT EXISTS runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name       TEXT NOT NULL,
    generation       INT,
    task             TEXT,
    outcome          TEXT,
    steps            INT,
    tokens           INT,
    seconds          DOUBLE PRECISION,
    lessons_recalled INT,
    at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Eval harness results, one row per suite run — the dashboard plots these
-- over time so classification accuracy is a metric, not a demo.
CREATE TABLE IF NOT EXISTS eval_results (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    git_sha  TEXT,
    tier     TEXT NOT NULL,     -- offline | live
    suite    TEXT NOT NULL,     -- classification | retrieval | e2e | redteam | judge
    metrics  JSONB NOT NULL,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS eval_results_at_idx ON eval_results (suite, at DESC);

-- Every LLM call, for the cost/latency dashboard and model-tiering analysis.
CREATE TABLE IF NOT EXISTS llm_calls (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose    TEXT NOT NULL,     -- classify | classify-escalated | screen | distill | agent-step | judge
    model      TEXT NOT NULL,
    in_tokens  INT NOT NULL DEFAULT 0,
    out_tokens INT NOT NULL DEFAULT 0,
    cost_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    ms         INT NOT NULL DEFAULT 0,
    escalated  BOOLEAN NOT NULL DEFAULT false,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_calls_at_idx ON llm_calls (at DESC);

CREATE TABLE IF NOT EXISTS memory_audit (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id   UUID,
    agent_name TEXT,
    action     TEXT NOT NULL,     -- recall | learn | reinforce | supersede | retire | decay
    lesson_id  UUID,
    detail     TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_audit_at_idx ON memory_audit (at DESC);

-- One row per classification decision in learn(): the new lesson, a snapshot
-- of the candidate lessons it was judged against, and the verdict. Written in
-- the same transaction as the lesson write, so it always matches what
-- happened. It makes a later mistake report reproducible: a false "duplicate"
-- leaves no lesson row of its own, and candidate lessons can change or be
-- deleted afterwards.
CREATE TABLE IF NOT EXISTS decisions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    op          TEXT,                  -- log correlation id, e.g. learn-3f2a9c1d
    agent_id    UUID,
    situation   TEXT NOT NULL,
    content     TEXT NOT NULL,
    candidates  JSONB NOT NULL,        -- [{id, situation, content}] as classified
    verdict     JSONB NOT NULL,        -- {relation, target_id, confidence, model, escalated}
    action      TEXT NOT NULL,         -- inserted | reinforced | superseded | disputed
    lesson_id   UUID,                  -- the lesson written or reinforced
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decisions_at_idx ON decisions (at DESC);

-- Likely failures, queued for a human before they become golden eval rows
-- (`python -m evals.review`). Labels from a model (an escalation overrule)
-- need review as much as labels from people, so nothing is written to the
-- golden files automatically. The fingerprint dedups repeats, such as an MCP
-- client retrying the same call.
CREATE TABLE IF NOT EXISTS golden_candidates (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT NOT NULL,         -- classification | screen
    source      TEXT NOT NULL,         -- escalation-overrule | retired-as-injection | released-from-quarantine | reported
    fingerprint TEXT NOT NULL UNIQUE,
    payload     JSONB NOT NULL,        -- inputs and proposed label, shaped like a golden row
    got         JSONB,                 -- what the system decided
    note        TEXT,
    decision_id UUID,
    reported_by UUID,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    golden_id   TEXT,                  -- id of the golden row it became
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS golden_candidates_status_idx ON golden_candidates (status, created_at);
