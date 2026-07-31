"""Offline tests for the ReAct agent's tool layer and failure modes.

The *decision* to recall is a live-model behavior (covered by the `agent` eval
suite). These tests pin the deterministic machinery around it: memory-tool
dispatch, guardrails, the negative-control task, and chaos-mode determinism.

    TRIBUTARY_OFFLINE=1 pytest tests/test_agent_tools.py -v
"""

import os

os.environ.setdefault("TRIBUTARY_OFFLINE", "1")

import pytest

from agents.tools import MEMORY_TOOL_NAMES, MemoryTools
from gauntlet import Gauntlet
from gauntlet.compute import ComputeTask


def test_compute_task_is_self_contained_and_checkable():
    task = ComputeTask()
    digest = task.execute("sha256", {"text": ComputeTask.ARTIFACT})
    assert digest == task.expected
    task.execute("done", {"answer": digest.upper()})  # case-insensitive
    assert task.succeeded


def test_unknown_memory_tool_returns_error_not_crash():
    mem = MemoryTools("00000000-0000-0000-0000-000000000000")
    assert mem.execute("tribal_teleport", {}).startswith("ERROR")


def test_reinforce_requires_prior_recall():
    mem = MemoryTools("00000000-0000-0000-0000-000000000000")
    # Never recalled this id -> guarded, no DB write attempted.
    out = mem.execute("tribal_reinforce", {"lesson_id": "not-recalled"})
    assert "did not recall" in out
    assert not mem.reinforced


def test_chaos_is_deterministic_with_seed_and_corrupts():
    a = Gauntlet(chaos=1.0, seed=7)
    b = Gauntlet(chaos=1.0, seed=7)
    ra = a.execute("clear_build_cache", {})
    rb = b.execute("clear_build_cache", {})
    assert ra == rb, "seeded chaos must be reproducible"
    assert a.chaos_events == 1, "chaos=1.0 must corrupt a non-done result"
    assert "Cache cleared" not in ra, "result should be garbled, not the real one"


def test_chaos_never_corrupts_done():
    env = Gauntlet(chaos=1.0, seed=1)
    result = env.execute("done", {"summary": "finished"})
    assert result == "Task closed."
    assert env.chaos_events == 0


def test_zero_chaos_is_clean():
    env = Gauntlet(chaos=0.0)
    assert "Cache cleared" in env.execute("clear_build_cache", {})
    assert env.chaos_events == 0


def test_memory_tool_names_cover_specs():
    assert MEMORY_TOOL_NAMES == {"tribal_recall", "tribal_learn", "tribal_reinforce"}
