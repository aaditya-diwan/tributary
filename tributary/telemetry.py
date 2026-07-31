"""OpenTelemetry tracing across the agent loop — opt-in and degrade-safe.

Enable with TRIBUTARY_TRACING=1. Exporter is chosen by env:
  OTEL_EXPORTER_OTLP_ENDPOINT set -> OTLP over HTTP; otherwise console.

If the OpenTelemetry SDK isn't installed, or tracing is off, `span()` is a
no-op context manager that still accepts `.set_attribute(...)`, so call sites
never need to guard. Spans form the tree:

    agent.run
      agent.step
        llm.call            (attrs: purpose, model, tokens, cost_usd, escalated)
        tool.execute        (attrs: tool, chaos)
      memory.learn
        llm.classify        (attrs: relation, confidence, escalated)
        db.txn              (attrs: db.txn.attempts  <- serializable retries)
"""

import os
from contextlib import contextmanager

_ENABLED = os.environ.get("TRIBUTARY_TRACING", "") not in ("", "0", "false")
_tracer = None
_init_done = False


class _NoopSpan:
    def set_attribute(self, *a, **k):
        pass

    def record_exception(self, *a, **k):
        pass


def _init():
    global _tracer, _init_done
    _init_done = True
    if not _ENABLED:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter()
        else:
            exporter = ConsoleSpanExporter()

        provider = TracerProvider(resource=Resource.create({"service.name": "tributary"}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("tributary")
    except Exception:
        _tracer = None  # SDK missing or misconfigured -> silent no-op


@contextmanager
def span(name: str, **attrs):
    if not _init_done:
        _init()
    if _tracer is None:
        s = _NoopSpan()
        for k, v in attrs.items():
            s.set_attribute(k, v)
        yield s
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
        try:
            yield s
        except Exception as e:  # mark the span, then re-raise
            s.record_exception(e)
            raise
