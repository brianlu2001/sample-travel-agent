"""Failure-path tests use a temporary database and never send telemetry or email."""
import base64
import json
import time

import pytest

from quality import store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "operations.sqlite3")
    store.init()


def test_monitor_warmup_persistence_recovery_and_deduplication(monkeypatch):
    from quality import monitoring
    for name, value in {"MIN_SAMPLES": 3, "WINDOW": 5, "PERSISTENCE": 2}.items():
        monkeypatch.setattr(monitoring, name, value)
    def window(*args, **kwargs):
        rows = store.rows("SELECT * FROM runs ORDER BY created DESC LIMIT 5")
        for row in rows:
            row["evaluation"] = json.loads(row["evaluation"])
            row["trace_id"] = "unit-test-only-" + row["id"]
        return rows
    monkeypatch.setattr(monitoring, "current_window", window)
    def sample(i, label):
        evaluation = {"version": "unit-judge", "metrics": {"correctness": {"label": label}}}
        store.execute("INSERT INTO runs(id,created,source,version,event,evaluation,evaluated) VALUES(?,?,?,?,?,?,?)",
                      (str(i), time.time()+i/1000, "live", "unit", "{}", json.dumps(evaluation), time.time()))
        monitoring.monitor("live", "unit", evaluation_version="unit-judge")
    for i in range(1, 5):
        sample(i, "fail")
    assert not store.rows("SELECT * FROM incidents")
    sample(5, "fail")
    assert store.rows("SELECT status FROM incidents")[0]["status"] == "open"
    monitoring.monitor("live", "unit", evaluation_version="unit-judge")
    assert len(store.rows("SELECT * FROM jobs WHERE kind='email'")) == 1
    for i in range(6, 13):
        sample(i, "pass")
    assert store.rows("SELECT status FROM incidents")[0]["status"] == "resolved"
    for i in range(13, 16):
        sample(i, "fail")
    assert store.rows("SELECT status FROM incidents")[0]["status"] == "open"
    assert len(store.rows("SELECT * FROM incidents")) == 1
    assert len(store.rows("SELECT * FROM jobs WHERE kind='email'")) == 3


def test_expired_lease_reclaimed_and_old_owner_cannot_complete():
    store.enqueue("unit", "lease", {})
    old = store.claim(("unit",))
    store.execute("UPDATE jobs SET lease_until=0")
    new = store.claim(("unit",))
    store.finish(old)
    assert store.rows("SELECT state FROM jobs")[0]["state"] == "running"
    store.finish(new)
    assert store.rows("SELECT state FROM jobs")[0]["state"] == "done"


def test_span_redacted_before_outbox_serialization():
    from openinference.instrumentation import TracerProvider
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from quality.tracing import RedactingOutbox
    provider = TracerProvider()
    provider.add_span_processor(RedactingOutbox())
    with provider.get_tracer("unit-local-only").start_as_current_span("unit") as span:
        span.set_attribute("input.mime_type", "application/json")
        span.set_attribute("input.value", "My passport number is X12345678. Email jane@example.com. Plan 2 days in Paris.")
        trace_id = span.context.trace_id
    job = store.rows("SELECT payload FROM jobs")[0]
    encoded = base64.b64decode(json.loads(job["payload"])["otlp"])
    assert b"X12345678" not in encoded and b"jane@example.com" not in encoded
    parsed = ExportTraceServiceRequest.FromString(encoded).resource_spans[0].scope_spans[0].spans[0]
    assert int.from_bytes(parsed.trace_id, "big") == trace_id
    assert parsed.end_time_unix_nano >= parsed.start_time_unix_nano > 0
    assert next(a.value.string_value for a in parsed.attributes if a.key == "input.mime_type") == "application/json"


def test_privacy_failure_immediately_queues_alert():
    store.save_run({"id": "unit", "created": time.time(), "source": "unit", "version": "unit", "privacy_ok": False})
    assert store.rows("SELECT status FROM incidents")[0]["status"] == "open"
    assert len(store.rows("SELECT id FROM jobs WHERE kind='email'")) == 1


def test_api_input_and_origin_validation():
    from fastapi.testclient import TestClient
    from agent.api import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/health").status_code == 200
        assert client.post("/chat", json={"message": "  "}).status_code == 422
        assert client.post("/chat", json={"message": "Paris", "conversation_id": "invalid"}).status_code == 422
        assert client.post("/chat", json={"message": "Paris"}, headers={"origin": "https://untrusted.example"}).status_code == 403
        assert client.get("/health", headers={"host": "untrusted.example"}).status_code == 400


def test_repair_rejects_changed_baseline_evidence():
    from quality.remediation import repair
    store.save_run({"id": "unit", "created": time.time(), "source": "unit", "version": "unit"})
    store.execute("INSERT INTO benchmarks(id,kind,status,created,manifest,report) VALUES(?,?,?,?,?,?)",
                  ("base", "baseline", "complete", time.time(), "{}", json.dumps({"case_run_ids": ["unit"], "evidence_hash": "changed"})))
    with pytest.raises(ValueError, match="evidence changed"):
        repair("incident", "base")
    assert not store.rows("SELECT * FROM repairs")


def test_repair_context_excludes_held_out_cases():
    from quality.evaluation import scenarios
    from quality.remediation import evidence_for
    chosen = [next(s for s in scenarios() if s["split"] == split) for split in ("development", "held_out")]
    for i, scenario in enumerate(chosen):
        store.save_run({"id": str(i), "created": time.time(), "source": "unit", "version": "unit", "scenario_id": scenario["id"], "input": [], "output": "unit fixture", "tools": []})
        store.execute("UPDATE runs SET evaluation=? WHERE id=?", (json.dumps({"metrics": {"correctness": {"label": "fail"}}, "tool_diagnostics": []}), str(i)))
    context = evidence_for({"report": json.dumps({"case_run_ids": ["0", "1"]})})
    assert [r["run_id"] for r in context] == ["0"]


def test_expired_chat_context_is_removed():
    import threading
    from agent import api
    api.CONVERSATIONS["unit-expired"] = [{"role": "user", "content": "unit"}]
    api.LOCKS["unit-expired"] = threading.Lock()
    api.LAST_ACTIVE["unit-expired"] = time.time()-3601
    api.expire_conversations()
    assert "unit-expired" not in api.CONVERSATIONS and "unit-expired" not in api.LOCKS
