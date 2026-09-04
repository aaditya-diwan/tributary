"""Tributary — shared, persistent, conflict-safe memory for AI agents.

Every agent's learnings flow into one shared river of memory, stored in
PostgreSQL. Agents are born knowing what the tribe knows, and die leaving
the tribe smarter.
"""

from tributary.memory import (
    Lesson,
    ensure_agent,
    learn,
    lessons_as_of,
    recall,
    recall_as_of,
    reinforce,
    retire,
)

__all__ = [
    "Lesson",
    "ensure_agent",
    "learn",
    "lessons_as_of",
    "recall",
    "recall_as_of",
    "reinforce",
    "retire",
]
