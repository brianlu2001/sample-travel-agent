"""Manual checkpoint control tests: isolated storage; no model calls or exports."""
import copy
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from quality import store
from quality import full_evaluation as full
from quality.versions import archive_benchmarks, baseline_for, saved_benchmarks

VERSION = "a" * 64
CONFIG = {"agent_version": VERSION, "evaluator_version": "judge", "dataset_version": "dataset",
          "fixture_version": "fixture", "privacy_version": "privacy", "validation_policy_version": "policy",
          "repetitions": 2, "models": {"agent": "model", "judge": "judge"}, "packages": {}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "manual-evaluation.sqlite3")
    monkeypatch.setattr(full, "configuration", lambda: copy.deepcopy(CONFIG))
    store.init()
    store.set_setting("serving_agent", {"version": VERSION})


def sealed(identifier, *, kind="baseline", at=1, config=None, status="complete"):
    manifest = {**(config or CONFIG), "candidate": None}
    report = {"metrics": {"correctness": {"pass_rate": .5}}, "evidence_hash": "test-only"}
    store.execute("INSERT INTO benchmarks(id,kind,status,created,completed,manifest,report) VALUES(?,?,?,?,?,?,?)",
                  (identifier, kind, status, at, at, json.dumps(manifest), json.dumps(report)))


def test_versions_are_immutable_and_manual_checkpoints_do_not_replace_anchor():
    sealed("original")
    sealed("incomplete", status="evaluation_failed")
    archive_benchmarks()
    original = saved_benchmarks()[0]
    sealed("later", at=2)
    sealed("checkpoint", at=3, kind="checkpoint")
    archive_benchmarks()
    assert baseline_for(CONFIG) == "original"
    assert saved_benchmarks()[0] == original
    assert len(saved_benchmarks()) == 3
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.execute("UPDATE benchmark_versions SET record='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.execute("DELETE FROM benchmark_versions")
    assert baseline_for({**CONFIG, "evaluator_version": "new-judge"}) is None


def test_duplicate_request_is_idempotent_even_after_completion():
    request_id = uuid.uuid4()
    first = full.request(request_id, VERSION)
    assert full.request(request_id, VERSION)["reused"]
    store.execute("UPDATE jobs SET state='done'")
    assert full.request(request_id, VERSION)["id"] == first["id"]
    assert len(store.rows("SELECT * FROM jobs")) == 1


def test_concurrent_clicks_queue_only_one_paid_run():
    def one(_):
        try:
            return full.request(uuid.uuid4(), VERSION)
        except full.EvaluationUnavailable:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, range(4)))
    assert sum(result is not None for result in results) == 1
    assert len(store.rows("SELECT * FROM jobs")) == 1


@pytest.mark.parametrize("reason", ["version", "provider", "repair", "benchmark"])
def test_unsafe_or_busy_request_queues_nothing(reason):
    if reason == "version":
        store.set_setting("serving_agent", {"version": "b" * 64})
    elif reason == "provider":
        store.set_setting("provider_block", {"message": "recorded block"})
    elif reason == "repair":
        store.enqueue("repair", "existing-repair", {})
    else:
        sealed("running", status="running")
    with pytest.raises(full.EvaluationUnavailable):
        full.request(uuid.uuid4(), VERSION)
    assert not store.rows("SELECT * FROM jobs WHERE kind='full_evaluation'")


def test_worker_checks_frozen_configuration_before_any_execution(monkeypatch):
    from quality import benchmarks, runtime
    full.request(uuid.uuid4(), VERSION)
    job = store.claim(("full_evaluation",))
    monkeypatch.setattr(full, "configuration", lambda: {**CONFIG, "dataset_version": "changed"})
    monkeypatch.setattr(runtime, "prepare_agent", lambda: {"version": VERSION})
    monkeypatch.setattr(benchmarks, "create", lambda **kw: pytest.fail("Must not create experiment"))
    with pytest.raises(full.EvaluationUnavailable):
        full.execute(job["payload"])


def test_interrupted_run_with_changed_inputs_is_failed_without_losing_partial_evidence(monkeypatch):
    full.request(uuid.uuid4(), VERSION)
    job = store.claim(("full_evaluation",))
    sealed(job["payload"]["id"], status="running")
    monkeypatch.setattr(full, "configuration", lambda: {**CONFIG, "dataset_version": "changed"})
    with pytest.raises(full.EvaluationUnavailable):
        full.execute(job["payload"])
    row = store.rows("SELECT * FROM benchmarks")[0]
    assert row["status"] == "evaluation_failed"
    assert json.loads(row["report"])["evidence_hash"] == "test-only"


def test_worker_uses_fixed_anchor_and_resumes_same_experiment(monkeypatch):
    from quality import benchmarks, runtime
    sealed("anchor")
    archive_benchmarks()
    full.request(uuid.uuid4(), VERSION)
    job = store.claim(("full_evaluation",))
    calls = []
    monkeypatch.setattr(runtime, "prepare_agent", lambda: {"version": VERSION})
    monkeypatch.setattr(benchmarks, "create", lambda **kw: calls.append(kw))
    monkeypatch.setattr(benchmarks, "run", lambda identifier: calls.append(identifier))
    full.execute(job["payload"])
    full.execute(job["payload"])
    assert calls[0] == calls[2] == {"kind": "checkpoint", "parent_id": "anchor", "benchmark_id": job["payload"]["id"]}
    assert calls[1] == calls[3] == job["payload"]["id"]


def test_api_returns_queue_acknowledgement_and_saved_version_without_model_calls():
    from fastapi.testclient import TestClient
    from agent.api import app
    client = TestClient(app, base_url="http://localhost")
    request = {"request_id": str(uuid.uuid4()), "expected_version": VERSION}
    response = client.post("/quality/benchmarks", json=request)
    assert response.status_code == 202 and response.json()["queued"]
    assert client.post("/quality/benchmarks", json=request).json()["reused"]
    assert client.post("/quality/benchmarks", json={**request, "request_id": str(uuid.uuid4())}).status_code == 409
    assert client.post("/quality/benchmarks", json=request, headers={"origin": "https://untrusted.example"}).status_code == 403
    sealed("archive")
    archive_benchmarks()
    record = client.get("/quality/benchmarks/archive/version")
    assert record.status_code == 200 and record.json()["report"]["evidence_hash"] == "test-only"
    assert client.get("/quality/benchmarks/missing/version").status_code == 404
