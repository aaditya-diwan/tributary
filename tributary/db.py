"""PostgreSQL connection handling and serializable-transaction retry."""

import time
from pathlib import Path

import psycopg

from tributary import config, log

logger = log.get_logger(__name__)

RETRYABLE_SQLSTATE = "40001"  # serialization failure — Postgres asks the client to retry
MAX_RETRIES = 5


def connect() -> psycopg.Connection:
    """Open a connection whose transactions run at SERIALIZABLE isolation.

    Postgres defaults to READ COMMITTED, under which two agents learning
    contradictory lessons at the same instant can both commit and leave the
    tribe with a split brain. Setting the isolation level here (rather than
    per-statement) means every transaction opened through this module gets
    the guarantee, so a forgotten SET TRANSACTION can't silently downgrade it.
    """
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    conn = psycopg.connect(config.DATABASE_URL, application_name="tributary")
    conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
    return conn


def run_txn(fn, retries: int = MAX_RETRIES):
    """Run `fn(cursor)` inside a serializable transaction, retrying on 40001.

    Under SERIALIZABLE, concurrent conflicting writes surface as a retryable
    serialization failure rather than silent lost updates. This is the
    property Tributary leans on for conflict-safe shared memory.
    """
    from tributary import telemetry

    last_err = None
    with telemetry.span("db.txn") as sp:
        for attempt in range(retries):
            try:
                with connect() as conn:
                    with conn.cursor() as cur:
                        result = fn(cur)
                    conn.commit()
                    # Serializable retries are the headline cost of the
                    # conflict-safety guarantee — surface the count as a span
                    # attribute so it's visible in traces under contention.
                    sp.set_attribute("db.txn.attempts", attempt + 1)
                    if attempt:
                        logger.info("txn committed after serialization retries",
                                    attempts=attempt + 1)
                    return result
            except psycopg.Error as e:
                # SerializationFailure is the usual 40001; some drivers/paths
                # surface it as a plain psycopg.Error with the same SQLSTATE.
                if getattr(e, "sqlstate", None) != RETRYABLE_SQLSTATE:
                    raise
                last_err = e
                backoff = 0.05 * (2**attempt)
                logger.warning("serialization conflict (40001), retrying",
                               attempt=attempt + 1, of=retries,
                               backoff_ms=int(backoff * 1000))
                time.sleep(backoff)  # brief backoff, then retry
        sp.set_attribute("db.txn.attempts", retries)
        sp.set_attribute("db.txn.exhausted", True)
    logger.error("txn gave up after serialization retries", attempts=retries)
    raise last_err


def run_read_committed(fn):
    """Run `fn(cursor)` in one READ COMMITTED transaction.

    Only for bookkeeping writes that don't need serializability, such as
    usage counters (increments commute) and append-only audit rows. Nothing
    here can raise 40001, so there is no retry loop; a caller that locks
    several rows must lock them in a fixed order to avoid deadlocks.
    """
    with connect() as conn:
        conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        with conn.cursor() as cur:
            result = fn(cur)
        conn.commit()
        return result


def run_readonly(sql: str, params=()) -> list[tuple]:
    """Run a single read-only statement on an autocommit connection.

    Used for reads that don't need to participate in a write transaction
    (candidate lookups that feed the classifier, time-travel queries,
    dashboard stats), so they never hold serializable predicate locks.
    """
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def vec_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector literal string ('[x,y,z]')."""
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


def _with_database(url: str, dbname: str) -> str:
    """Return `url` pointing at `dbname` instead of its current database."""
    base, _, query = url.partition("?")
    server, _, _ = base.rpartition("/")
    return f"{server}/{dbname}" + (f"?{query}" if query else "")


def ensure_database(url: str, dbname: str) -> str:
    """Create `dbname` on the server behind `url` if missing; return a URL
    for it. Postgres has no CREATE DATABASE IF NOT EXISTS, so check first.
    Used by the test and eval harnesses to get an isolated database."""
    if not dbname.replace("_", "").isalnum():
        raise ValueError(f"unsafe database name: {dbname!r}")
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')
    return _with_database(url, dbname)


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements on ';', honouring $$-quoted bodies
    (DO blocks, function bodies), single-quoted strings and `--` comments."""
    statements, buf = [], []
    i, n = 0, len(sql)
    in_dollar = in_quote = False
    while i < n:
        ch = sql[i]
        if in_dollar:
            if sql.startswith("$$", i):
                buf.append("$$"); i += 2; in_dollar = False
            else:
                buf.append(ch); i += 1
        elif in_quote:
            buf.append(ch); i += 1
            if ch == "'":
                in_quote = False
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j  # drop the comment, keep the newline
        elif sql.startswith("$$", i):
            buf.append("$$"); i += 2; in_dollar = True
        elif ch == "'":
            buf.append(ch); i += 1; in_quote = True
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []; i += 1
        else:
            buf.append(ch); i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def init_schema() -> None:
    """Apply schema.sql statement by statement (autocommit).

    Statements run individually so idempotent migrations (ALTER TYPE ... ADD
    VALUE IF NOT EXISTS, ALTER TABLE ... ADD COLUMN IF NOT EXISTS) can be
    used by statements later in the same file — Postgres won't let a new
    enum value be referenced inside the transaction that added it.
    """
    raw = (Path(__file__).parent / "schema.sql").read_text()
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in split_statements(raw):
                cur.execute(stmt)
