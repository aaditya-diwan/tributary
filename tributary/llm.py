"""Headless Claude Code CLI helpers: the tool-use loop and the lesson classifier.

Shells out to `claude -p`, reusing whatever auth the local `claude` binary
already has (Pro/Max subscription or API key) instead of per-token Bedrock
billing. Each call runs with an isolated system prompt and no project
settings (`--setting-sources ""`, `--tools ""`) so it doesn't inherit Claude
Code's own CLAUDE.md/skills/tool-catalog context — that alone is the
difference between ~200 input tokens and ~30K per call.

`claude -p` is stateless per invocation (no server-side multi-turn session
the way Bedrock Converse has), so `converse()` re-renders the full message
transcript into one prompt each turn and asks for a JSON decision
(`--json-schema`) shaped like a single Converse turn, so callers (the agent
tool-use loop in agents/runner.py) don't need to change.
"""

import json
import re
import subprocess

from tributary import config

TIMEOUT_SECONDS = 120

TOOL_LOOP_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "What to say before/instead of acting."},
        "tool_use": {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": "string"},
                "input": {"type": "object"},
            },
            "required": ["name", "input"],
            "description": "The next tool call, or null to just reply with text.",
        },
    },
    "required": ["text", "tool_use"],
}


def _run(prompt: str, system: str, json_schema: dict | None = None) -> dict:
    """Run `claude -p` headless and return the parsed --output-format json result."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
        "--system-prompt", system,
        "--model", config.CLAUDE_CODE_MODEL,
    ]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "`claude` CLI not found on PATH — install Claude Code and log in "
            "(claude auth) before running online."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def complete(prompt: str, system: str | None = None) -> str:
    """Single-turn text completion."""
    result = _run(prompt, system or "You are a helpful assistant.")
    return (result.get("result") or "").strip()


def _tool_catalog(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        spec = t["toolSpec"]
        lines.append(
            f"- {spec['name']}: {spec['description']} "
            f"(input schema: {json.dumps(spec['inputSchema']['json'])})"
        )
    return "\n".join(lines)


def _render_transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m["role"]
        for block in m["content"]:
            if "text" in block:
                lines.append(f"{role}: {block['text']}")
            elif "toolUse" in block:
                tu = block["toolUse"]
                lines.append(f"{role} called {tu['name']}({json.dumps(tu.get('input') or {})})")
            elif "toolResult" in block:
                text = "".join(c.get("text", "") for c in block["toolResult"].get("content", []))
                lines.append(f"tool result: {text}")
    return "\n".join(lines)


def converse(messages, system: str | None = None, tools: list | None = None) -> dict:
    """Stand-in for the Bedrock Converse API, backed by headless `claude -p`.

    Returns a dict shaped like a Converse response (`output.message.content`,
    `usage`, `stopReason`) so callers don't need to know the backend changed.
    """
    parts = [system] if system else []
    if tools:
        parts.append("Available tools:\n" + _tool_catalog(tools))
        parts.append(
            "IMPORTANT: these tools are NOT attached to you — never try to "
            "invoke them yourself. Instead, decide the single next call and "
            "describe it in your structured output: set `tool_use` to "
            "{name, input matching the tool's schema}; the harness executes "
            "it and sends you the result next turn. Put any explanation in "
            "`text`. You are running unattended — there is no user to ask "
            "questions of. Keep going (experiment, retry variations) until "
            "the task is done — a step is not impossible merely because "
            "several attempts failed; exhaust plausible alternatives "
            "(renamed keys, versioned variants, different parameters) "
            "before concluding. Set `tool_use` to null only when the task "
            "is complete or truly impossible."
        )
    result = _run(
        _render_transcript(messages) or "(begin)",
        "\n\n".join(parts),
        json_schema=TOOL_LOOP_SCHEMA if tools else None,
    )

    content = []
    stop_reason = "end_turn"
    if tools:
        decision = result.get("structured_output") or {}
        if decision.get("text"):
            content.append({"text": decision["text"]})
        tool_use = decision.get("tool_use")
        if tool_use and tool_use.get("name"):
            call_id = f"call_{result.get('session_id', '0')[:8]}_{len(messages)}"
            content.append({"toolUse": {
                "toolUseId": call_id,
                "name": tool_use["name"],
                "input": tool_use.get("input") or {},
            }})
            stop_reason = "tool_use"
    elif result.get("result"):
        content.append({"text": result["result"]})

    usage = result.get("usage", {})
    return {
        "output": {"message": {"role": "assistant", "content": content or [{"text": ""}]}},
        "usage": {
            "inputTokens": usage.get("input_tokens", 0),
            "outputTokens": usage.get("output_tokens", 0),
        },
        "stopReason": stop_reason,
    }


CLASSIFY_SYSTEM = """You maintain a shared memory of lessons learned by AI agents.
Given a NEW lesson and EXISTING lessons, decide the relationship:
- "duplicate": the new lesson says essentially the same thing as an existing one
- "contradicts": the new lesson directly conflicts with an existing one (both cannot be true)
- "novel": the new lesson is genuinely new information

Respond with ONLY a JSON object: {"relation": "...", "target_id": "<id of the duplicate/contradicted lesson, or null>"}"""


def classify_lesson(new_situation: str, new_content: str, existing: list[dict]) -> dict:
    """Classify a new lesson against similar existing ones.

    `existing` items: {"id": str, "situation": str, "content": str}.
    Returns {"relation": "duplicate"|"contradicts"|"novel", "target_id": str|None}.
    """
    if not existing:
        return {"relation": "novel", "target_id": None}
    if config.OFFLINE:
        return _heuristic_classify(new_situation, new_content, existing)

    prompt = (
        f"NEW lesson:\n  situation: {new_situation}\n  content: {new_content}\n\n"
        "EXISTING lessons:\n"
        + "\n".join(
            f"  id={e['id']}\n  situation: {e['situation']}\n  content: {e['content']}"
            for e in existing
        )
    )
    raw = complete(prompt, system=CLASSIFY_SYSTEM)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        out = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        out = {}
    relation = out.get("relation", "novel")
    if relation not in ("duplicate", "contradicts", "novel"):
        relation = "novel"
    target = out.get("target_id")
    if target not in {e["id"] for e in existing}:
        target = existing[0]["id"] if relation != "novel" else None
    return {"relation": relation, "target_id": target}


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _heuristic_classify(situation: str, content: str, existing: list[dict]) -> dict:
    """Offline stand-in: same situation + same content -> duplicate;
    same situation + different content -> contradicts; else novel."""
    new_sit, new_con = _word_set(situation), _word_set(content)
    for e in existing:
        sit_overlap = len(new_sit & _word_set(e["situation"])) / max(len(new_sit), 1)
        con_overlap = len(new_con & _word_set(e["content"])) / max(len(new_con), 1)
        if sit_overlap >= 0.6:
            if con_overlap >= 0.8:
                return {"relation": "duplicate", "target_id": e["id"]}
            return {"relation": "contradicts", "target_id": e["id"]}
    return {"relation": "novel", "target_id": None}
