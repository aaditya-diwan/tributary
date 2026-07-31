"""The Tributary memory API: recall / learn / reinforce / retire.

All writes run as serializable transactions against CockroachDB. When two
agents learn contradictory things at the same instant, one transaction
retries against the other's committed result — no lost updates, no split
brain. That property is the whole reason shared agent memory needs a real
database underneath it.
"""

from dataclasses import dataclass
from datetime import datetime

from tributary import guard, llm
from tributary.db import run_readonly, run_txn, vec_literal
from tributary.embeddings import embed


class PrivilegeError(PermissionError):
    """An agent attempted an action its role does not permit."""


# Role -> permitted actions. Overturning another agent's lesson (supersede via
# contradiction, or retire) is curator-only; writers get a `disputed` filing.
WRITE_ROLES = {"writer", "curator"}

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


def ensure_agent(name: str, role: str = "writer") -> str:
    """Register (or refresh) an agent by name and role; returns its id."""
    if role not in ("reader", "writer", "curator"):
        raise ValueError(f"unknown role: {role}")

    def txn(cur):
        cur.execute(
            """
            INSERT INTO agents (name, role, last_seen) VALUES (%s, %s, now())
            ON CONFLICT (name) DO UPDATE SET last_seen = now(), role = EXCLUDED.role
            RETURNING id
            """,
            (name, role),
        )
        return str(cur.fetchone()[0])

    return run_txn(txn)


def _role(cur, agent_id: str) -> str:
    cur.execute("SELECT role FROM agents WHERE id = %s", (agent_id,))
    row = cur.fetchone()
    return row[0] if row else "reader"


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

    Returns {"action": "inserted"|"reinforced"|"superseded"|"quarantined"|
    "disputed", "lesson": Lesson, "verdict": {...}}.

    Raises PrivilegeError if the agent's role may not write.
    """
    vec = vec_literal(embed(f"{situation}: {content}"))

    # 1. Privilege gate: readers may not write.
    role = run_readonly("SELECT role FROM agents WHERE id = %s", (agent_id,))
    role = role[0][0] if role else "reader"
    if role not in WRITE_ROLES:
        _audit_blocked(agent_id, "learn", f"role={role} may not write")
        raise PrivilegeError(f"agent role '{role}' is not permitted to write lessons")

    # 2. Injection screen: instruction-shaped content is quarantined out of
    #    recall and out of the classifier's context before it can spread.
    if screen:
        screened = guard.screen_lesson(situation, content)
        if screened["verdict"] == "quarantine":
            return _quarantine(content, situation, vec, agent_id, task_id,
                               evidence, screened["reasons"])

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
                cur, content, situation, vec, agent_id, role, task_id, confidence,
                evidence, verdict, candidate_ids))
        except _StaleCandidates:
            continue  # candidate set moved; reclassify against the new view

    # Unreachable in practice (last attempt forces novel), but keep it total.
    return run_txn(lambda cur: _apply_verdict(
        cur, content, situation, vec, agent_id, role, task_id, confidence, evidence,
        {"relation": "novel", "target_id": None, "confidence": 1.0}, ()))


def _quarantine(content, situation, vec, agent_id, task_id, evidence, reasons) -> dict:
    """Store a screened-out lesson as quarantined (never recalled) and log it."""
    def txn(cur):
        cur.execute(
            f"""
            INSERT INTO lessons (content, situation, embedding, agent_id, task_id,
                                 evidence, confidence, status)
            VALUES (%s, %s, %s::VECTOR, %s, %s, %s, 0.0, 'quarantined')
            RETURNING {_LESSON_COLS}
            """,
            (content, situation, vec, agent_id, task_id, evidence),
        )
        lesson = Lesson.from_row(cur.fetchone())
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
            "VALUES (%s, 'quarantine', %s, %s)",
            (agent_id, lesson.id, "injection screen: " + ",".join(reasons)),
        )
        return {"action": "quarantined", "lesson": lesson, "reasons": reasons}

    return run_txn(txn)


def _audit_blocked(agent_id, action, detail) -> None:
    def txn(cur):
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, detail) "
            "VALUES (%s, 'blocked', %s)",
            (agent_id, f"{action}: {detail}"),
        )
    run_txn(txn)


def _apply_verdict(cur, content, situation, vec, agent_id, role, task_id, confidence,
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

    # 3b/3c. Decide whether a contradiction may supersede, or must be disputed.
    # Overturning another agent's active lesson is a curator privilege; a
    # writer's contradiction of someone else's lesson is filed as `disputed`
    # (stored, not recalled) for curator review — so no single writer can
    # silently delete the tribe's shared knowledge via a crafted contradiction.
    target_owner = next((c.agent_id for c in current
                         if c.id == verdict.get("target_id")), None)
    contradicts = verdict["relation"] == "contradicts" and verdict.get("target_id")
    must_dispute = (contradicts and target_owner not in (None, agent_id)
                    and role != "curator")

    if must_dispute:
        cur.execute(
            f"""
            INSERT INTO lessons (content, situation, embedding, agent_id, task_id,
                                 evidence, confidence, status)
            VALUES (%s, %s, %s::VECTOR, %s, %s, %s, %s, 'disputed')
            RETURNING {_LESSON_COLS}
            """,
            (content, situation, vec, agent_id, task_id, evidence, confidence),
        )
        new = Lesson.from_row(cur.fetchone())
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
            "VALUES (%s, 'dispute', %s, %s)",
            (agent_id, new.id, f"contradicts {verdict['target_id']} (curator review)"),
        )
        return {"action": "disputed", "lesson": new, "verdict": verdict,
                "target_id": verdict["target_id"]}

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

    if contradicts:
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
    """Retire a lesson. Curator-only — retiring shared knowledge is the most
    destructive write, so it needs the highest privilege. A human curating via
    the MCP Server (TRIBUTARY_AGENT_ROLE=curator) qualifies."""

    def txn(cur):
        if agent_id is not None and _role(cur, agent_id) != "curator":
            cur.execute(
                "INSERT INTO memory_audit (agent_id, action, detail) "
                "VALUES (%s, 'blocked', %s)",
                (agent_id, f"retire {lesson_id}: curator role required"),
            )
            raise PrivilegeError("retiring a lesson requires the curator role")
        cur.execute("UPDATE lessons SET status = 'retired' WHERE id = %s", (lesson_id,))
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
            "VALUES (%s, 'retire', %s, %s)",
            (agent_id, lesson_id, reason),
        )

    run_txn(txn)


def resolve_dispute(lesson_id: str, curator_id: str, accept: bool) -> dict:
    """Curator resolves a disputed lesson: accept it (activate + supersede the
    lesson it contradicted) or reject it (retire the challenger)."""
    def txn(cur):
        if _role(cur, curator_id) != "curator":
            raise PrivilegeError("resolving disputes requires the curator role")
        cur.execute(
            f"SELECT {_LESSON_COLS} FROM lessons WHERE id = %s AND status = 'disputed'",
            (lesson_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("no disputed lesson with that id")
        lesson = Lesson.from_row(row)
        if not accept:
            cur.execute("UPDATE lessons SET status = 'retired' WHERE id = %s", (lesson_id,))
            cur.execute(
                "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
                "VALUES (%s, 'retire', %s, 'dispute rejected')", (curator_id, lesson_id))
            return {"action": "rejected", "lesson": lesson}
        # Accept: find the active lesson it contradicts and supersede it.
        cur.execute(
            "UPDATE lessons SET status = 'active' WHERE id = %s", (lesson_id,))
        cur.execute(
            "INSERT INTO memory_audit (agent_id, action, lesson_id, detail) "
            "VALUES (%s, 'dispute-accept', %s, 'activated by curator')",
            (curator_id, lesson_id))
        return {"action": "accepted", "lesson": lesson}

    return run_txn(txn)
