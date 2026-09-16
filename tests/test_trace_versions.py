"""Isolated trace and version-history checks; no model calls or external exports."""
import base64
import json
from types import SimpleNamespace

import pytest

from quality import store


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "trace-version-tests.sqlite3")
    store.init()


def test_tool_exception_retains_input_and_error_type_without_private_message(monkeypatch):
    from openinference.instrumentation import TracerProvider
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from agent import loop
    from quality import runtime
    from quality.tracing import RedactingOutbox

    provider = TracerProvider()
    provider.add_span_processor(RedactingOutbox())
    monkeypatch.setattr(runtime, "setup", lambda: provider.get_tracer("isolated-tool-test"))
    monkeypatch.setattr(runtime, "_patched", False)
    def broken(name, arguments):
        raise ValueError("private error jane@example.com")
    monkeypatch.setattr(loop, "execute_tool", broken)
    runtime._instrument_tools()
    capture = []
    token = runtime._capture.set(capture)
    try:
        with pytest.raises(ValueError):
            loop.execute_tool("test_lookup", {"destination": "Tokyo", "email": "jane@example.com"})
    finally:
        runtime._capture.reset(token)
    assert capture[0]["error_type"] == "ValueError" and capture[0]["result"] is None
    assert capture[0]["finished"] >= capture[0]["started"]
    encoded = base64.b64decode(json.loads(store.rows("SELECT payload FROM jobs WHERE kind='trace'")[0]["payload"])["otlp"])
    assert b"jane@example.com" not in encoded and b"private error" not in encoded
    span = ExportTraceServiceRequest.FromString(encoded).resource_spans[0].scope_spans[0].spans[0]
    assert span.status.code == 2
    assert any(a.key == "input.value" and "Tokyo" in a.value.string_value for a in span.attributes)
    assert any(a.key == "error.type" and a.value.string_value == "ValueError" for a in span.attributes)


def test_restart_is_not_a_version_update_and_rollback_is_recorded():
    from quality.versions import register_running_agent, recent_updates
    a = {"version": "code-a", "model": "model-a", "files": {"prompt.py": "a"}}
    b = {"version": "code-b", "model": "model-a", "files": {"prompt.py": "b"}}
    register_running_agent(a)
    register_running_agent(a)
    assert len(store.setting("agent_version_history")) == 1
    register_running_agent(b)
    register_running_agent(a)
    entries = recent_updates("judge", [])
    assert len(entries) == 3 and sum(e["status"] == "running" for e in entries) == 1
    assert entries[0]["previous_version"] == "code-b"
    assert "prompt.py" in entries[0]["summary"]


def test_candidate_pr_is_never_reported_as_deployed():
    from quality.versions import register_running_agent, recent_updates
    register_running_agent({"version": "running-code", "model": "model", "files": {}})
    repair = {"id": "repair", "updated": 100, "status": "pr_open", "payload": {
        "candidate_hash": "candidate-code", "summary": "Proposed fix", "pr_url": "https://github.com/test/test/pull/1"}}
    events = recent_updates("judge", [repair])
    candidate = next(e for e in events if e["kind"] == "candidate")
    assert candidate["status"] == "pr_open" and "not a running-agent" in candidate["detail"]
    assert store.setting("serving_agent")["version"] == "running-code"


def test_trace_inspector_preserves_hierarchy_and_unknown_token_usage(monkeypatch):
    from quality import trace_view
    spans = [{"name": name, "span_kind": kind, "context": {"span_id": identifier}, "parent_id": parent,
              "start_time": f"2026-01-01T00:00:0{start}+00:00", "end_time": "2026-01-01T00:00:05+00:00",
              "status_code": "OK", "attributes": attrs}
             for name, kind, identifier, parent, start, attrs in [
                 ("lookup", "TOOL", "tool", "root", 2, {"input.value": '{"city":"Tokyo"}', "output.value": '{"result":1}'}),
                 ("turn", "AGENT", "root", None, 0, {}),
                 ("model", "LLM", "llm", "root", 1, {"llm.model_name": "actual-model", "llm.token_count.total": 17})]]
    monkeypatch.setattr(trace_view, "client", lambda: SimpleNamespace(spans=SimpleNamespace(get_spans=lambda **kw: spans)))
    data = trace_view.trace_details("local-test")
    assert [s["id"] for s in data["spans"]] == ["root", "llm", "tool"]
    assert data["spans"][2]["depth"] == 1 and data["spans"][2]["duration_ms"] == 3000
    assert data["spans"][2]["input"] == {"city": "Tokyo"}
    assert data["spans"][0]["tokens"]["total"] is None
    assert data["spans"][1]["tokens"]["total"] == 17
