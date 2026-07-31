"""Tributary memory exposed as agent *tools*, not auto-injected context.

The point is agency: instead of the runner always recalling before the task
and always distilling after, the agent decides — turn by turn — whether to
consult tribal memory at all. That's what lets us measure the thing
interviewers probe for: knowing when NOT to use a tool. A self-contained
computation should never trigger a recall; an unfamiliar ops system should.

`MemoryTools.execute` returns a plain string (the tool result the harness
feeds back) and records what the agent did, so a caller can assert on it.
"""

from tributary import memory

MEMORY_TOOLS = [
    {"toolSpec": {
        "name": "tribal_recall",
        "description": "Search the tribe's shared memory for lessons another agent "
                       "already learned about a situation. Use ONLY when you're about "
                       "to do something an earlier agent may have hit before (deploys, "
                       "builds, calling an unfamiliar internal system). Do NOT use it "
                       "for self-contained work (arithmetic, string ops, reading a "
                       "value you were given) — there's nothing tribal to know.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}},
    }},
    {"toolSpec": {
        "name": "tribal_learn",
        "description": "Record a durable, reusable lesson so no agent re-learns it. "
                       "Use after discovering something non-obvious that future agents "
                       "in this environment would need.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "situation": {"type": "string"},
            "content": {"type": "string"},
            "evidence": {"type": "string"}},
            "required": ["situation", "content"]}},
    }},
    {"toolSpec": {
        "name": "tribal_reinforce",
        "description": "Mark a recalled lesson (by its id) as having actually helped, "
                       "so the tribe trusts it more.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "lesson_id": {"type": "string"}}, "required": ["lesson_id"]}},
    }},
]

MEMORY_TOOL_NAMES = {t["toolSpec"]["name"] for t in MEMORY_TOOLS}


class MemoryTools:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.recalls = 0
        self.recalled_ids: set[str] = set()
        self.learns = 0
        self.reinforced: set[str] = set()

    def execute(self, name: str, args: dict) -> str:
        if name == "tribal_recall":
            hits = memory.recall(args.get("query", ""), agent_id=self.agent_id, k=5)
            self.recalls += 1
            self.recalled_ids.update(h.id for h in hits)
            if not hits:
                return "No tribal lessons for this. If you learn something, call tribal_learn."
            return "\n".join(
                f"[{h.id}] When {h.situation}: {h.content} "
                f"(confidence {h.confidence:.2f}, helped {h.times_helpful}x)"
                for h in hits)
        if name == "tribal_learn":
            out = memory.learn(args.get("content", ""), args.get("situation", ""),
                               self.agent_id, evidence=args.get("evidence", ""))
            self.learns += 1
            return f"Recorded ({out['action']}): lesson {out['lesson'].id}"
        if name == "tribal_reinforce":
            lid = args.get("lesson_id", "")
            if lid not in self.recalled_ids:
                return f"Cannot reinforce {lid}: you did not recall that lesson this run."
            memory.reinforce(lid, self.agent_id)
            self.reinforced.add(lid)
            return f"Reinforced {lid}."
        return f"ERROR: unknown memory tool '{name}'"
