"""Filter real-record shapes in an isolated journal; never export test telemetry."""
import json

import pytest

from quality import dashboard, store
from quality.config import METRICS


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(dashboard, "evaluator_version", lambda: "current")
    store.init()


def record(identifier, labels=None, version="current", conversation=None):
    store.save_run({"id": identifier, "created": 100, "source": "live", "version": "agent",
                    "conversation_id": conversation or identifier, "input": [], "tools": []})
    if labels is not None:
        evaluation = {"version": version, "metrics": {name: {"label": labels.get(name, "pass")} for name in METRICS}}
        store.execute("UPDATE runs SET evaluation=? WHERE id=?", (json.dumps(evaluation), identifier))


def test_metric_and_label_filter_before_pagination_and_count():
    for i in range(25):
        record(f"r{i:02}", {"groundedness": "fail" if i % 2 else "pass"})
    identifiers, cursor = [], None
    while True:
        page = dashboard.run_history(metric="groundedness", label="fail", limit=3, cursor=cursor)
        assert page["total"] == 12
        identifiers.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(set(identifiers)) == len(identifiers) == 12
    assert all(int(identifier[1:]) % 2 for identifier in identifiers)


def test_any_primary_failure_excludes_completion_but_diagnostic_is_searchable():
    record("completion", {"task_completion": "fail"})
    record("relevance", {"topic_relevance": "fail"})
    assert [r["id"] for r in dashboard.run_history(label="fail")["items"]] == ["relevance"]
    assert dashboard.run_history(metric="task_completion", label="fail")["total"] == 1


def test_pending_unknown_na_and_old_evaluators_are_distinct():
    record("pending")
    record("unknown", {"correctness": "unknown"})
    record("na", {"correctness": "not_applicable"})
    record("old", {"correctness": "fail"}, version="old")
    for label, identifier in [("pending", "pending"), ("unknown", "unknown"), ("not_applicable", "na"), ("fail", "old")]:
        found = dashboard.run_history(metric="correctness", label=label)["items"]
        assert [r["id"] for r in found] == [identifier]
    assert dashboard.run_history(label="fail", evaluator="current")["total"] == 0
    assert dashboard.run_history(label="pending", evaluator="current")["total"] == 1
    assert dashboard.run_history(label="fail")["items"][0]["requires_revalidation"]


def test_conversation_inspection_keeps_earlier_failed_turn():
    record("a-first", {"correctness": "fail"}, conversation="conversation")
    record("b-later", {}, conversation="conversation")
    record("unrelated", {})
    page = dashboard.run_history(conversation_id="conversation")
    assert [r["id"] for r in page["items"]] == ["b-later", "a-first"]
    assert dashboard.run_history(conversation_id="conversation", label="fail")["total"] == 1


@pytest.mark.parametrize("filters", [{"metric": "bogus"}, {"label": "bogus"}, {"evaluator": "bogus"}])
def test_invalid_filters_rejected(filters):
    with pytest.raises(ValueError):
        dashboard.run_history(**filters)


def test_api_accepts_metric_label_and_conversation_filters():
    from fastapi.testclient import TestClient
    from agent.api import app
    record("match", {"groundedness": "fail"}, conversation="conversation")
    record("other", {"groundedness": "fail"})
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/quality/runs", params={"metric": "groundedness", "label": "fail", "conversation_id": "conversation"})
        assert response.status_code == 200
        assert [r["id"] for r in response.json()["items"]] == ["match"]
        assert client.get("/quality/runs?label=invalid").status_code == 422
