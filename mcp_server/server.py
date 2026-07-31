"""Tributary MCP server — plug ANY agent into the tribe's shared memory.

One config line gives Claude Code, Cursor, or any MCP-compatible client
recall/learn access to the same CockroachDB-backed memory the autonomous
agents use. Two developers' coding agents share lessons instantly.

Add to Claude Code:

    claude mcp add tributary -e DATABASE_URL=<your-crdb-url> \
        -e TRIBUTARY_AGENT_NAME=alice-claude-code \
        -- python -m mcp_server.server

Identity comes from TRIBUTARY_AGENT_NAME (default: "mcp-agent"), so lessons
are attributed to whoever learned them.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

from tributary import memory
from tributary.db import run_readonly

mcp = FastMCP("tributary")

_agent_id = None


def _me() -> str:
    global _agent_id
    if _agent_id is None:
        _agent_id = memory.ensure_agent(
            os.environ.get("TRIBUTARY_AGENT_NAME", "mcp-agent"),
            role=os.environ.get("TRIBUTARY_AGENT_ROLE", "writer"),
        )
    return _agent_id


def _lesson_dict(l: memory.Lesson) -> dict:
    return {"id": l.id, "situation": l.situation, "content": l.content,
            "confidence": l.confidence, "times_helpful": l.times_helpful,
            "created_at": l.created_at}


@mcp.tool()
def tribal_recall(query: str, k: int = 5) -> str:
    """Search the tribe's shared memory for lessons relevant to a situation.
    Call this BEFORE attempting anything another agent may have done before
    (deploys, builds, debugging known systems). Returns lessons with ids —
    if one helps you, call tribal_reinforce with its id."""
    hits = memory.recall(query, agent_id=_me(), k=k)
    if not hits:
        return "The tribe has no lessons for this yet. If you learn something, share it with tribal_learn."
    return json.dumps([_lesson_dict(l) for l in hits], indent=2)


@mcp.tool()
def tribal_learn(content: str, situation: str, evidence: str = "") -> str:
    """Share a lesson with the tribe so no agent ever re-learns it. Use after
    solving something non-obvious. `situation` = when the lesson applies
    (e.g. "deploying via the internal deploy API"); `content` = one crisp
    sentence; `evidence` = what happened that taught it. Duplicates reinforce
    the existing lesson; contradictions supersede it transactionally.
    Instruction-shaped content is quarantined; contradicting another agent's
    lesson is filed for curator review unless you are a curator."""
    try:
        out = memory.learn(content, situation, _me(), evidence=evidence)
    except memory.PrivilegeError as e:
        return json.dumps({"error": str(e)})
    result = {"action": out["action"], "lesson": _lesson_dict(out["lesson"])}
    if out.get("reasons"):
        result["quarantine_reasons"] = out["reasons"]
    return json.dumps(result, indent=2)


@mcp.tool()
def tribal_reinforce(lesson_id: str) -> str:
    """Mark a recalled lesson as having actually helped you. This raises its
    confidence so the tribe trusts it more."""
    memory.reinforce(lesson_id, _me())
    return f"Reinforced {lesson_id}."


@mcp.tool()
def tribal_retire(lesson_id: str, reason: str = "") -> str:
    """Retire an outdated or wrong lesson so the tribe stops using it."""
    memory.retire(lesson_id, _me(), reason=reason)
    return f"Retired {lesson_id}."


@mcp.tool()
def tribal_recall_as_of(query: str, timestamp: str, k: int = 5) -> str:
    """Time-travel: what would the tribe have recalled for this query at a
    past instant? `timestamp` is ISO format (e.g. 2026-07-10T15:42:00).
    Uses CockroachDB AS OF SYSTEM TIME — no snapshots needed. Great for
    forensics: 'what did the agents believe when that decision was made?'"""
    hits = memory.recall_as_of(query, timestamp, k=k)
    return json.dumps([_lesson_dict(l) for l in hits], indent=2) if hits else \
        f"The tribe knew nothing relevant at {timestamp}."


@mcp.tool()
def tribal_stats() -> str:
    """Overview of the tribe's memory: lesson counts, top contributors,
    recent conflict resolutions."""
    counts = run_readonly(
        "SELECT status::STRING, count(*) FROM lessons GROUP BY status"
    )
    contributors = run_readonly(
        """
        SELECT ag.name, count(*), COALESCE(sum(l.times_helpful), 0)
        FROM lessons l JOIN agents ag ON ag.id = l.agent_id
        GROUP BY ag.name ORDER BY 3 DESC LIMIT 5
        """
    )
    return json.dumps({
        "lessons_by_status": {r[0]: r[1] for r in counts},
        "top_contributors": [
            {"agent": r[0], "lessons": r[1], "times_helpful": r[2]}
            for r in contributors
        ],
    }, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
