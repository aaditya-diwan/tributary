"""Tributary eval harness.

Two tiers:

- ``offline`` — deterministic (hash embeddings + heuristic classifier).
  Runs in CI on every push against a single-node CockroachDB; catches
  regressions in the memory pipeline itself (gate logic, transaction
  semantics, retrieval plumbing).
- ``live`` — real embeddings + real LLM classification. Produces the
  quality metrics (classification accuracy, paraphrase recall, judge
  scores) that are expected to move over time as prompts/models change.

Run:  python -m evals.run_eval --tier offline --check-baseline
"""
