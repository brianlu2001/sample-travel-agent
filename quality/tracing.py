"""OpenInference spans with a redaction-first durable OTLP export boundary."""
import base64
import json
import logging
import threading

from openinference.instrumentation import TracerProvider
from openinference.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Link, Status, StatusCode

from quality import store
from quality.config import ARIZE_API_KEY, ARIZE_SPACE_ID, PROJECT
from quality.privacy import safe_payload

_provider = None
_lock = threading.Lock()


class RedactingOutbox(SpanProcessor):
    def on_end(self, span):
        try:
            original = dict(span.attributes or {})
            structural = {key: original.pop(key) for key in ("input.mime_type", "output.mime_type", "openinference.span.kind") if key in original}
            attributes = safe_payload(original)
            attributes.update(structural)
            # Events may contain raw provider exception messages / stack traces.
            # Keep their types only; never export free-form exception content.
            if span.events:
                attributes["event.types"] = json.dumps(safe_payload([e.name for e in span.events]))
            attributes = {k: v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
                          for k, v in attributes.items() if v is not None}
            safe = ReadableSpan(
                name=str(safe_payload(span.name)), context=span.context, parent=span.parent,
                resource=span.resource, attributes=attributes, events=(), links=[Link(link.context, attributes={}) for link in span.links],
                kind=span.kind, instrumentation_scope=span.instrumentation_scope,
                status=Status(span.status.status_code), start_time=span.start_time, end_time=span.end_time,
            )
            payload = base64.b64encode(encode_spans([safe]).SerializeToString()).decode()
            store.enqueue("trace", f"trace:{span.context.trace_id:032x}:{span.context.span_id:016x}", {"otlp": payload})
            if ARIZE_API_KEY and ARIZE_SPACE_ID:
                store.enqueue("arize_trace", f"arize:{span.context.trace_id:032x}:{span.context.span_id:016x}", {"otlp": payload})
        except Exception as error:
            # Telemetry failures must not break the chat; no raw payload fallback.
            logging.getLogger(__name__).error("telemetry_capture_failed:%s", type(error).__name__)


def setup():
    global _provider
    with _lock:
        if _provider is None:
            store.init()
            _provider = TracerProvider(resource=Resource({"service.name": PROJECT, "openinference.project.name": PROJECT}))
            _provider.add_span_processor(RedactingOutbox())
            trace.set_tracer_provider(_provider)
            AnthropicInstrumentor().instrument(tracer_provider=_provider)
            # SDK wire/debug logs must never print prompts, headers or keys.
            for name in ("httpx", "httpcore", "anthropic", "presidio-analyzer"):
                logging.getLogger(name).setLevel(logging.CRITICAL)
    return _provider.get_tracer("quality")


def ids(span):
    ctx = span.get_span_context()
    return {"trace_id": f"{ctx.trace_id:032x}", "span_id": f"{ctx.span_id:016x}"}


def set_io(span, input_value, output_value=None):
    span.set_attribute("input.value", json.dumps(safe_payload(input_value), ensure_ascii=False))
    span.set_attribute("input.mime_type", "application/json")
    if output_value is not None:
        span.set_attribute("output.value", json.dumps(safe_payload(output_value), ensure_ascii=False))
        span.set_attribute("output.mime_type", "application/json")


def error_status(span, error):
    span.set_attribute("error.type", type(error).__name__)
    span.set_status(Status(StatusCode.ERROR))
