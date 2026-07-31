"""CockroachDB connection handling and serializable-transaction retry."""

import time
from pathlib import Path

import psycopg

from tributary import config

RETRYABLE_SQLSTATE = "40001"  # serialization failure — CRDB asks the client to retry
MAX_RETRIES = 5


def connect() -> psycopg.Connection:
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return psycopg.connect(config.DATABASE_URL, application_name="tributary")


def run_txn(fn, retries: int = MAX_RETRIES):
    """Run `fn(cursor)` inside a serializable transaction, retrying on 40001.

    CockroachDB runs SERIALIZABLE by default; concurrent conflicting writes
    surface as a retryable error rather than silent lost updates. This is the
    property Tributary leans on for conflict-safe shared memory.
    """
    last_err = None
    for attempt in range(retries):
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
                return result
        except psycopg.errors.SerializationFailure as e:
            last_err = e
            time.sleep(0.05 * (2**attempt))  # brief backoff, then retry
        except psycopg.Error as e:
            if getattr(e, "sqlstate", None) == RETRYABLE_SQLSTATE:
                last_err = e
                time.sleep(0.05 * (2**attempt))
            else:
                raise
    raise last_err


def run_readonly(sql: str, params=()) -> list[tuple]:
    """Run a single read-only statement on an autocommit connection.

    AS OF SYSTEM TIME queries must run outside an explicit transaction,
    so time-travel reads go through here rather than run_txn.
    """
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def vec_literal(embedding: list[float]) -> str:
    """Render an embedding as a CockroachDB VECTOR literal string."""
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


def init_schema() -> None:
    """Apply schema.sql statement by statement (autocommit).

    Statements run individually so idempotent migrations (ALTER TYPE ... ADD
    VALUE IF NOT EXISTS, ALTER TABLE ... ADD COLUMN IF NOT EXISTS) can be
    used by statements later in the same file — CockroachDB won't let a new
    enum value or column be referenced inside the transaction that added it.
    """
    raw = (Path(__file__).parent / "schema.sql").read_text()
    # Strip `--` line comments before splitting on ';' so a semicolon inside a
    # comment doesn't get mistaken for a statement terminator.
    schema = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    statements = [s.strip() for s in schema.split(";") if s.strip()]
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
