"""Queue state must not be presented as proof of email delivery."""
import uuid

import pytest

from quality import store
from quality.email_history import delivery_history


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "mail.sqlite3")
    store.init()


@pytest.mark.parametrize("state,error,expected", [
    ("pending", None, "queued"), ("running", None, "sending"),
    ("pending", "ConnectionRefusedError", "retrying"), ("dead", "SMTPException", "failed"),
    ("done", None, "unconfirmed"),
])
def test_job_state_is_visible_without_claiming_delivery(state, error, expected):
    store.enqueue("email", "incident:one", {"incident_id": "one", "event": "opened"})
    store.execute("UPDATE jobs SET state=?,error=?,attempts=2", (state, error))
    event = delivery_history()[0]
    assert event["status"] == expected
    assert event["incident_id"] == "one"
    assert event["queued_at"] > 0
    assert event["attempts"] == 2
    assert event["accepted"] is None


def test_receipt_visible_without_local_inbox_and_survives_restart():
    store.enqueue("email", "incident:one", {"incident_id": "one", "event": "opened"})
    identifier = f"<{uuid.uuid5(uuid.NAMESPACE_URL, 'incident:one').hex}@quality.local>"
    store.execute("INSERT INTO email_receipts VALUES(?,?,?,?,?)", (identifier, 200, "configured SMTP relay", 250, "Accepted"))
    before = delivery_history()
    assert before[0]["status"] == "sent"
    assert before[0]["incident_id"] == "one"
    assert before[0]["recipient"] is None  # Never invent missing historical headers.
    store.init()
    assert delivery_history() == before


def test_inbox_only_retained_as_received_and_failed_receipt_not_sent():
    store.execute("INSERT INTO emails VALUES(?,?,?,?,?,?)", ("old", 100, "sender", "recipient", "subject", "body"))
    store.execute("INSERT INTO email_receipts VALUES(?,?,?,?,?)", ("failed", 200, "relay", 550, "Rejected"))
    events = {e["id"]: e for e in delivery_history()}
    assert events["old"]["status"] == "received"
    assert events["failed"]["status"] != "sent"
