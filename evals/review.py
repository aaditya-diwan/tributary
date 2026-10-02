"""Review captured failures and turn the good ones into golden eval rows.

    python -m evals.review                     # interactive: go through the queue
    python -m evals.review --list              # just show what's pending
    python -m evals.review --accept ID         # accept with the proposed label
    python -m evals.review --accept ID --relation duplicate --target e2
    python -m evals.review --accept ID --benign   # screen case: should pass
    python -m evals.review --reject ID

Candidates come from tributary/golden.py (escalation overrules, reported
mistakes, injections retired by a curator, lessons released from quarantine).
Accepted rows are appended to evals/golden/, routed by golden.route():
classification cases to classification.jsonl, screen cases to redteam.jsonl,
except attacks the regex layer misses, which go to redteam_live.jsonl so the
regex-only CI gate doesn't fail on them.

Accepted rows are committed to git. Reject or edit anything that contains a
secret or internal detail that shouldn't be.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
GOLDEN_DIR = HERE / "golden"
RELATIONS = ("duplicate", "contradicts", "novel")


def describe(c: dict) -> str:
    p, lines = c["payload"], []
    lines.append(f"[{c['id'][:8]}] {c['kind']} | {c['source']}")
    if c["kind"] == "classification":
        lines.append(f"  NEW  ({p['new']['situation']}): {p['new']['content']}")
        for i, e in enumerate(p["existing"], 1):
            lines.append(f"  e{i}   ({e['situation']}): {e['content']}")
        ids = {e["id"]: f"e{i}" for i, e in enumerate(p["existing"], 1)}
        exp = p["expected"]
        lines.append(f"  proposed: {exp['relation']}"
                     + (f" {ids.get(exp['target'], exp['target'])}" if exp["target"] else ""))
        if c.get("got"):
            g = c["got"]
            lines.append(f"  system said: {g.get('relation')}"
                         + (f" {ids.get(g.get('target_id'), g.get('target_id'))}"
                            if g.get("target_id") else "")
                         + (f" (model {g['model']})" if g.get("model") else ""))
    else:
        lines.append(f"  LESSON ({p['situation']}): {p['content']}")
        lines.append(f"  proposed: {'should be BLOCKED' if p['expect_blocked'] else 'should PASS'}")
        if c.get("got"):
            lines.append(f"  system said: {json.dumps(c['got'])}")
    if c.get("note"):
        lines.append(f"  note: {c['note']}")
    return "\n".join(lines)


def relabel(c: dict, relation=None, target=None, blocked=None) -> dict:
    """Apply a reviewer's correction to the proposed label."""
    p = c["payload"]
    if c["kind"] == "classification" and blocked is not None:
        raise SystemExit("--blocked/--benign apply to screen candidates, not classification")
    if c["kind"] == "screen" and (relation or target):
        raise SystemExit("--relation/--target apply to classification candidates, not screen")
    if c["kind"] == "classification":
        if relation:
            if relation not in RELATIONS:
                raise SystemExit(f"--relation must be one of {RELATIONS}")
            p["expected"]["relation"] = relation
        if p["expected"]["relation"] == "novel":
            p["expected"]["target"] = None
        elif target:
            local = {f"e{i}": e["id"] for i, e in enumerate(p["existing"], 1)}
            real = local.get(target, target)
            if real not in local.values():
                raise SystemExit(f"--target must be one of {sorted(local)}")
            p["expected"]["target"] = real
        if p["expected"]["relation"] != "novel" and not p["expected"]["target"]:
            raise SystemExit("duplicate/contradicts needs a target (e.g. --target e1)")
    elif blocked is not None:
        p["expect_blocked"] = blocked
    return c


def check_baseline() -> None:
    """Classification rows change what the offline (heuristic) tier scores,
    so show the reviewer the effect on the CI gate. Never update it here:
    a baseline change is a deliberate, reviewed act."""
    print("\nRe-running the offline classification suite against the baseline...")
    env = {**os.environ, "TRIBUTARY_OFFLINE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "evals.run_eval", "--tier", "offline",
         "--suite", "classification", "--check-baseline"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    tail = [l for l in (proc.stdout + proc.stderr).splitlines()
            if "accuracy" in l or "baseline" in l or "REGRESSION" in l]
    print("\n".join(tail[-4:]) or proc.stdout[-500:])
    if proc.returncode != 0:
        print("\nThe offline gate would fail. The new rows changed the dataset, not the "
              "code, so if the new number is expected, refresh the baseline in the same "
              "commit:\n    TRIBUTARY_OFFLINE=1 python -m evals.run_eval --tier offline "
              "--update-baseline")


def accept(c: dict, golden_dir: Path) -> str:
    from tributary import golden

    fname, gid = golden.accept(c, golden_dir)
    print(f"  accepted -> {fname} as {gid}")
    return c["kind"]


def reject(c: dict) -> None:
    from tributary import golden

    golden.mark(c["id"], "rejected")
    print("  rejected")


def interactive(golden_dir: Path) -> set:
    from tributary import golden

    queue, kinds = golden.pending(), set()
    if not queue:
        print("Nothing pending.")
        return kinds
    print(f"{len(queue)} pending. Accepted rows are committed to git: reject or edit "
          "anything containing secrets.\n")
    for c in queue:
        print(describe(c))
        while True:
            choice = input("  [a]ccept  [e]dit label  [r]eject  [s]kip  [q]uit > ").strip().lower()
            if choice == "a":
                kinds.add(accept(c, golden_dir))
                break
            if choice == "e":
                if c["kind"] == "classification":
                    rel = input(f"  relation {RELATIONS}: ").strip()
                    tgt = None if rel == "novel" else input("  target (e1, e2, ...): ").strip()
                    relabel(c, relation=rel, target=tgt)
                else:
                    relabel(c, blocked=input("  should it be blocked? [y/n]: ").strip() == "y")
                kinds.add(accept(c, golden_dir))
                break
            if choice == "r":
                reject(c)
                break
            if choice == "s":
                break
            if choice == "q":
                return kinds
        print()
    return kinds


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show pending candidates and exit")
    ap.add_argument("--accept", metavar="ID", help="candidate id (or its first 8 chars)")
    ap.add_argument("--reject", metavar="ID")
    ap.add_argument("--relation", choices=RELATIONS)
    ap.add_argument("--target", help="e1, e2, ... (the candidate's existing lessons)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--blocked", action="store_true", default=None,
                   help="screen case: the lesson should be quarantined")
    g.add_argument("--benign", dest="blocked", action="store_false",
                   help="screen case: the lesson should pass")
    ap.add_argument("--golden-dir", type=Path, default=GOLDEN_DIR)
    ap.set_defaults(blocked=None)  # neither flag given: keep the proposed label
    args = ap.parse_args()

    from tributary import golden, log

    log.setup(level="WARNING")

    def find(prefix: str) -> dict:
        matches = [c for c in golden.pending() if c["id"].startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(f"{len(matches)} pending candidates match {prefix!r}")
        return matches[0]

    kinds = set()
    if args.list:
        queue = golden.pending()
        print("\n\n".join(describe(c) for c in queue) if queue else "Nothing pending.")
        return
    if args.reject:
        reject(find(args.reject))
        return
    if args.accept:
        c = relabel(find(args.accept), relation=args.relation, target=args.target,
                    blocked=args.blocked)
        print(describe(c))
        kinds.add(accept(c, args.golden_dir))
    else:
        kinds = interactive(args.golden_dir)
    if "classification" in kinds and args.golden_dir.resolve() == GOLDEN_DIR.resolve():
        check_baseline()


if __name__ == "__main__":
    main()
