"""Isolated workflow contracts; test doubles never leave this temporary database."""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quality import store


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "live-tests.sqlite3")
    store.init()


def test_monitor_reads_phoenix_annotations_and_excludes_old_judges(monkeypatch):
    from quality import phoenix_io
    spans = [{"context": {"span_id": str(i), "trace_id": str(i)},
              "start_time": datetime.fromtimestamp(time.time()-i, timezone.utc).isoformat()} for i in (1, 2)]
    for i in (1, 2):
        store.save_run({"id": str(i), "created": time.time()-i, "source": "live", "version": "agent-v",
                        "trace_id": str(i), "span_id": str(i), "conversation_id": str(i)})
    annotations = [{"span_id": "1", "name": "correctness", "metadata": {"evaluator_version": "current"}, "result": {"label": "fail"}},
                   {"span_id": "2", "name": "correctness", "metadata": {"evaluator_version": "old"}, "result": {"label": "pass"}}]
    captured = {}
    def get_spans(**kwargs):
        captured.update(kwargs)
        return spans
    fake = SimpleNamespace(spans=SimpleNamespace(get_spans=get_spans, get_span_annotations=lambda **kw: annotations))
    monkeypatch.setattr(phoenix_io, "client", lambda: fake)
    window = phoenix_io.live_window("agent-v", "current", 20, time.time()-86400)
    assert captured["attributes"] == {"metadata.source": "live", "metadata.version": "agent-v"}
    assert window[0]["evaluation"]["metrics"]["correctness"]["label"] == "fail"
    assert window[1]["evaluation"] is None


def test_window_uses_latest_turn_per_conversation_including_unexported(monkeypatch):
    from quality import phoenix_io
    now = time.time()
    for i in range(25):
        store.save_run({"id": str(i), "created": now-i, "source": "live", "version": "agent",
                        "trace_id": str(i), "span_id": str(i), "conversation_id": "same" if i < 3 else str(i)})
    fake = SimpleNamespace(spans=SimpleNamespace(get_spans=lambda **kw: []))
    monkeypatch.setattr(phoenix_io, "client", lambda: fake)
    window = phoenix_io.live_window("agent", "judge", 20, now-86400)
    assert len(window) == 20
    assert len({r["conversation_id"] for r in window}) == 20
    assert window[0]["id"] == "0"
    assert not {"1", "2", "22", "23", "24"}.intersection(r["id"] for r in window)
    assert all(r["evaluation"] is None for r in window)


@pytest.mark.parametrize("passes,incident", [(17, False), (16, True)])
def test_three_primary_metrics_warn_strictly_below_85_percent(monkeypatch, passes, incident):
    from quality import monitoring
    from quality.config import METRICS, PRIMARY_METRICS
    rows = [{"id": str(i), "trace_id": str(i), "evaluation": {"metrics": {
        name: {"label": "pass" if i < passes else "fail"} for name in METRICS}}} for i in range(20)]
    monkeypatch.setattr(monitoring, "current_window", lambda *a, **kw: rows)
    monkeypatch.setattr(monitoring, "THRESHOLD", .85)
    monitoring.monitor("live", "agent", evaluation_version="judge")
    incidents = store.rows("SELECT payload FROM incidents")
    assert len(incidents) == (3 if incident else 0)
    if incident:
        assert {json.loads(i["payload"])["metric"] for i in incidents} == set(PRIMARY_METRICS)
        monitoring.monitor("live", "agent", evaluation_version="judge")
        assert len(store.rows("SELECT * FROM jobs WHERE kind='email'")) == 3


def test_history_cursor_keeps_all_records_when_timestamps_tie():
    from quality.dashboard import run_history
    for i in range(27):
        store.save_run({"id": f"r{i:02}", "created": 100, "source": "live", "version": "agent",
                        "trace_id": str(i), "span_id": str(i), "input": [], "tools": []})
    found, cursor = [], None
    while True:
        page = run_history(cursor=cursor, limit=12)
        found.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(found) == len(set(found)) == 27
    assert page["total"] == 27
    with pytest.raises(ValueError):
        run_history(cursor="invalid")


def test_portable_audit_evidence_does_not_create_live_telemetry():
    from quality.audit_evaluator import REFERENCES, source_rows
    rows = source_rows()
    assert set(rows) == set(REFERENCES)
    assert all(row["event"]["privacy_ok"] for row in rows.values())
    assert not store.rows("SELECT * FROM runs")
    assert not store.rows("SELECT * FROM jobs")


def test_credit_resume_reuses_evidence_and_only_requeues_current_work(monkeypatch):
    from scripts import demo
    from quality.evaluation import evaluator_version
    monkeypatch.setattr(demo, "start", lambda: None)
    monkeypatch.setattr(demo, "stop", lambda *a: None)
    store.execute("INSERT INTO benchmarks(id,kind,status,created,manifest) VALUES(?,?,?,?,?)",
                  ("base", "baseline", "provider_blocked", time.time(), json.dumps({"evaluator_version": evaluator_version()})))
    store.save_run({"id": "saved", "created": time.time(), "source": "benchmark", "version": "agent", "benchmark_id": "base"})
    store.enqueue("repair", "blocked-repair", {"incident_id": "incident", "live": True})
    store.enqueue("evaluate", "older-job", {"run_id": "unrelated"})
    store.execute("UPDATE jobs SET state='dead'")
    store.set_setting("scenario_campaign", {"status": "needs_attention"})
    store.set_setting("provider_block", {"baseline_id": "base", "incident_id": "incident"})
    original = store.rows("SELECT * FROM runs")
    demo.resume_provider()
    assert store.rows("SELECT * FROM runs") == original
    assert store.rows("SELECT state FROM jobs WHERE key='evaluate:saved'")[0]["state"] == "pending"
    assert store.rows("SELECT state FROM jobs WHERE key='older-job'")[0]["state"] == "dead"
    assert store.rows("SELECT state FROM jobs WHERE key='blocked-repair'")[0]["state"] == "pending"
    assert store.setting("provider_block") is None


def test_revision_evidence_only_contains_failed_development_cases(monkeypatch):
    from quality import remediation
    monkeypatch.setattr(remediation, "scenarios", lambda: [
        {"id": "development", "split": "development", "category": "scope"},
        {"id": "holdout", "split": "held_out", "category": "scope"}])
    rows = {key: {"scenario_id": key, "evaluation": {"metrics": {"topic_relevance": {"label": "fail"}},
        "tool_diagnostics": []}, "event": {"input": [], "output": "isolated fixture", "tools": []}}
        for key in ("development", "holdout")}
    monkeypatch.setattr(store, "get_run", rows.get)
    selected = remediation.evidence_for({"report": json.dumps({"case_run_ids": list(rows)})})
    assert [r["run_id"] for r in selected] == ["development"]


def test_benchmark_scores_never_trigger_live_alerts():
    from quality.monitoring import monitor
    assert monitor("benchmark", "agent", "benchmark-id")["status"] == "offline_experiment_only"
    assert not store.rows("SELECT * FROM incidents")


def test_live_incident_schedules_without_an_offline_baseline_and_deduplicates():
    from quality.config import agent_version
    from quality.evaluation import evaluator_version
    from quality.monitoring import schedule_repair
    store.set_setting("auto_repair", True)
    store.set_setting("evaluator_audit", {"status": "passed", "evaluator_version": evaluator_version()})
    for index, source in enumerate(("benchmark", "live", "live")):
        payload = {"source": source, "metric": "correctness", "version": agent_version(), "evaluator_version": evaluator_version(),
                   "window_run_ids": ["actual-id-in-production"], "failing_run_ids": ["actual-id-in-production"]}
        store.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                      (str(index), str(index), "open", time.time(), time.time(), json.dumps(payload)))
    schedule_repair()
    schedule_repair()
    jobs = store.rows("SELECT * FROM jobs")
    assert len(jobs) == 1
    assert json.loads(jobs[0]["payload"]) == {"incident_id": "1", "live": True}


def test_live_repair_measures_baseline_before_proposal(monkeypatch):
    from quality import remediation
    from quality.config import agent_version
    from quality.evaluation import evaluator_version
    store.set_setting("evaluator_audit", {"status": "passed", "evaluator_version": evaluator_version()})
    payload = {"source": "live", "version": agent_version(), "evaluator_version": evaluator_version()}
    store.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                  ("live-incident", "live-incident", "open", time.time(), time.time(), json.dumps(payload)))
    order = []
    monkeypatch.setattr(remediation, "live_evidence", lambda incident: [{"run_id": "local-test"}])
    monkeypatch.setattr(remediation, "create", lambda **kw: order.append("create_baseline") or "base")
    monkeypatch.setattr(remediation, "run", lambda *a, **kw: order.append("measure_baseline"))
    monkeypatch.setattr(remediation, "repair", lambda *a, **kw: order.append("propose_and_validate"))
    remediation.repair_live_incident("live-incident")
    assert order == ["create_baseline", "measure_baseline", "propose_and_validate"]


def test_arize_key_redaction_and_endpoint_boundary():
    from quality.arize_export import endpoint_url
    from quality.privacy import redact_text
    key = "ak-" + "x" * 42  # Noncredential unit fixture, never exported.
    assert key not in redact_text("Credential " + key)
    assert endpoint_url("https://otlp.arize.com/v1") == "https://otlp.arize.com/v1/traces"
    with pytest.raises(ValueError):
        endpoint_url("https://untrusted.example/v1")
