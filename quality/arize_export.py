"""Authenticated Arize AX mirror of already-redacted OpenInference spans.

Phoenix remains the trace/evaluation/experiment workbench. This exporter never
creates spans, changes their timestamps, or places credentials in the outbox.
"""
import base64
import time
from urllib.parse import urlsplit

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from quality import store
from quality.config import ARIZE_API_KEY, ARIZE_OTLP_ENDPOINT, ARIZE_SPACE_ID


def endpoint_url(endpoint=ARIZE_OTLP_ENDPOINT):
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in {
        "otlp.arize.com", "otlp.eu-west-1a.arize.com", "otlp.ca-central-1a.arize.com",
    } or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Arize exporter requires an official HTTPS collector endpoint")
    path = parsed.path.rstrip("/")
    if path == "/v1":
        return endpoint.rstrip("/") + "/traces"
    if path == "/v1/traces":
        return endpoint.rstrip("/")
    raise ValueError("Expected an Arize /v1 or /v1/traces endpoint")


def export(payload, client):
    if not ARIZE_API_KEY or not ARIZE_SPACE_ID:
        raise RuntimeError("Arize credentials unavailable")
    try:
        response = client.post(endpoint_url(), content=base64.b64decode(payload["otlp"]),
                               headers={"Content-Type": "application/x-protobuf",
                                        "space_id": ARIZE_SPACE_ID, "api_key": ARIZE_API_KEY},
                               timeout=20, follow_redirects=False)
        response.raise_for_status()
        if response.content:
            result = ExportTraceServiceResponse.FromString(response.content)
            if result.partial_success.rejected_spans:
                raise RuntimeError("Arize rejected spans")
        store.set_setting("arize_delivery", {"status": "accepted", "at": time.time(),
                                            "http_status": response.status_code})
    except Exception as error:
        store.set_setting("arize_delivery", {"status": "failed", "at": time.time(),
                                            "error_type": type(error).__name__})
        raise
