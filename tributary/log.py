"""Structured logging: what the memory layer is doing, and why.

Library modules log through `get_logger(__name__)`. Nothing is emitted until
an entrypoint (a script, the dashboard, the MCP server, the gardener, the
eval harness) calls `setup()`, so importing tributary never hijacks a host
application's logging.

    TRIBUTARY_LOG_LEVEL   DEBUG | INFO (default) | WARNING | ERROR
    TRIBUTARY_LOG_FORMAT  text (default) | json  (one JSON object per line)
    TRIBUTARY_LOG_FILE    also append JSON lines to this file (rotating)

Calls take structured fields as keyword arguments (None-valued fields are
dropped, so optional details can be passed unconditionally):

    log.info("learn", action="superseded", lesson=new.id, ms=41)

and every record carries the context bound with `context()` — the agent and
a short operation id — so one learn() can be followed from injection screen
to classifier verdict to escalation to serializable retries to commit:

    with log.context(op=log.new_op("learn")):
        ...

Three rules this module enforces:
- Handlers write to stderr, never stdout. The MCP server speaks JSON-RPC over
  stdout; a single stray log line there corrupts the protocol.
- Only the `tributary` and `typesafe_sdk` loggers are configured, with
  propagate=False. The root logger is left alone (no httpx/openai request
  noise), and Lambda's root handler doesn't print every gardener line twice.
- Lesson text is untrusted and can be long: callers truncate it with
  `preview()`, and the text formatter caps every field value.
"""

import contextvars
import json
import logging
import logging.handlers
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT = "tributary"
_FIELD_CAP = 300  # max chars per field value in text output

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("tributary_log_context",
                                                               default={})
_defaults: dict = {}  # process-wide fields (e.g. the MCP server's agent name)
_configured = False

logging.getLogger(ROOT).addHandler(logging.NullHandler())

# Attributes every LogRecord has; anything else passed via `extra` is ours.
_STD_KWARGS = {"exc_info", "stack_info", "stacklevel", "extra"}


class _FieldsAdapter(logging.LoggerAdapter):
    """Lets callers pass structured fields as plain keyword arguments."""

    def process(self, msg, kwargs):
        fields = {k: kwargs.pop(k) for k in list(kwargs) if k not in _STD_KWARGS}
        extra = kwargs.setdefault("extra", {})
        extra["fields"] = {**extra.get("fields", {}), **fields}
        return msg, kwargs


def get_logger(name: str) -> logging.LoggerAdapter:
    """A logger under the `tributary` hierarchy (`agents.runner` becomes
    `tributary.agents.runner`), accepting structured keyword fields."""
    if name != ROOT and not name.startswith(ROOT + "."):
        name = f"{ROOT}.{name}"
    return _FieldsAdapter(logging.getLogger(name), {})


def new_op(kind: str) -> str:
    """A short id naming one operation, e.g. `learn-3f2a9c1d`."""
    return f"{kind}-{uuid.uuid4().hex[:8]}"


@contextmanager
def context(**fields):
    """Attach fields to every record logged inside this block (and in any
    thread or task started from it)."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def set_defaults(**fields) -> None:
    """Attach fields to every record for the life of the process."""
    _defaults.update(fields)


def preview(text, limit: int = 120) -> str:
    """One-line, truncated rendering of untrusted text for a log field."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _ContextFilter(logging.Filter):
    """Stamps bound context (and the OpenTelemetry trace id, when tracing is
    on) onto every record, including records from the TypeSafe SDK."""

    def filter(self, record):
        record.ctx = {**_defaults, **_context.get()}
        trace_id = _current_trace_id()
        if trace_id:
            record.ctx["trace_id"] = trace_id
        if not hasattr(record, "fields"):
            record.fields = {}
        return True


def _current_trace_id() -> str | None:
    from tributary import telemetry

    if telemetry._tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None
    except Exception:
        return None


def _short_name(name: str) -> str:
    return name[len(ROOT) + 1:] if name.startswith(ROOT + ".") else name


def _render(value) -> str:
    s = value if isinstance(value, str) else json.dumps(
        value, default=str, ensure_ascii=False, separators=(",", ":"))
    if len(s) > _FIELD_CAP:
        s = s[: _FIELD_CAP - 1] + "…"
    return json.dumps(s, ensure_ascii=False) if (" " in s or s == "") else s


class TextFormatter(logging.Formatter):
    """`12:01:02.345 INFO    memory  [agent=agent-a op=learn-3f2a] learn action=inserted`"""

    def format(self, record):
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        ctx = " ".join(f"{k}={v}" for k, v in record.ctx.items() if k != "trace_id")
        fields = " ".join(f"{k}={_render(v)}" for k, v in record.fields.items()
                          if v is not None)
        line = (f"{ts} {record.levelname:<7} {_short_name(record.name):<14} "
                + (f"[{ctx}] " if ctx else "") + record.getMessage()
                + (f" {fields}" if fields else ""))
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, context, fields."""

    def format(self, record):
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **record.ctx,
            **{k: v for k, v in record.fields.items() if v is not None},
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def setup(level: str | None = None, fmt: str | None = None,
          file: str | None = None, force: bool = False) -> None:
    """Configure Tributary logging from arguments or TRIBUTARY_LOG_* env vars.

    Idempotent: entrypoints can all call it; the first call wins unless
    `force=True`.
    """
    global _configured
    if _configured and not force:
        return
    level = (level or os.environ.get("TRIBUTARY_LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.environ.get("TRIBUTARY_LOG_FORMAT") or "text").lower()
    file = file or os.environ.get("TRIBUTARY_LOG_FILE") or None

    # Windows consoles default to cp1252; lesson text with "→" in a log line
    # would raise mid-run (the same crash agents/runner.py guards stdout from).
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    handlers = []
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handlers.append(console)
    if file:
        rotating = logging.handlers.RotatingFileHandler(
            file, maxBytes=10_000_000, backupCount=3, encoding="utf-8")
        rotating.setFormatter(JsonFormatter())
        handlers.append(rotating)
    for h in handlers:
        h.addFilter(_ContextFilter())

    ours = logging.getLogger(ROOT)
    ours.setLevel(level)
    # The TypeSafe SDK logs request/response bodies at DEBUG; keep it at
    # WARNING (retries, errors) unless its own TYPESAFE_LOG_LEVEL asks for more.
    sdk = logging.getLogger("typesafe_sdk")
    if not os.environ.get("TYPESAFE_LOG_LEVEL"):
        sdk.setLevel(logging.WARNING)
    for lg in (ours, sdk):
        for h in [h for h in lg.handlers if not isinstance(h, logging.NullHandler)]:
            lg.removeHandler(h)
            h.close()
        for h in handlers:
            lg.addHandler(h)
        lg.propagate = False
    _configured = True
