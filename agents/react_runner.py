"""ReAct-style agent that *decides* when to consult Tributary.

Unlike agents/runner.py (which always recalls before the task and distills
after), here tribal memory is a set of tools the agent chooses to call. That
makes tool discipline observable: on a self-contained task the agent should
never call tribal_recall; on an ops task it should. Both cases are exercised
by the `agent` eval suite.

Visible failure modes are first-class: tool results can be corrupted (Gauntlet
chaos mode), the model can name a tool that doesn't exist, and llm._run retries
transient CLI failures underneath. Each is surfaced, not hidden.

    python -m agents.react_runner --agent ada --task deploy
    python -m agents.react_runner --agent ada --task compute   # negative control
"""

import argparse
import json
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tributary import llm, log, memory, telemetry
from agents import prompts
from agents.tools import MEMORY_TOOLS, MEMORY_TOOL_NAMES, MemoryTools
from gauntlet import Gauntlet
from gauntlet.compute import ComputeTask

MAX_STEPS = 25

logger = log.get_logger(__name__)


def run_react_agent(agent_name: str, env, task: str, chaos: float = 0.0,
                    verbose: bool = True) -> dict:
    """Run the ReAct agent; structured log lines are tagged with its name."""
    with log.context(agent=agent_name):
        logger.info("agent run start", mode="react", task=log.preview(task, 80),
                    chaos=chaos or None)
        stats = _run_react_agent(agent_name, env, task, chaos, verbose)
        logger.info("agent run end", outcome=stats["outcome"], steps=stats["steps"],
                    tokens=stats["tokens"], recalls=stats["recalls"],
                    learns=stats["learns"], llm_errors=stats["llm_errors"] or None,
                    unknown_tool_calls=stats["unknown_tool_calls"] or None,
                    seconds=stats["seconds"])
        return stats


def _run_react_agent(agent_name: str, env, task: str, chaos: float,
                     verbose: bool) -> dict:
    agent_id = memory.ensure_agent(agent_name)
    mem = MemoryTools(agent_id)
    tools = list(env.TOOLS) + MEMORY_TOOLS
    env_tool_names = {t["toolSpec"]["name"] for t in env.TOOLS}

    messages = [{"role": "user", "content": [{"text": task}]}]
    tokens = {"input": 0, "output": 0}
    llm_errors = 0
    unknown_tool_calls = 0
    start = time.time()
    done = False

    if verbose:
        print(f"\n=== {agent_name} (ReAct) | {task[:70]} ===")

    with telemetry.span("agent.run", agent=agent_name, chaos=chaos) as run_span:
        for _ in range(MAX_STEPS):
            with telemetry.span("agent.step"):
                try:
                    resp = llm.converse(messages, system=prompts.REACT_SYSTEM, tools=tools)
                except llm.LLMError as e:
                    # The model turn itself failed after retries. Record it and
                    # stop cleanly rather than crashing the whole run.
                    llm_errors += 1
                    if verbose:
                        print(f"  [llm error] {e}")
                    break

            tokens["input"] += resp["usage"]["inputTokens"]
            tokens["output"] += resp["usage"]["outputTokens"]
            msg = resp["output"]["message"]
            messages.append(msg)

            for block in msg["content"]:
                if "text" in block and block["text"].strip() and verbose:
                    print(f"  [{agent_name}] {block['text'].strip()}")

            if resp["stopReason"] != "tool_use":
                break

            tool_results = []
            for block in msg["content"]:
                if "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                name, args = tu["name"], (tu.get("input") or {})
                with telemetry.span("tool.execute", tool=name):
                    if name in MEMORY_TOOL_NAMES:
                        result = mem.execute(name, args)
                    elif name in env_tool_names:
                        result = env.execute(name, args)
                    else:
                        # Model hallucinated a tool — feed the error back so it
                        # can correct, don't crash.
                        unknown_tool_calls += 1
                        result = (f"ERROR: no tool named '{name}'. Available: "
                                  f"{sorted(env_tool_names | MEMORY_TOOL_NAMES)}")
                if verbose:
                    print(f"  [{agent_name}] -> {name}({json.dumps(args)}) => {result[:90]}")
                tool_results.append({"toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": result or "(empty)"}]}})
                if name == "done":
                    done = True
            messages.append({"role": "user", "content": tool_results})
            if done:
                break

        outcome = "SUCCESS" if getattr(env, "succeeded", False) else "FAILED"
        run_span.set_attribute("outcome", outcome)
        run_span.set_attribute("recalls", mem.recalls)

    stats = {
        "agent": agent_name, "outcome": outcome, "steps": env.steps,
        "seconds": round(time.time() - start, 1),
        "tokens": tokens["input"] + tokens["output"],
        "recalls": mem.recalls, "learns": mem.learns,
        "reinforced": len(mem.reinforced),
        "chaos_events": getattr(env, "chaos_events", 0),
        "unknown_tool_calls": unknown_tool_calls, "llm_errors": llm_errors,
    }
    if verbose:
        print(f"    {outcome} | steps {stats['steps']} | recalls {stats['recalls']} "
              f"| chaos {stats['chaos_events']} | bad-tool {unknown_tool_calls}")
    return stats


def make_env(task_kind: str, chaos: float):
    if task_kind == "compute":
        return ComputeTask(), ComputeTask.TASK
    return Gauntlet(chaos=chaos), "Deploy the payments service."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--task", choices=["deploy", "compute"], default="deploy")
    ap.add_argument("--chaos", type=float, default=0.0,
                    help="probability a tool result is corrupted (deploy only)")
    args = ap.parse_args()
    env, task = make_env(args.task, args.chaos)
    run_react_agent(args.agent, env, task, chaos=args.chaos)


if __name__ == "__main__":
    log.setup()
    main()
