"""A negative-control task: self-contained computation, no tribal knowledge.

The Gauntlet rewards recall — hidden ops gotchas another agent already solved.
This task is the opposite: everything needed is in front of the agent, so a
well-behaved ReAct agent should NOT call tribal_recall. It's how we measure
"knows when NOT to use a tool" rather than just asserting it.
"""

import hashlib


class ComputeTask:
    """Task: report the SHA-256 of a given artifact name. Deterministic."""

    ARTIFACT = "payments-svc-4.2.1"
    TASK = (f"Compute the SHA-256 hex digest of the exact string "
            f"'{ARTIFACT}' and report it with the `done` tool. This is pure "
            f"computation — everything you need is here.")

    def __init__(self):
        self.steps = 0
        self.answer = None
        self.transcript: list[dict] = []

    TOOLS = [
        {"toolSpec": {
            "name": "sha256",
            "description": "Return the SHA-256 hex digest of the given text.",
            "inputSchema": {"json": {"type": "object", "properties": {
                "text": {"type": "string"}}, "required": ["text"]}},
        }},
        {"toolSpec": {
            "name": "done",
            "description": "Submit the final answer.",
            "inputSchema": {"json": {"type": "object", "properties": {
                "answer": {"type": "string"}}, "required": ["answer"]}},
        }},
    ]

    def execute(self, name: str, args: dict) -> str:
        self.steps += 1
        result = self._dispatch(name, args or {})
        self.transcript.append({"step": self.steps, "tool": name,
                                "args": args, "result": result})
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "sha256":
            return hashlib.sha256(args.get("text", "").encode()).hexdigest()
        if name == "done":
            self.answer = (args.get("answer") or "").strip().lower()
            return "Answer submitted."
        return f"ERROR: unknown tool '{name}'"

    @property
    def expected(self) -> str:
        return hashlib.sha256(self.ARTIFACT.encode()).hexdigest()

    @property
    def succeeded(self) -> bool:
        return self.answer == self.expected
