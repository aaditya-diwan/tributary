"""Bedrock Claude helpers: the Converse wrapper and the lesson classifier."""

import json
import re

from tributary import config

_client = None


def _bedrock():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return _client


def converse(messages, system: str | None = None, tools: list | None = None) -> dict:
    """Thin wrapper over the Bedrock Converse API. Returns the raw response."""
    kwargs = {
        "modelId": config.BEDROCK_MODEL_ID,
        "messages": messages,
        "inferenceConfig": {"maxTokens": 2048, "temperature": 0.2},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    if tools:
        kwargs["toolConfig"] = {"tools": tools}
    return _bedrock().converse(**kwargs)


def complete(prompt: str, system: str | None = None) -> str:
    """Single-turn text completion."""
    resp = converse([{"role": "user", "content": [{"text": prompt}]}], system=system)
    return "".join(
        b.get("text", "") for b in resp["output"]["message"]["content"]
    ).strip()


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
