"""The Gauntlet — a simulated ops environment with deterministic traps.

Agents are asked to deploy a service. The environment hides gotchas behind
unhelpful error messages (like real infrastructure does), so an agent with
empty memory burns steps discovering them, while an agent with tribal
memory sails through. Traps:

  1. run_build fails with a cryptic error unless clear_build_cache ran first.
  2. get_config("db_url") is silently deprecated (returns empty); the real
     key is "db_url_v2".
  3. deploy() returns 429 unless headers include {"X-Batch": "true"}.
  4. verify_deployment fails if the deploy used an empty db_url.
"""

import json
import random


class Gauntlet:
    def __init__(self, chaos: float = 0.0, seed: int | None = None):
        """chaos: probability [0,1] that a tool call returns garbage or a
        truncated result instead of its real one — a visible failure mode for
        the agent to detect, distrust, and retry. Deterministic when `seed`
        is set, so chaos runs are reproducible."""
        self.cache_cleared = False
        self.build_ok = False
        self.deployed_db_url = None
        self.deployed = False
        self.rate_limited = 0
        self.steps = 0
        self.chaos = chaos
        self.chaos_events = 0
        self._rng = random.Random(seed)
        self.transcript: list[dict] = []

    _GARBAGE = [
        "\x00\x01� garbled response �\x02",
        "ERROR ERROR ERROR ",  # truncated mid-word
        "{'partial': tru",     # malformed JSON fragment
        "504 Gateway Timeout upstream connect error or disconnect/reset",
    ]

    def _maybe_corrupt(self, name: str, result: str) -> str:
        # `done` is never corrupted — corrupting the terminal call can't teach
        # the agent anything and only wedges the loop.
        if name != "done" and self.chaos and self._rng.random() < self.chaos:
            self.chaos_events += 1
            return self._rng.choice(self._GARBAGE)
        return result

    # ---- tool schemas (Converse-style toolSpec format) ----
    TOOLS = [
        {"toolSpec": {
            "name": "run_build",
            "description": "Build the payments service artifact.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }},
        {"toolSpec": {
            "name": "clear_build_cache",
            "description": "Clear the local build cache directory.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }},
        {"toolSpec": {
            "name": "get_config",
            "description": "Read a key from the config service.",
            "inputSchema": {"json": {"type": "object", "properties": {
                "key": {"type": "string"}}, "required": ["key"]}},
        }},
        {"toolSpec": {
            "name": "deploy",
            "description": "Deploy the built artifact via the internal deploy API. "
                           "Optionally pass extra HTTP headers as a JSON object.",
            "inputSchema": {"json": {"type": "object", "properties": {
                "db_url": {"type": "string"},
                "headers": {"type": "object"}}, "required": ["db_url"]}},
        }},
        {"toolSpec": {
            "name": "verify_deployment",
            "description": "Run the post-deploy health check.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }},
        {"toolSpec": {
            "name": "done",
            "description": "Declare the task finished, with a one-line summary.",
            "inputSchema": {"json": {"type": "object", "properties": {
                "summary": {"type": "string"}}, "required": ["summary"]}},
        }},
    ]

    def execute(self, name: str, args: dict) -> str:
        self.steps += 1
        result = self._maybe_corrupt(name, self._dispatch(name, args or {}))
        self.transcript.append({"step": self.steps, "tool": name,
                                "args": args, "result": result})
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "clear_build_cache":
            self.cache_cleared = True
            return "Cache cleared (freed 1.2 GB)."

        if name == "run_build":
            if not self.cache_cleared:
                return ("BUILD FAILED: linker exited with code 137. "
                        "See build.log for details. (build.log is empty)")
            self.build_ok = True
            return "Build succeeded: artifact payments-svc-4.2.1.tar.gz"

        if name == "get_config":
            key = args.get("key", "")
            if key == "db_url":
                return ""  # silently deprecated — the classic trap
            if key == "db_url_v2":
                return "postgres://payments:****@db.internal:26257/payments"
            return (f"ERROR: unknown config key '{key}'. Available keys: "
                    "build_flags, db_url, db_url_v2, deploy_region, log_level")

        if name == "deploy":
            if not self.build_ok:
                return "DEPLOY FAILED: no artifact found. Run a successful build first."
            headers = args.get("headers") or {}
            if str(headers.get("X-Batch", "")).lower() != "true":
                self.rate_limited += 1
                if self.rate_limited < 2:
                    return "HTTP 429 Too Many Requests: rate limit exceeded. Retry later."
                return ("HTTP 429 Too Many Requests: rate limit exceeded. "
                        "(docs: interactive deploys are rate limited; batch "
                        "deploys with header X-Batch: true are exempt)")
            self.deployed = True
            self.deployed_db_url = args.get("db_url", "")
            return "Deploy accepted: release r-8817 rolling out."

        if name == "verify_deployment":
            if not self.deployed:
                return "HEALTH CHECK FAILED: no active release found."
            if not self.deployed_db_url:
                return ("HEALTH CHECK FAILED: service crash-looping — "
                        "DB connection string is empty.")
            return "HEALTH CHECK PASSED: payments-svc r-8817 serving traffic."

        if name == "done":
            return "Task closed."

        return f"ERROR: unknown tool '{name}'"

    @property
    def succeeded(self) -> bool:
        return self.deployed and bool(self.deployed_db_url)

    def transcript_json(self) -> str:
        return json.dumps(self.transcript, indent=2, default=str)
