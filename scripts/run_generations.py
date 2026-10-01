"""The generational learning curve: run N generations of fresh agents and
watch the species get smarter — tokens and steps per task decline as the
tribe's memory accumulates. Results land in the `runs` table and render as
the curve on the dashboard.

    python scripts/run_generations.py --generations 6
"""

import argparse

from agents.runner import run_agent

# Task phrasing varies per generation so recall has to work semantically,
# not by exact match. Same Gauntlet traps underneath.
TASK_VARIANTS = [
    "Deploy the payments service",
    "Ship the latest build of payments-svc to production",
    "Get the payments service deployed and healthy",
    "Roll out the payments service",
    "Push the payments service live and verify it",
    "Release the new payments service version",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=6)
    args = ap.parse_args()

    results = []
    for gen in range(1, args.generations + 1):
        task = TASK_VARIANTS[(gen - 1) % len(TASK_VARIANTS)]
        stats = run_agent(f"gen{gen}-agent", task, generation=gen)
        results.append(stats)

    print("\n=== The species gets smarter ===")
    print(f"{'gen':<5}{'outcome':<10}{'steps':<8}{'tokens':<10}{'recalled'}")
    for gen, s in enumerate(results, 1):
        print(f"{gen:<5}{s['outcome']:<10}{s['steps']:<8}{s['tokens']:<10}{s['recalled']}")


if __name__ == "__main__":
    from tributary import log

    log.setup()
    main()
