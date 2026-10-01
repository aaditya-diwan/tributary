"""Agent runner: a Claude Code (headless) tool-use loop wired into Tributary memory.

    python -m agents.runner --agent agent-a --task "Deploy the payments service"

Before acting, the agent recalls relevant tribal lessons; after the run, an
LLM distills new lessons from the transcript and writes them to the shared
memory, and reinforces the given lessons that actually helped.
"""

import argparse
import json
import re
import sys
import time

# Windows consoles default to cp1252, which can't print the arrows/emoji the
# model likes to emit — a UnicodeEncodeError mid-run would lose the lessons.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tributary import llm, log, memory, runs
from agents import prompts
from gauntlet import Gauntlet

MAX_STEPS = 25

logger = log.get_logger(__name__)


def run_agent(agent_name: str, task: str, use_memory: bool = True,
              generation: int | None = None) -> dict:
    """Run one agent on a task. The printed narration on stdout is the demo;
    the structured log on stderr (tagged with the agent's name) is how you
    see what the memory layer did underneath it."""
    with log.context(agent=agent_name):
        logger.info("agent run start", task=log.preview(task, 80),
                    use_memory=use_memory, generation=generation)
        stats = _run_agent(agent_name, task, use_memory, generation)
        logger.info("agent run end", outcome=stats["outcome"], steps=stats["steps"],
                    tokens=stats["tokens"], recalled=stats["recalled"],
                    seconds=stats["seconds"])
        return stats


def _run_agent(agent_name: str, task: str, use_memory: bool,
               generation: int | None) -> dict:
    agent_id = memory.ensure_agent(agent_name)
    env = Gauntlet()

    # --- recall: inject tribal knowledge into the system prompt ---
    recalled = memory.recall(task, agent_id=agent_id, k=5) if use_memory else []
    if recalled:
        lessons_text = "\n".join(
            f"- [{l.id}] When {l.situation}: {l.content} "
            f"(confidence {l.confidence:.2f}, helped {l.times_helpful}x)"
            for l in recalled
        )
        tribal = prompts.TRIBAL_SECTION.format(lessons=lessons_text)
    else:
        tribal = prompts.NO_TRIBAL_SECTION
    system = prompts.AGENT_SYSTEM.format(tribal_section=tribal)

    print(f"\n=== {agent_name} | task: {task} ===")
    print(f"    tribal lessons recalled: {len(recalled)}")

    # --- the tool-use loop (Converse-shaped, backed by headless claude -p) ---
    messages = [{"role": "user", "content": [{"text": task}]}]
    tokens = {"input": 0, "output": 0}
    start = time.time()
    done = False

    for _ in range(MAX_STEPS):
        resp = llm.converse(messages, system=system, tools=Gauntlet.TOOLS)
        tokens["input"] += resp["usage"]["inputTokens"]
        tokens["output"] += resp["usage"]["outputTokens"]
        msg = resp["output"]["message"]
        messages.append(msg)

        for block in msg["content"]:
            if "text" in block and block["text"].strip():
                print(f"  [{agent_name}] {block['text'].strip()}")

        if resp["stopReason"] != "tool_use":
            break

        tool_results = []
        for block in msg["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            result = env.execute(tu["name"], tu.get("input") or {})
            print(f"  [{agent_name}] -> {tu['name']}({json.dumps(tu.get('input') or {})})"
                  f" => {result[:100]}")
            tool_results.append({"toolResult": {
                "toolUseId": tu["toolUseId"],
                "content": [{"text": result or "(empty)"}],
            }})
            if tu["name"] == "done":
                done = True
        messages.append({"role": "user", "content": tool_results})
        if done:
            break

    elapsed = time.time() - start
    outcome = "SUCCESS" if env.succeeded else "FAILED"
    stats = {"agent": agent_name, "outcome": outcome, "steps": env.steps,
             "seconds": round(elapsed, 1), "tokens": tokens["input"] + tokens["output"],
             "recalled": len(recalled)}
    print(f"    {outcome} in {env.steps} steps, {stats['tokens']} tokens, {elapsed:.1f}s")
    runs.log_run(agent_name, task, outcome, env.steps, stats["tokens"],
                 elapsed, len(recalled), generation=generation)

    # --- distill new lessons and write them to the tribe ---
    if use_memory and env.transcript:
        _distill_and_learn(agent_id, task, outcome, recalled, env)

    return stats


def _distill_and_learn(agent_id, task, outcome, recalled, env):
    given = "\n".join(f"[{l.id}] {l.content}" for l in recalled) or "(none)"
    raw = llm.complete(
        prompts.DISTILL_PROMPT.format(task=task, outcome=outcome, given=given,
                                      transcript=env.transcript_json()),
        system=prompts.DISTILL_SYSTEM,
    )
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        print("    (distillation produced no JSON — skipping)")
        logger.warning("distillation produced no JSON; no lessons learned",
                       reply=log.preview(raw, 200))
        return
    try:
        distilled = json.loads(match.group(0))
    except json.JSONDecodeError:
        print("    (distillation JSON invalid — skipping)")
        logger.warning("distillation JSON invalid; no lessons learned",
                       reply=log.preview(match.group(0), 200))
        return

    for l in distilled.get("new_lessons", []):
        out = memory.learn(l["content"], l["situation"], agent_id,
                           evidence=l.get("evidence", ""))
        print(f"    learned ({out['action']}): {l['content']}")
    valid_ids = {l.id for l in recalled}
    for lid in distilled.get("helpful_lesson_ids", []):
        if lid in valid_ids:
            memory.reinforce(lid, agent_id)
            print(f"    reinforced: {lid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="agent name, e.g. agent-a")
    ap.add_argument("--task", default="Deploy the payments service")
    ap.add_argument("--no-memory", action="store_true",
                    help="run without Tributary (baseline)")
    args = ap.parse_args()
    run_agent(args.agent, args.task, use_memory=not args.no_memory)


if __name__ == "__main__":
    log.setup()
    main()
