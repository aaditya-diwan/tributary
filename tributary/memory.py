"""The Tributary memory API: recall / learn / reinforce / retire.

All writes run as serializable transactions against CockroachDB. When two
agents learn contradictory things at the same instant, one transaction
retries against the other's committed result — no lost updates, no split
brain. That property is the whole reason shared agent memory needs a real
database underneath it.
"""

from dataclasses import dataclass
from datetime import datetime

from tributary import llm
from tributary.db import run_readonly, run_txn, vec_literal
from tributary.embeddings import embed

# Cosine distance below which an existing lesson is "similar enough" to be a
# candidate duplicate/contradiction for a new lesson.
SIMILARITY_GATE = 0.45

# How many candidate lessons the classifier sees.
CANDIDATE_K = 3

# If the candidate set shifts between classifying (outside the txn) and
# committing (inside it), we reclassify. Cap the reclassify loop; on the last
# attempt we fall back to a safe novel-insert rather than acting on a verdict
# computed against a stale view.
MAX_CLASSIFY_ATTEMPTS = 3


class _StaleCandidates(Exception):
    """The similar-lesson set changed under us; the verdict must be recomputed."""


@dataclass
class Lesson:
    id: str
    content: str
    situation: str
    agent_id: str
    confidence: float
    times_helpful: int
    created_at: str
    distance: float | None = None  # populated on recall

    @classmethod
    def from_row(cls, row) -> "Lesson":
        return cls(
            id=str(row[0]),
            content=row[1],
            situation=row[2],
            agent_id=str(row[3]),
            confidence=row[4],
            times_helpful=row[5],
            created_at=str(row[6]),
            distance=row[7] if len(row) > 7 else None,
        )


_LESSON_COLS = "id, content, situation, agent_id, confidence, times_helpful, created_at"


def ensure_agent(name: str) -> str:
    """Register (or refresh) an agent by name; returns its id."""

    def txn(cur):
        cur.execute(
            """
            INSERT INTO agents (name, last_seen) VALUES (%s, now())
            ON CONFLICT (name) DO UPDATE SET last_seen = now()
            RETURNING id
            """,
            (name,),
        )
        return str(cur.fetchone()[0])

    return run_txn(txn)


def recall(query: str, agent_id: str | None = None, k: int = 5,
           min_confidence: float = 0.3) -> list[Lesson]:
    """Semantic search over the tribe's active lessons."""
    qvec = vec_literal(embed(query))

    def txn(cur):
        cur.execute(
            f"""
            SELECT {_LESSON_COLS}, embedding <=> %s::VECTOR AS distance
            FROM lessons
            WHERE status = 'active' AND confidence >= %s
            ORDER BY embedding <=> %s::VECTOR
            LIMIT %s
            """,
            (qvec, min_confidence, qvec, k),
        )
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        if ids:
            cur.execute(
                "UPDATE lessons SET times_recalled = times_recalled + 1, "
                "last_used_at = now() WHERE id = ANY(%s)",
                (ids,),
            )
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, detail) VALUES (%s, 'recall', %s)",
            (agent_id, f"{query[:200]} -> {len(rows)} hits"),
        )
        return [Lesson.from_row(r) for r in rows]

    return run_txn(txn)


def _as_of_clause(timestamp: str | datetime) -> str:
    """Validate a timestamp and render an AS OF SYSTEM TIME clause.

    CockroachDB can read any table as it existed at a past instant — no
    snapshots, no extra tables. Tributary uses it for time-travel memory:
    "what did the tribe believe at 3:42pm yesterday?"
    """
    ts = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp)
    return f"AS OF SYSTEM TIME '{ts.isoformat()}'"


def recall_as_of(query: str, timestamp: str | datetime, k: int = 5) -> list[Lesson]:
    """What would recall() have returned at `timestamp`? Read-only; leaves
    no trace in usage counters — this is forensics, not memory access."""
    qvec = vec_literal(embed(query))
    rows = run_readonly(
        f"""
        SELECT {_LESSON_COLS}, embedding <=> %s::VECTOR AS distance
        FROM lessons {_as_of_clause(timestamp)}
        WHERE status = 'active'
        ORDER BY embedding <=> %s::VECTOR
        LIMIT %s
        """,
        (qvec, qvec, k),
    )
    return [Lesson.from_row(r) for r in rows]


def lessons_as_of(timestamp: str | datetime, limit: int = 200) -> list[Lesson]:
    """The tribe's full active belief set as it existed at `timestamp`."""
    rows = run_readonly(
        f"""
        SELECT {_LESSON_COLS}
        FROM lessons {_as_of_clause(timestamp)}
        WHERE status = 'active'
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [Lesson.from_row(r) for r in rows]


def _fetch_candidates(vec: str) -> list[Lesson]:
    """Similar active lessons, read outside any write transaction.

    This read feeds the (slow) LLM classifier. The verdict it produces is
    re-validated inside the write transaction before we act on it, so a
    concurrent write between this read and the commit can never cause a
    wrong supersede — at worst it forces a reclassification.
    """
    rows = run_readonly(
        f"""
        SELECT {_LESSON_COLS}, embedding <=> %s::VECTOR AS distance
        FROM lessons
        WHERE status = 'active'
        ORDER BY embedding <=> %s::VECTOR
        LIMIT %s
        """,
        (vec, vec, CANDIDATE_K),
    )
    return [Lesson.from_row(r) for r in rows if r[7] is not None and r[7] < SIMILARITY_GATE]


def learn(content: str, situation: str, agent_id: str, evidence: str = "",
          task_id: str | None = None, confidence: float = 0.6,
          screen: bool = True) -> dict:
    """Write a lesson to the shared memory, resolving conflicts transactionally.

    The expensive LLM classification runs *outside* the serializable
    transaction; the short transaction only re-checks the candidate set and
    applies the verdict. Keeping a 120s subprocess out of the transaction
    slashes the window in which concurrent writers contend (and thus the
    40001 retry rate) without giving up the conflict-safety guarantee — the
    in-txn re-validation is what preserves it.

    Returns {"action": "inserted"|"reinforced"|"superseded", "lesson": Lesson,
    "verdict": {...}}. Raises if a screening policy rejects the content.
    """
    vec = vec_literal(embed(f"{situation}: {content}"))

    last_verdict = {"relation": "novel", "target_id": None, "confidence": 1.0}
    for attempt in range(MAX_CLASSIFY_ATTEMPTS):
        candidates = _fetch_candidates(vec)
        candidate_ids = tuple(sorted(c.id for c in candidates))

        # Final attempt with a still-contended set: degrade to a safe
        # novel-insert rather than act on a verdict we can't validate.
        force_novel = attempt == MAX_CLASSIFY_ATTEMPTS - 1
        if candidates and not force_novel:
            verdict = llm.classify_lesson(
                situation, content,
                [{"id": c.id, "situation": c.situation, "content": c.content}
                 for c in candidates],
            )
        else:
            verdict = {"relation": "novel", "target_id": None,
                       "confidence": 1.0, "model": "trivial"}
        last_verdict = verdict

        try:
            return run_txn(lambda cur: _apply_verdict(
                cur, content, situation, vec, agent_id, task_id, confidence,
                evidence, verdict, candidate_ids))
        except _StaleCandidates:
            continue  # candidate set moved; reclassify against the new view

    # Unreachable in practice (last attempt forces novel), but keep it total.
    return run_txn(lambda cur: _apply_verdict(
        cur, content, situation, vec, agent_id, task_id, confidence, evidence,
        {"relation": "novel", "target_id": None, "confidence": 1.0}, ()))


def _apply_verdict(cur, content, situation, vec, agent_id, task_id, confidence,
                   evidence, verdict, classified_against: tuple) -> dict:
    """Re-validate the classifier's view, then apply the verdict atomically.

    `classified_against` is the candidate id set the verdict was computed on.
    If the current active candidate set differs, the verdict is stale and we
    bail out to reclassify. This is the guard that lets classification live
    outside the transaction without weakening serializable conflict safety.
    """
    cur.execute(
        f"""
        SELECT {_LESSON_COLS}, embedding <=> %s::VECTOR AS distance
        FROM lessons
        WHERE status = 'active'
        ORDER BY embedding <=> %s::VECTOR
        LIMIT %s
        """,
        (vec, vec, CANDIDATE_K),
    )
    current = [
        Lesson.from_row(r) for r in cur.fetchall()
        if r[7] is not None and r[7] < SIMILARITY_GATE
    ]
    current_ids = tuple(sorted(c.id for c in current))
    if current_ids != classified_against:
        raise _StaleCandidates()

    # 3a. Duplicate -> reinforce the existing lesson instead of inserting.
    if verdict["relation"] == "duplicate":
        cur.execute(
            f"""
            UPDATE lessons
            SET times_helpful = times_helpful + 1,
                confidence = LEAST(confidence + 0.1, 0.99),
                last_used_at = now()
            WHERE id = %s AND status = 'active'
            RETURNING {_LESSON_COLS}
            """,
            (verdict["target_id"],),
        )
        row = cur.fetchone()
        if row is not None:
            existing = Lesson.from_row(row)
            cur.execute(
                "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
                "VALUES (%s, 'reinforce', %s, 'independent rediscovery')",
                (agent_id, existing.id),
            )
            return {"action": "reinforced", "lesson": existing, "verdict": verdict}
        # Target vanished (retired/superseded concurrently) -> insert as novel.

    # 3b/3c. Insert the new lesson; if it contradicts, supersede the old one.
    cur.execute(
        f"""
        INSERT INTO lessons (content, situation, embedding, agent_id, task_id,
                             evidence, confidence)
        VALUES (%s, %s, %s::VECTOR, %s, %s, %s, %s)
        RETURNING {_LESSON_COLS}
        """,
        (content, situation, vec, agent_id, task_id, evidence, confidence),
    )
    new = Lesson.from_row(cur.fetchone())
    action = "inserted"

    if verdict["relation"] == "contradicts":
        # Newer evidence wins; keep the chain for provenance. The WHERE
        # status = 'active' makes the supersede idempotent under a concurrent
        # winner — if someone already superseded it, we simply don't.
        cur.execute(
            "UPDATE lessons SET status = 'superseded', superseded_by = %s "
            "WHERE id = %s AND status = 'active'",
            (new.id, verdict["target_id"]),
        )
        if cur.rowcount:
            cur.execute(
                "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
                "VALUES (%s, 'supersede', %s, %s)",
                (agent_id, verdict["target_id"], f"superseded by {new.id}"),
            )
            action = "superseded"

    cur.execute(
        "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
        "VALUES (%s, 'learn', %s, %s)",
        (agent_id, new.id, content[:200]),
    )
    return {"action": action, "lesson": new, "verdict": verdict}


def reinforce(lesson_id: str, agent_id: str | None = None) -> None:
    """Mark a recalled lesson as having actually helped."""

    def txn(cur):
        cur.execute(
            "UPDATE lessons SET times_helpful = times_helpful + 1, "
            "confidence = LEAST(confidence + 0.05, 0.99) WHERE id = %s",
            (lesson_id,),
        )
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, lesson_id) "
            "VALUES (%s, 'reinforce', %s)",
            (agent_id, lesson_id),
        )

    run_txn(txn)


def retire(lesson_id: str, agent_id: str | None = None, reason: str = "") -> None:
    """Manually retire a lesson (also doable by a human via the MCP Server)."""

    def txn(cur):
        cur.execute("UPDATE lessons SET status = 'retired' WHERE id = %s", (lesson_id,))
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
            "VALUES (%s, 'retire', %s, %s)",
            (agent_id, lesson_id, reason),
        )

    run_txn(txn)
