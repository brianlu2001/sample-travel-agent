"""Campaign contracts use isolated storage; none of these fixtures are exported."""
import json
import time

import pytest

from quality import store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "scenario-test.sqlite3")
    store.init()


def test_plan_only_development_with_varied_categories():
    from quality.scenario_stream import plan, scenarios
    by_id = {s["id"]: s for s in scenarios()}
    ids = plan()
    assert len(ids) == len(set(ids)) == 45
    assert all(by_id[i]["split"] == "development" for i in ids)
    assert len({by_id[i]["category"] for i in ids[:7]}) == 7


def test_scenarios_use_separate_phoenix_source(monkeypatch):
    from quality import monitoring, phoenix_io
    captured = []
    monkeypatch.setattr(phoenix_io, "live_window", lambda *args, **kwargs: captured.append(kwargs) or [])
    monitoring.current_window("scenario", "agent", evaluation_version="judge")
    monitoring.current_window("live", "agent", evaluation_version="judge")
    assert captured == [{"source": "scenario"}, {"source": "live"}]


def test_breach_pauses_scenarios_and_pr_stops_campaign():
    from quality.scenario_stream import start, tick
    current = start()
    data = {"source": "scenario", "version": current["version"], "evaluator_version": current["evaluator_version"]}
    store.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                  ("incident", "flag", "open", time.time(), time.time(), json.dumps(data)))
    tick()
    assert store.setting("scenario_campaign")["status"] == "awaiting_repair"
    assert not store.rows("SELECT * FROM jobs WHERE kind='scenario'")
    store.execute("INSERT INTO repairs VALUES(?,?,?,?,?,?)", ("repair", "incident", "pr_open", time.time(), time.time(), json.dumps({"pr_url": "https://example.com/pr"})))
    tick()
    assert store.setting("scenario_campaign")["status"] == "complete"


def test_rejection_allows_one_distinct_revision_then_stops():
    from quality.scenario_stream import start, tick
    start()
    store.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                  ("incident", "flag", "open", time.time(), time.time(), json.dumps({"source": "scenario"})))
    store.execute("INSERT INTO repairs VALUES(?,?,?,?,?,?)", ("initial", "incident", "rejected", time.time(), time.time(), json.dumps({"baseline_id": "base"})))
    tick()
    tick()
    assert len(store.rows("SELECT * FROM jobs WHERE kind='repair'")) == 1
    store.execute("INSERT INTO repairs VALUES(?,?,?,?,?,?)", ("revision", "incident", "rejected", time.time()+1, time.time()+1, json.dumps({"baseline_id": "base", "revision_of": "initial"})))
    tick()
    assert store.setting("scenario_campaign")["status"] == "rejected"
    assert len(store.rows("SELECT * FROM jobs WHERE kind='repair'")) == 1


def test_custom_campaign_continues_after_incident_and_stops_at_requested_count(monkeypatch):
    from quality.scenario_stream import start, tick
    monkeypatch.setattr("quality.privacy.safe_payload", lambda value: value)
    current = start([{"id": "custom-1", "messages": ["Plan a trip"]}], stop_on_incident=False)
    store.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                  ("flag", "flag", "open", time.time(), time.time(), json.dumps({"source": "scenario"})))
    tick()
    job = store.claim(("scenario",))
    assert job and job["payload"]["campaign_id"] == current["id"]
    store.finish(job)
    current.update(completed=1)
    store.set_setting("scenario_campaign", current)
    tick()
    assert store.setting("scenario_campaign")["status"] == "complete"
    assert len(store.rows("SELECT * FROM jobs WHERE kind='scenario'")) == 1


def test_custom_campaign_rejects_duplicate_conversation_ids():
    from quality.scenario_stream import start
    case = {"id": "same", "messages": ["Hello"]}
    with pytest.raises(ValueError, match="distinct"):
        start([case, case])


def test_sender_receipt_only_after_actual_smtp_acceptance(monkeypatch):
    import asyncio
    import socket
    from aiosmtpd.controller import Controller
    from quality import mailbox
    # A genuine local SMTP round trip on an ephemeral port, isolated from the
    # running demo inbox, trace exporter and production incident store.
    monkeypatch.setattr(mailbox, "safe_payload", lambda value: value)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    loop = asyncio.new_event_loop()
    controller = Controller(mailbox.Inbox(), hostname="127.0.0.1", port=port, loop=loop)
    controller.start()
    try:
        monkeypatch.setenv("QUALITY_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("QUALITY_SMTP_PORT", str(port))
        monkeypatch.setenv("QUALITY_SMTP_STARTTLS", "false")
        monkeypatch.delenv("QUALITY_SMTP_USERNAME", raising=False)
        store.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                      ("smtp-test", "smtp-test", "open", time.time(), time.time(), json.dumps({"metric": "correctness", "source": "isolated_test"})))
        mailbox.send_alert({"incident_id": "smtp-test", "event": "opened"}, "isolated-smtp-test")
        rows = store.rows("SELECT * FROM emails")
        receipts = store.rows("SELECT * FROM email_receipts")
        assert len(rows) == len(receipts) == 1
        assert rows[0]["id"] == receipts[0]["id"]
        assert receipts[0]["smtp_code"] == 250
        assert receipts[0]["smtp_response"] == "Message accepted for local delivery"
    finally:
        controller.stop()
    with pytest.raises(OSError):
        mailbox.send_alert({"incident_id": "smtp-test", "event": "opened"}, "failed-smtp-test")
    assert len(store.rows("SELECT * FROM email_receipts")) == 1
