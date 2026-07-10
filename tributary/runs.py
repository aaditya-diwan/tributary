"""Benchmark run logging — powers the generational learning curve."""

from tributary.db import run_readonly, run_txn


def log_run(agent_name: str, task: str, outcome: str, steps: int, tokens: int,
            seconds: float, lessons_recalled: int, generation: int | None = None) -> None:
    def txn(cur):
        cur.execute(
            """
            INSERT INTO runs (agent_name, generation, task, outcome, steps,
                              tokens, seconds, lessons_recalled)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (agent_name, generation, task, outcome, steps, tokens, seconds,
             lessons_recalled),
        )

    run_txn(txn)


def list_runs(limit: int = 500) -> list[dict]:
    rows = run_readonly(
        """
        SELECT agent_name, generation, task, outcome, steps, tokens,
               seconds, lessons_recalled, at
        FROM runs ORDER BY at ASC LIMIT %s
        """,
        (limit,),
    )
    return [
        {"agent": r[0], "generation": r[1], "task": r[2], "outcome": r[3],
         "steps": r[4], "tokens": r[5], "seconds": r[6],
         "lessons_recalled": r[7], "at": str(r[8])}
        for r in rows
    ]
