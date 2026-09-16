"""History reads must survive deployments without mixing agent or judge versions."""
import json

import pytest

from quality import dashboard, store
from quality.config import PRIMARY_METRICS
from quality.live_history import assessment_for, performance_history
from quality.versions import register_running_agent


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(dashboard, "evaluator_version", lambda: "judge-new")
    store.init()


def result(judge="judge-new", label="pass"):
    return {"version": judge, "metrics": {name: {"label": label} for name in PRIMARY_METRICS}}


def record(identifier, *, agent="agent-old", conversation=None, created=100, source="live", evaluation=None):
    store.save_run({"id": identifier, "created": created, "source": source, "version": agent,
                    "conversation_id": conversation or identifier, "trace_id": identifier, "input": [], "tools": []})
    if evaluation:
        store.execute("UPDATE runs SET evaluation=? WHERE id=?", (json.dumps(evaluation), identifier))


def archive(identifier, evaluation, started=1):
    store.execute("INSERT INTO evaluation_history VALUES(?,?,?,?,?)",
                  (identifier, evaluation["version"], started, json.dumps(evaluation), started+1))


def test_restart_and_new_deployment_keep_old_live_scores_and_all_traces():
    for i in range(25):
        record(str(i), created=i+1, evaluation=result(label="fail" if i < 10 else "pass"))
    record("old-turn", created=0, conversation="24", evaluation=result(label="fail"))
    record("offline", source="benchmark", evaluation=result(label="fail"))
    record("simulation", source="scenario", evaluation=result(label="fail"))
    snapshot = {"version": "agent-old", "model": "test", "files": {}}
    register_running_agent(snapshot)
    before = performance_history("agent-old", "judge-new")["cohorts"][0]
    store.init()  # Startup creates tables if absent; it must never clear evidence.
    register_running_agent(snapshot)
    assert performance_history("agent-old", "judge-new")["cohorts"][0] == before
    register_running_agent({**snapshot, "version": "agent-new"})
    after = performance_history("agent-new", "judge-new")["cohorts"][0]
    assert not after["current_agent"]
    assert after["report"] == before["report"]
    assert after["conversation_count"] == 25
    assert after["report"]["total"] == 20
    assert after["report"]["metrics"]["correctness"]["pass_rate"] == .75
    assert dashboard.run_history(agent_version="agent-old", source="live")["total"] == 26
    record("new-agent", agent="agent-new", created=200, evaluation=result())
    cohorts = performance_history("agent-new", "judge-new")["cohorts"]
    assert len(cohorts) == 2
    assert cohorts[0]["report"]["total"] == 1
    assert cohorts[1]["report"] == before["report"]


def test_previous_judge_remains_inspectable_and_retries_are_not_extra_votes():
    record("run", evaluation=result())
    archive("run", result("judge-old", "pass"), 1)
    archive("run", result("judge-old", "fail"), 2)
    archive("run", result("judge-new", "fail"), 3)
    cohorts = {c["evaluator_version"]: c for c in performance_history("agent-old", "judge-new")["cohorts"]}
    assert cohorts["judge-old"]["report"]["metrics"]["correctness"]["pass_rate"] == 0
    assert cohorts["judge-new"]["report"]["metrics"]["correctness"]["pass_rate"] == 1
    assert all(c["conversation_count"] == 1 for c in cohorts.values())
    page = dashboard.run_history(agent_version="agent-old", evaluation_version="judge-old", label="fail")
    assert page["total"] == 1
    assert page["items"][0]["evaluator_version"] == "judge-old"
    assert dashboard.run_history(evaluation_version="judge-new", label="fail")["total"] == 0
    assert assessment_for(store.get_run("run"), "judge-old")["metrics"]["correctness"]["label"] == "fail"
    assert store.get_run("run")["evaluation"] == result()


def test_pending_is_not_fabricated_for_an_unrecorded_historical_judge():
    record("pending")
    cohorts = performance_history("agent-old", "judge-new")["cohorts"]
    assert len(cohorts) == 1
    assert cohorts[0]["report"]["metrics"]["correctness"]["pending"] == 1
    assert cohorts[0]["report"]["metrics"]["correctness"]["pass_rate"] is None
    assert dashboard.run_history(evaluation_version="judge-old")["total"] == 0
    assert dashboard.run_history(evaluation_version="judge-new", label="pending")["total"] == 1


def test_api_filters_and_detail_use_the_same_historical_judgment(monkeypatch):
    from fastapi.testclient import TestClient
    from agent.api import app
    from quality import trace_view
    monkeypatch.setattr(trace_view, "trace_details", lambda _: {})
    for i in range(3):
        record(str(i), evaluation=result())
        archive(str(i), result("judge-old", "fail"))
    record("other-agent", agent="different", evaluation=result("judge-old", "fail"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        params = {"agent_version": "agent-old", "evaluation_version": "judge-old", "label": "fail", "limit": 1}
        seen = []
        while True:
            response = client.get("/quality/runs", params=params)
            assert response.status_code == 200
            page = response.json()
            assert page["total"] == 3
            identifier = page["items"][0]["id"]
            seen.append(identifier)
            detail = client.get(f"/quality/runs/{identifier}?evaluation_version=judge-old").json()
            assert detail["evaluation"]["metrics"]["correctness"]["label"] == "fail"
            if not page["next_cursor"]:
                break
            params["cursor"] = page["next_cursor"]
        assert len(set(seen)) == 3
