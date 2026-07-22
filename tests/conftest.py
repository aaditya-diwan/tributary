"""Route the whole test session at a dedicated `tributary_test` database.

The tests run with TRIBUTARY_OFFLINE=1, so their lessons carry deterministic
fake embeddings — meaningless noise in the real vector space. Isolating them
here means `pytest` can never contaminate the demo/production memory tables
(learned the hard way: leftover fixture lessons gave agent-a a head start and
flattened the A-then-B demo comparison).
"""

import os

import pytest

os.environ.setdefault("TRIBUTARY_OFFLINE", "1")

from tributary import config, db

TEST_DB = "tributary_test"


def _with_database(url: str, dbname: str) -> str:
    base, _, query = url.partition("?")
    server, _, _ = base.rpartition("/")
    return f"{server}/{dbname}" + (f"?{query}" if query else "")


@pytest.fixture(scope="session", autouse=True)
def test_database():
    if not config.DATABASE_URL:
        yield  # individual tests skip themselves via their skipif marker
        return
    original = config.DATABASE_URL
    import psycopg

    with psycopg.connect(original, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE IF NOT EXISTS {TEST_DB}")
    config.DATABASE_URL = _with_database(original, TEST_DB)
    db.init_schema()
    yield
    config.DATABASE_URL = original
