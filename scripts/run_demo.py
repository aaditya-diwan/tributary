"""The A-then-B demo: proof that tribal memory makes agents cheaper and faster.

Runs agent-a (typically with empty memory — it hits every trap and learns),
then agent-b as a completely separate cognitive run that recalls agent-a's
lessons and sails through. Prints the comparison table that goes in the video.

    python scripts/run_demo.py
"""

from agents.runner import run_agent

TASK = "Deploy the payments service"


def main():
    a = run_agent("agent-a", TASK)
    b = run_agent("agent-b", TASK)

    print("\n=== Tributary effect ===")
    print(f"{'agent':<10}{'outcome':<10}{'steps':<8}{'tokens':<10}{'lessons recalled'}")
    for s in (a, b):
        print(f"{s['agent']:<10}{s['outcome']:<10}{s['steps']:<8}"
              f"{s['tokens']:<10}{s['recalled']}")
    if a["tokens"]:
        saved = 100 * (1 - b["tokens"] / a["tokens"])
        print(f"\nagent-b used {saved:.0f}% fewer tokens and "
              f"{a['steps'] - b['steps']} fewer steps, thanks to the tribe's memory.")


if __name__ == "__main__":
    from tributary import log

    log.setup()
    main()
