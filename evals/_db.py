"""Isolated eval database, mirroring tests/conftest.py.

Eval suites that touch the database (retrieval, e2e) run in a dedicated
`tributary_eval` database so golden fixtures — which in offline mode carry
fake hash embeddings — never contaminate real tribal memory.
"""

import psycopg

EVAL_DB = "tributary_eval"


def _with_database(url: str, dbname: str) -> str:
    base, _, query = url.partition("?")
    server, _, _ = base.rpartition("/")
    return f"{server}/{dbname}" + (f"?{query}" if query else "")


def use_isolated_db() -> None:
    """Point tributary.config at the eval database (created if missing)."""
    from tributary import config, db

    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for DB-backed eval suites")
    if f"/{EVAL_DB}" in config.DATABASE_URL:
        return
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE IF NOT EXISTS {EVAL_DB}")
    config.DATABASE_URL = _with_database(config.DATABASE_URL, EVAL_DB)
    db.init_schema()


def clear_lessons() -> None:
    """Remove all lessons/audit rows so each eval run starts from scratch."""
    from tributary.db import run_txn

    def txn(cur):
        cur.execute("DELETE FROM memory_audit")
        cur.execute("DELETE FROM lessons")

    run_txn(txn)
