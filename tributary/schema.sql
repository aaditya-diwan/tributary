-- Tributary memory schema for CockroachDB (v25.2+ for VECTOR INDEX).

CREATE TABLE IF NOT EXISTS agents (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       STRING NOT NULL UNIQUE,
    role       STRING NOT NULL DEFAULT 'writer',  -- reader | writer | curator
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ
);

-- Privilege separation for existing databases created before the role column.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS role STRING NOT NULL DEFAULT 'writer';

-- 'quarantined' = failed the injection screen; 'disputed' = a writer challenged
-- another agent's lesson and it awaits curator review. Neither is recalled or
-- shown to the classifier, so poisoned/contested content can't spread.
CREATE TYPE IF NOT EXISTS lesson_status AS ENUM ('active', 'superseded', 'retired');
ALTER TYPE lesson_status ADD VALUE IF NOT EXISTS 'quarantined';
ALTER TYPE lesson_status ADD VALUE IF NOT EXISTS 'disputed';

CREATE TABLE IF NOT EXISTS lessons (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content        STRING NOT NULL,          -- the lesson itself, one or two crisp sentences
    situation      STRING NOT NULL,          -- when it applies, e.g. "deploying via the internal deploy API"
    embedding      VECTOR(1024) NOT NULL,    -- 1024-d embedding of situation + content
    agent_id       UUID NOT NULL REFERENCES agents(id),
    task_id        UUID,
    evidence       STRING,                   -- what happened that taught this (error message, etc.)
    confidence     FLOAT NOT NULL DEFAULT 0.6,
    times_recalled INT NOT NULL DEFAULT 0,
    times_helpful  INT NOT NULL DEFAULT 0,
    status         lesson_status NOT NULL DEFAULT 'active',
    superseded_by  UUID,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);

CREATE VECTOR INDEX IF NOT EXISTS lessons_embedding_idx ON lessons (embedding);

CREATE INDEX IF NOT EXISTS lessons_status_idx ON lessons (status, confidence);

-- Benchmark runs, for the generational learning curve.
CREATE TABLE IF NOT EXISTS runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name       STRING NOT NULL,
    generation       INT,
    task             STRING,
    outcome          STRING,
    steps            INT,
    tokens           INT,
    seconds          FLOAT,
    lessons_recalled INT,
    at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Eval harness results, one row per suite run — the dashboard plots these
-- over time so classification accuracy is a metric, not a demo.
CREATE TABLE IF NOT EXISTS eval_results (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    git_sha  STRING,
    tier     STRING NOT NULL,   -- offline | live
    suite    STRING NOT NULL,   -- classification | retrieval | e2e | redteam | judge
    metrics  JSONB NOT NULL,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS eval_results_at_idx ON eval_results (suite, at DESC);

CREATE TABLE IF NOT EXISTS memory_audit (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id   UUID,
    agent_name STRING,
    action     STRING NOT NULL,   -- recall | learn | reinforce | supersede | retire | decay
    lesson_id  UUID,
    detail     STRING,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_audit_at_idx ON memory_audit (at DESC);
