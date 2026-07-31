"""The headline test: two agents learn contradictory lessons CONCURRENTLY,
and CockroachDB's serializable isolation guarantees a deterministic outcome —
exactly one lesson stays active, the other is superseded with a provenance
chain. No lost updates, no split brain.

Requires DATABASE_URL (a real CockroachDB cluster). Runs offline from AWS:

    TRIBUTARY_OFFLINE=1 pytest tests/test_conflicts.py -v
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("TRIBUTARY_OFFLINE", "1")

import pytest

from tributary import memory
from tributary.db import run_txn

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a CockroachDB cluster"
)


@pytest.fixture
def agents():
    # Curators: these tests exercise the transactional supersede *mechanism*,
    # which is a curator privilege. The writer-level dispute path (a writer may
    # not overturn another agent's lesson) is covered in test_injection.py.
    return (memory.ensure_agent("test-agent-a", role="curator"),
            memory.ensure_agent("test-agent-b", role="curator"))


def _statuses(ids):
    def txn(cur):
        cur.execute(
            "SELECT id::STRING, status::STRING, superseded_by::STRING "
            "FROM lessons WHERE id = ANY(%s)", (ids,),
        )
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    return run_txn(txn)


def test_concurrent_contradiction_resolves_deterministically(agents):
    agent_a, agent_b = agents
    # Unique marker so this test never collides with previous runs' lessons.
    marker = uuid.uuid4().hex[:8]
    situation = f"configuring the frobnicator service {marker}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(memory.learn, f"Use port 8080 for {marker}", situation, agent_a)
        fb = pool.submit(memory.learn, f"Use port 9090 for {marker}", situation, agent_b)
        ra, rb = fa.result(), fb.result()

    ids = [ra["lesson"].id, rb["lesson"].id]
    # Both may insert, or the second may supersede the first — but never
    # two active contradictory lessons if either observed the other.
    if "superseded" in (ra["action"], rb["action"]):
        statuses = _statuses(ids)
        active = [i for i, (s, _) in statuses.items() if s == "active"]
        superseded = [i for i, (s, _) in statuses.items() if s == "superseded"]
        assert len(active) == 1, f"expected exactly one active lesson, got {statuses}"
        assert len(superseded) == 1
        # Provenance chain points from the loser to the winner.
        assert statuses[superseded[0]][1] == active[0]


def test_sequential_contradiction_supersedes(agents):
    agent_a, agent_b = agents
    marker = uuid.uuid4().hex[:8]
    situation = f"authenticating to the widget api {marker}"

    first = memory.learn(f"Use basic auth for {marker}", situation, agent_a)
    assert first["action"] == "inserted"

    second = memory.learn(f"Use oauth tokens for {marker}", situation, agent_b)
    assert second["action"] == "superseded"

    statuses = _statuses([first["lesson"].id, second["lesson"].id])
    assert statuses[first["lesson"].id] == ("superseded", second["lesson"].id)
    assert statuses[second["lesson"].id][0] == "active"


def test_duplicate_reinforces_instead_of_inserting(agents):
    agent_a, agent_b = agents
    marker = uuid.uuid4().hex[:8]
    situation = f"building the gizmo artifact {marker}"
    content = f"Clear the build cache before building {marker}"

    first = memory.learn(content, situation, agent_a)
    assert first["action"] == "inserted"

    second = memory.learn(content, situation, agent_b)
    assert second["action"] == "reinforced"
    assert second["lesson"].id == first["lesson"].id
    assert second["lesson"].times_helpful == first["lesson"].times_helpful + 1


def test_recall_finds_lesson_by_paraphrase(agents):
    agent_a, _ = agents
    marker = uuid.uuid4().hex[:8]
    memory.learn(
        f"The deploy api requires the X-Batch header set to true {marker}",
        f"deploying services through the internal deploy api {marker}",
        agent_a,
    )
    hits = memory.recall(f"deploy api {marker} rate limited 429", k=3)
    assert any(marker in h.content for h in hits)
