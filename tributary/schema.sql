-- Tributary memory schema for CockroachDB (v25.2+ for VECTOR INDEX).

CREATE TABLE IF NOT EXISTS agents (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       STRING NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ
);

CREATE TYPE IF NOT EXISTS lesson_status AS ENUM ('active', 'superseded', 'retired');

CREATE TABLE IF NOT EXISTS lessons (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content        STRING NOT NULL,          -- the lesson itself, one or two crisp sentences
    situation      STRING NOT NULL,          -- when it applies, e.g. "deploying via the internal deploy API"
    embedding      VECTOR(1024) NOT NULL,    -- Titan V2 embedding of situation + content
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
