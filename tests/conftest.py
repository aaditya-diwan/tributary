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


@pytest.fixture(scope="session", autouse=True)
def test_database():
    if not config.DATABASE_URL:
        yield  # individual tests skip themselves via their skipif marker
        return
    original = config.DATABASE_URL
    config.DATABASE_URL = db.ensure_database(original, TEST_DB)
    db.init_schema()
    # Start from an empty tribe every session. Fixtures use unique markers, but
    # the offline heuristic classifier matches on the *other* words, so a lesson
    # left over from a prior run can classify a fresh fixture as a duplicate and
    # make an assertion fail non-deterministically. CI gets a fresh DB anyway;
    # this makes repeated local runs behave identically.
    def _clear(cur):
        cur.execute("DELETE FROM memory_audit")
        cur.execute("DELETE FROM lessons")
        # Candidates dedup by fingerprint, so leftovers would make a rerun's
        # capture look like a duplicate.
        cur.execute("DELETE FROM golden_candidates")
        cur.execute("DELETE FROM decisions")
    db.run_txn(_clear)
    yield
    config.DATABASE_URL = original
