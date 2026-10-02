"""Turning failures into golden eval rows.

The flywheel: something in production signals a likely mistake, the inputs
are snapshotted into `golden_candidates`, a person reviews the candidate
(`python -m evals.review`), and accepted ones are appended to the golden
files that the eval harness scores. A mistake the system made once becomes a
regression test it has to keep passing.

Signals that capture a candidate:

    escalation-overrule       the strong classifier tier overruled the cheap one
                              (llm.classify_lesson); label proposed by a model
    reported                  someone called report_mistake on a decision
    retired-as-injection      a curator retired a lesson as an injection that
                              the screen let through
    released-from-quarantine  a curator released a lesson the screen wrongly
                              quarantined

Nothing reaches a golden file without review: a model's label can be wrong,
and lesson text can hold secrets that must not be committed. Capture is
best-effort like cost logging; it never breaks the operation that triggered it.

TRIBUTARY_GOLDEN_CAPTURE=0 turns capture off. The eval harness sets it: a
tiered live eval escalates on golden cases, and capturing those overrules
would queue copies of rows that are already golden.

Dedup is by inputs, not label, so a later signal about the same case (say, a
lesson released as benign and later retired as an injection) is dropped with
a debug line. If the first candidate is still pending, review it with that in
mind; if it was already accepted, edit the golden row by hand.
"""

import hashlib
import json
import os
import re

from tributary import config, log

logger = log.get_logger(__name__)
_warned = False

# Golden files, relative to the golden directory.
CLASSIFICATION_FILE = "classification.jsonl"
REDTEAM_FILE = "redteam.jsonl"            # offline tier: regex only, CI-gated
REDTEAM_LIVE_FILE = "redteam_live.jsonl"  # live tier only: attacks regex misses


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def classification_payload(situation: str, content: str, existing: list[dict],
                           relation: str, target_id: str | None) -> dict:
    """Inputs and proposed label for a classification case. `existing` items
    are {"id", "situation", "content"} snapshots of the candidate lessons."""
    return {
        "new": {"situation": situation, "content": content},
        "existing": [{"id": e["id"], "situation": e["situation"], "content": e["content"]}
                     for e in existing],
        "expected": {"relation": relation, "target": target_id},
    }


def screen_payload(situation: str, content: str, expect_blocked: bool) -> dict:
    return {"situation": situation, "content": content, "expect_blocked": expect_blocked}


def fingerprint(kind: str, payload: dict) -> str:
    """Same inputs, same test case. A classification case is the new lesson
    *and* the candidates it was judged against: the same lesson against a
    different candidate set is a different case. The label is left out, so
    two conflicting reports of one case collide and get reviewed once."""
    if kind == "classification":
        parts = [_norm(payload["new"]["situation"]), _norm(payload["new"]["content"])]
        parts += sorted(_norm(f"{e['situation']}|{e['content']}") for e in payload["existing"])
    else:
        parts = [_norm(payload["situation"]), _norm(payload["content"])]
    return hashlib.sha256("\x1f".join([kind, *parts]).encode("utf-8")).hexdigest()


def capture(kind: str, source: str, payload: dict, got: dict | None = None,
            note: str | None = None, decision_id: str | None = None,
            reported_by: str | None = None) -> str | None:
    """Queue a candidate for review. Returns its id, or None if this case is
    already queued (or capture failed, which is logged, never raised)."""
    global _warned
    if not config.DATABASE_URL or os.environ.get("TRIBUTARY_GOLDEN_CAPTURE") in ("0", "false"):
        return None
    fp = fingerprint(kind, payload)
    try:
        from tributary.db import run_txn

        def txn(cur):
            cur.execute(
                """
                INSERT INTO golden_candidates
                    (kind, source, fingerprint, payload, got, note, decision_id, reported_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint) DO NOTHING
                RETURNING id::TEXT
                """,
                (kind, source, fp, json.dumps(payload), json.dumps(got) if got else None,
                 note, decision_id, reported_by),
            )
            row = cur.fetchone()
            return row[0] if row else None

        cid = run_txn(txn)
    except Exception as e:
        (logger.debug if _warned else logger.warning)(
            "golden candidate capture failed", kind=kind, source=source,
            error=log.preview(e, 200))
        _warned = True
        return None
    if cid:
        logger.info("golden candidate captured", kind=kind, source=source, candidate=cid)
    else:
        logger.debug("golden candidate already queued", kind=kind, source=source)
    return cid


def pending() -> list[dict]:
    from tributary.db import run_readonly

    rows = run_readonly(
        "SELECT id::TEXT, kind, source, payload, got, note, decision_id::TEXT, created_at "
        "FROM golden_candidates WHERE status = 'pending' ORDER BY created_at")
    return [{"id": r[0], "kind": r[1], "source": r[2], "payload": r[3], "got": r[4],
             "note": r[5], "decision_id": r[6], "created_at": str(r[7])} for r in rows]


def get(candidate_id: str) -> dict | None:
    from tributary.db import run_readonly

    rows = run_readonly(
        "SELECT id::TEXT, kind, source, payload, got, note, decision_id::TEXT, status "
        "FROM golden_candidates WHERE id::TEXT = %s", (candidate_id,))
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "kind": r[1], "source": r[2], "payload": r[3], "got": r[4],
            "note": r[5], "decision_id": r[6], "status": r[7]}


def mark(candidate_id: str, status: str, golden_id: str | None = None) -> None:
    from tributary.db import run_txn

    def txn(cur):
        cur.execute(
            "UPDATE golden_candidates SET status = %s, golden_id = %s, reviewed_at = now() "
            "WHERE id::TEXT = %s", (status, golden_id, candidate_id))
    run_txn(txn)


def route(kind: str, payload: dict) -> str:
    """Which golden file a reviewed case belongs in.

    Only one combination needs care: an attack the regex layer misses. The
    offline tier scores the screen with regex alone and CI gates on its block
    rate, so that row would fail CI for doing its job. It goes to the
    live-only file. A benign lesson the regex blocks stays in the offline file
    on purpose: it is a real regex false positive, the false-positive rate is
    reported but not gated, and seeing it there is the point.
    """
    if kind == "classification":
        return CLASSIFICATION_FILE
    if payload["expect_blocked"]:
        from tributary import guard

        caught = guard.screen_lesson(payload["situation"], payload["content"],
                                     use_llm=False)["verdict"] == "quarantine"
        return REDTEAM_FILE if caught else REDTEAM_LIVE_FILE
    return REDTEAM_FILE


def to_golden_row(candidate: dict, golden_id: str) -> dict:
    """A candidate in its golden file's row format. Real lesson ids become
    e1..eN, the way hand-written rows name their existing lessons."""
    p, source = candidate["payload"], candidate["source"]
    if candidate["kind"] == "classification":
        ids = {e["id"]: f"e{i}" for i, e in enumerate(p["existing"], 1)}
        target = p["expected"]["target"]
        return {
            "id": golden_id,
            "category": f"captured:{source}",
            "difficulty": "unrated",
            "source": source,
            "existing": [{"id": ids[e["id"]], "situation": e["situation"],
                          "content": e["content"]} for e in p["existing"]],
            "new": {"situation": p["new"]["situation"], "content": p["new"]["content"]},
            "expected": {"relation": p["expected"]["relation"],
                         "target": ids.get(target) if target else None},
        }
    return {
        "id": golden_id,
        "attack": f"captured:{source}" if p["expect_blocked"] else "none",
        "source": source,
        "situation": p["situation"],
        "content": p["content"],
        "expect_blocked": p["expect_blocked"],
    }


def next_golden_id(path, prefix: str) -> str:
    """`prefix-NN`, one past the highest existing id with that prefix."""
    nums = [0]
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = re.fullmatch(re.escape(prefix) + r"-(\d+)", json.loads(line).get("id", ""))
                if m:
                    nums.append(int(m.group(1)))
    return f"{prefix}-{max(nums) + 1:02d}"


def append_row(path, row: dict) -> None:
    """Append one JSON line, keeping the file LF-terminated."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_bytes((text + json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))


def accept(candidate: dict, golden_dir) -> tuple[str, str]:
    """Write a reviewed candidate to its golden file. Returns (file, golden id)."""
    from pathlib import Path

    golden_dir = Path(golden_dir)
    fname = route(candidate["kind"], candidate["payload"])
    # One prefix per file, so ids stay unique within each suite's case list.
    prefix = {CLASSIFICATION_FILE: "cap-cls", REDTEAM_FILE: "cap-rt",
              REDTEAM_LIVE_FILE: "cap-live"}[fname]
    path = golden_dir / fname
    gid = next_golden_id(path, prefix)
    append_row(path, to_golden_row(candidate, gid))
    mark(candidate["id"], "accepted", gid)
    logger.info("golden row added", candidate=candidate["id"], file=fname, golden_id=gid)
    return fname, gid
