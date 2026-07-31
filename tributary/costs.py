"""Best-effort LLM cost/latency logging to the llm_calls table.

`claude -p --output-format json` returns real per-call token usage and
`total_cost_usd`, which the rest of the pipeline previously discarded. We
capture it here so the dashboard can show cost per run, cost per lesson, and
whether model tiering is actually saving money.

Logging is best-effort: it never raises into the caller (a metrics write must
not break a learn), and it's skipped entirely offline or without a database.
"""

from tributary import config


def log_call(purpose: str, model: str, usage: dict, cost_usd: float,
             ms: int, escalated: bool = False) -> None:
    if config.OFFLINE or not config.DATABASE_URL:
        return
    try:
        from tributary.db import run_txn

        def txn(cur):
            cur.execute(
                "INSERT INTO llm_calls (purpose, model, in_tokens, out_tokens, "
                "cost_usd, ms, escalated) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (purpose, model, usage.get("input_tokens", 0),
                 usage.get("output_tokens", 0), cost_usd, ms, escalated),
            )

        run_txn(txn)
    except Exception:
        pass  # metrics are never allowed to break the caller
