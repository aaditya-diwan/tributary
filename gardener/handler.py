"""The Gardener — an AWS Lambda that tends the tribe's memory.

Scheduled via EventBridge. Decays confidence of stale lessons and retires
those that fall below the floor, so the shared memory stays trustworthy.
Every action is written to memory_audit so gardening is visible on the
dashboard.

Local run:  python -m gardener.handler
"""

DECAY_AFTER_DAYS = 7
DECAY_AMOUNT = 0.05
RETIRE_BELOW = 0.2

from tributary.db import run_txn  # noqa: E402


def lambda_handler(event=None, context=None):
    def txn(cur):
        # Decay lessons that haven't been used recently.
        cur.execute(
            """
            UPDATE lessons
            SET confidence = GREATEST(confidence - %s, 0.0)
            WHERE status = 'active'
              AND COALESCE(last_used_at, created_at) < now() - make_interval(days => %s)
            RETURNING id
            """,
            (DECAY_AMOUNT, DECAY_AFTER_DAYS),
        )
        decayed = [r[0] for r in cur.fetchall()]

        # Retire lessons whose confidence has withered away.
        cur.execute(
            """
            UPDATE lessons SET status = 'retired'
            WHERE status = 'active' AND confidence < %s
            RETURNING id
            """,
            (RETIRE_BELOW,),
        )
        retired = [r[0] for r in cur.fetchall()]

        for lid in decayed:
            cur.execute(
                "INSERT INTO memory_audit (action, lesson_id, agent_name, detail) "
                "VALUES ('decay', %s, 'gardener', 'stale — confidence reduced')",
                (lid,),
            )
        for lid in retired:
            cur.execute(
                "INSERT INTO memory_audit (action, lesson_id, agent_name, detail) "
                "VALUES ('retire', %s, 'gardener', 'confidence below floor')",
                (lid,),
            )
        return {"decayed": len(decayed), "retired": len(retired)}

    result = run_txn(txn)
    print(f"gardener: {result}")
    return result


if __name__ == "__main__":
    lambda_handler()
