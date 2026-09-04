# Tributary eval harness

Measures the memory system instead of demoing it. Two tiers:

| tier | backends | when it runs | what it proves |
|---|---|---|---|
| `offline` | hash embeddings + heuristic classifier (deterministic) | CI, every push (`.github/workflows/eval.yml`) | the *pipeline* didn't regress: transaction semantics, gate logic, retrieval plumbing |
| `live` | real embeddings + real `claude -p` | manually / nightly | the *quality* metrics that move over time: classification accuracy, paraphrase recall, judge scores |

```bash
python -m evals.run_eval --tier offline --check-baseline   # what CI runs
python -m evals.run_eval --tier live                       # full quality run
python -m evals.run_eval --tier live --suite classification --limit 10
```

Every suite run appends to `evals/results/history.jsonl` and inserts into the
`eval_results` table, so the dashboard can plot metrics over time. The
regression gate compares one key metric per suite against `evals/baseline.json`
(refresh deliberately with `--update-baseline`).

## Suites

- **classification**, 45 golden lesson-writes (`golden/classification.jsonl`),
  each with pre-existing similar lessons and an expected verdict:
  duplicate / contradicts / novel (+ which lesson). Strict scoring: the
  relation *and* the target must match. Cases are tagged by category and
  difficulty; `hard` cases include unit conversions ("30 seconds" vs "half a
  minute"), instance-vs-generalization, and same-vocabulary/different-situation
  traps.
- **retrieval**, a fixed 12-lesson corpus plus golden queries
  (`golden/retrieval.jsonl`). Word-overlap queries run in both tiers;
  true-paraphrase queries ("compiler keeps getting killed" → clear the build
  cache) are live-only, since hash embeddings can't do semantics. Metrics:
  hit@1, hit@5, MRR.
- **e2e**, scripted write-path invariants against a real Postgres
  (`tributary_eval` database, auto-created): duplicate→reinforce,
  contradiction→supersede with provenance, concurrent contradiction→exactly
  one active lesson. Offline-only so it's deterministic.
- **redteam**, adversarial lesson-writes (`golden/redteam.jsonl`) against the
  injection screen (`tributary/guard.py`): classifier-hijacks, reader
  tool-hijacks, exfiltration, role-hijacks, fence breakouts. Metrics:
  `block_rate` (attacks quarantined) and `false_positive_rate` (benign ops
  lessons wrongly blocked, currently 0.0). The full write-path enforcement
  (quarantine + recall exclusion + privilege separation + curator disputes)
  is covered by `tests/test_injection.py`.
- **agent**, tool discipline for the ReAct agent (`agents/react_runner.py`),
  live-only. Runs the agent on an ops task (should call `tribal_recall`) and on
  a self-contained compute task (should NOT, there's nothing tribal to know).
  Metric `tool_discipline` = fraction of correct recall decisions across both.
  Run: `python -m evals.run_eval --tier live --suite agent`.
- **judge**, LLM-as-judge over golden distillations
  (`golden/distillation.jsonl`), each hand-labeled 1–5. The suite reports the
  judge's agreement with the human labels (exact, within-1, Pearson r), the
  judge is only trusted as far as that calibration holds. Grow the labeled set
  before trusting it further.

## Labeling policy (classification)

- **duplicate**, same actionable rule, even if phrased differently, with
  extra non-conflicting detail, or stated as an instance of the general rule.
- **contradicts**, both lessons cannot be followed at once: mutually
  exclusive values, flipped requirements, or reversed orderings for the same
  situation.
- **novel**, a rule not captured by any existing lesson, even on the same
  system or with overlapping vocabulary. A narrower *additional* requirement
  is novel; a narrower *restatement* is a duplicate.

## Notes

- The offline classification score (~0.44) is intentionally weak, it's the
  deterministic regression anchor for the heuristic fallback, not a quality
  claim. The live score is the quality metric.
- The first offline run exposed a real boundary quirk: "use port 1111" vs
  "use port 2222" lands on the heuristic's duplicate side (word overlap
  exactly 0.8), and the pre-existing concurrent-conflict pytest passed
  vacuously in that case. The e2e suite now pins the unambiguous behavior;
  near-boundary phrasings are covered by golden classification cases instead.
