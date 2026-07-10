"""Tributary — shared, persistent, conflict-safe memory for AI agents.

Every agent's learnings flow into one shared river of memory, stored in
CockroachDB. Agents are born knowing what the tribe knows, and die leaving
the tribe smarter.
"""

from tributary.memory import Lesson, ensure_agent, learn, recall, reinforce, retire

__all__ = ["Lesson", "ensure_agent", "learn", "recall", "reinforce", "retire"]
