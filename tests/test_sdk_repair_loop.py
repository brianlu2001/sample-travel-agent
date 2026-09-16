"""Workflow contracts in isolated storage. No test fixtures enter real telemetry."""
import asyncio
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from quality import benchmarks, checkpoints, remediation, repair_runtime, repair_tools, repair_skills, store
from quality.config import fingerprint


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "isolated.sqlite3")
    monkeypatch.setattr(repair_tools, "configuration", lambda: {"version": "frozen"})
    monkeypatch.setattr(remediation, "STATE", tmp_path)
    monkeypatch.setattr(checkpoints, "register_candidate", lambda *_: None)
    monkeypatch.setattr(checkpoints, "targets", lambda: [{"repair_id": "repair"}])
    monkeypatch.setattr(checkpoints, "publish_status", lambda *_: None)
    store.init()
    store.execute("INSERT INTO repairs VALUES('repair','incident','diagnosing',1,1,'{}')")
    directory = tmp_path / "repairs" / "repair"
    directory.mkdir(parents=True)
    candidate = {"prompt": "Travel guidance", "functions": {}, "summary": "Minimal fix", "rationale": "Observed evidence"}
    monkeypatch.setattr(remediation, "candidate_files", lambda c, _: {"agent/prompt.py": c["prompt"], "agent/tools.py": json.dumps(c["functions"])})
    monkeypatch.setattr(remediation, "verify_artifact", lambda *a: {"passed": True})
    monkeypatch.setattr(repair_tools.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"passed": true}'))
    span = SimpleNamespace(set_attribute=lambda *a: None, set_status=lambda *a: None, end=lambda: None)
    for module in (repair_runtime, repair_skills):
        monkeypatch.setattr(module, "setup", lambda: SimpleNamespace(start_as_current_span=lambda *a, **kw: nullcontext(span)))
        monkeypatch.setattr(module, "set_io", lambda *a: None)
        monkeypatch.setattr(module, "ids", lambda *a: {"trace_id": "unit-only", "span_id": "unit-only"})
    payload = {"baseline_id": "baseline", "root_repair_id": "repair"}
    workflow = repair_tools.RepairTools("repair", directory, payload, [{"run_id": "original", "input": [{"role": "user", "content": "Plan a Paris visit"}]}])
    investigation = repair_skills.Investigation([{"run_id": "original"}])
    investigation.loaded = set(investigation.required) | {("arize-prompt-optimization", "SKILL.md"),
        ("arize-prompt-optimization", "references/optimization-meta-prompt.md"),
        ("arize-experiment", "SKILL.md"), ("phoenix-evals", "references/validation.md")}
    investigation.inspected.add("original")
    workflow.investigation = investigation
    calls = []

    def create(**kw):
        identifier = kw["benchmark_id"]
        calls.append(("create", identifier))
        manifest = {"candidate_version": workflow.revision["candidate_hash"], "case_metadata": [{"id": "dev", "split": "development"}]}
        store.execute("INSERT OR IGNORE INTO benchmarks(id,kind,parent_id,status,created,manifest) VALUES(?,?,?,?,?,?)",
                      (identifier, "targeted", "baseline", "running", 1, json.dumps(manifest)))
        return identifier

    def run(identifier, **kw):
        sealed = store.rows("SELECT report FROM benchmarks WHERE id=?", (identifier,))[0]["report"]
        if sealed:
            return json.loads(sealed)
        calls.append(("execute", identifier))
        event = {"input": "Question", "output": "Actual fixture output", "tools": []}
        evaluation = {"metrics": {n: {"label": "pass"} for n in repair_tools.PRIMARY_METRICS}}
        run_id = identifier + "-case"
        store.execute("INSERT INTO runs(id,created,source,version,benchmark_id,scenario_id,event,evaluation) VALUES(?,?,?,?,?,?,?,?)",
                      (run_id, 1, "benchmark", workflow.revision["candidate_hash"], identifier, "dev", json.dumps(event), json.dumps(evaluation)))
        result = {"case_run_ids": [run_id], "metrics": {n: {"pass_rate": .2, "pass": 3, "n": 15} for n in repair_tools.PRIMARY_METRICS},
                  "evidence_hash": fingerprint([{"id": run_id, "event": event, "evaluation": evaluation}])}
        store.execute("UPDATE benchmarks SET status='complete',report=? WHERE id=?", (json.dumps(result), identifier))
        return result

    monkeypatch.setattr(benchmarks, "create", create)
    monkeypatch.setattr(benchmarks, "run", run)
    monkeypatch.setattr(remediation, "publish", lambda *a: calls.append(("publish", a)) or "https://github.com/unit/test/pull/1")
    workflow.test_candidate = candidate
    workflow.test_calls = calls
    return workflow


def inspect_current(workflow):
    workflow.investigation.experiments_inspected = True
    workflow.investigation.inspected.update(workflow.investigation.candidate_run_ids)


def test_one_session_can_check_evaluate_revise_and_publish(workflow, tmp_path):
    session = repair_runtime.Session(workflow.investigation, tmp_path / "sdk", "Original", workflow)
    def call(name, args=None):
        result = asyncio.run(session.call(name, args or {}))
        assert not result["isError"], result
        return json.loads(result["content"][0]["text"])
    call("propose_patch", workflow.test_candidate)
    assert not session.finished
    call("run_candidate_checks")
    call("run_targeted_evaluation")
    assert asyncio.run(session.call("publish_draft_pr", {}))["isError"]
    inspect_current(workflow)
    call("propose_patch", {**workflow.test_candidate, "prompt": "Revised travel guidance"})
    assert "results" not in workflow.revision and "checks" not in workflow.revision
    assert asyncio.run(session.call("publish_draft_pr", {}))["isError"]
    call("run_candidate_checks")
    call("run_targeted_evaluation")
    inspect_current(workflow)
    call("publish_draft_pr")
    assert session.finished
    assert [c[0] for c in workflow.test_calls].count("execute") == 2
    assert [c[0] for c in workflow.test_calls].count("publish") == 1
    assert asyncio.run(session.call("propose_patch", workflow.test_candidate))["isError"]


def test_retry_after_restart_reuses_same_experiment_and_rechecks_phoenix(workflow):
    workflow.stage(workflow.test_candidate)
    workflow.checks()
    first = workflow.evaluate()
    stored = json.loads(store.rows("SELECT payload FROM repairs WHERE id='repair'")[0]["payload"])
    resumed = repair_tools.RepairTools("repair", workflow.directory, stored, workflow.evidence)
    resumed.investigation = repair_skills.Investigation(workflow.evidence)
    resumed.investigation.loaded = set(workflow.investigation.loaded)
    resumed.restore_results()
    assert resumed.evaluate() == first
    assert [c[0] for c in workflow.test_calls].count("execute") == 1
    with pytest.raises(ValueError, match="Inspect Phoenix"):
        resumed.publish()
    inspect_current(resumed)
    resumed.publish()
    assert resumed.finished


def test_hard_checks_cannot_be_skipped_and_candidate_tampering_blocks(workflow):
    workflow.stage(workflow.test_candidate)
    with pytest.raises(ValueError, match="checks"):
        workflow.evaluate()
    with pytest.raises(ValueError, match="checks"):
        workflow.publish()
    workflow.checks()
    path = workflow.directory / workflow.revision["path"]
    path.write_text(json.dumps({**workflow.test_candidate, "prompt": "tampered"}), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        workflow.evaluate()


def test_fixed_revision_budget_and_immutable_revision_artifacts(workflow):
    workflow.stage(workflow.test_candidate)
    original_path = workflow.directory / workflow.revision["path"]
    original = original_path.read_text(encoding="utf-8")
    for i in range(2):
        workflow.stage({**workflow.test_candidate, "prompt": "Revision " + str(i)})
    with pytest.raises(ValueError, match="Three candidate"):
        workflow.stage({**workflow.test_candidate, "prompt": "Fourth"})
    assert original_path.read_text(encoding="utf-8") == original
    workflow.call("finish_repair", {"reason": "No supported fix within budget"})
    assert store.rows("SELECT status FROM repairs")[0]["status"] == "awaiting_human_evidence"


def test_full_or_heldout_results_never_reach_sdk(workflow):
    workflow.stage(workflow.test_candidate)
    workflow.checks()
    workflow.evaluate()
    identifier = workflow.revision["targeted_benchmark_id"]
    row = store.rows("SELECT manifest,report FROM benchmarks WHERE id=?", (identifier,))[0]
    manifest = json.loads(row["manifest"])
    manifest["case_metadata"][0]["split"] = "held_out"
    store.execute("UPDATE benchmarks SET manifest=? WHERE id=?", (json.dumps(manifest), identifier))
    with pytest.raises(ValueError, match="development"):
        workflow.allow_results(identifier, json.loads(row["report"]))


def test_configuration_and_published_files_remain_bound_to_checks(workflow, monkeypatch):
    workflow.stage(workflow.test_candidate)
    workflow.checks()
    monkeypatch.setattr(remediation, "REGRESSION_TEST", "changed fixed checks")
    with pytest.raises(ValueError, match="exact candidate"):
        workflow.evaluate()
    monkeypatch.setattr(repair_tools, "configuration", lambda: {"version": "changed"})
    with pytest.raises(ValueError, match="Configuration changed"):
        workflow.current()


def test_modified_evidence_cannot_unlock_publication(workflow):
    workflow.stage(workflow.test_candidate)
    workflow.checks()
    workflow.evaluate()
    inspect_current(workflow)
    run_id = workflow.revision["results"]["case_run_ids"][0]
    store.execute("UPDATE runs SET event=? WHERE id=?", ('{"output":"altered"}', run_id))
    with pytest.raises(ValueError, match="evidence changed"):
        workflow.publish()
    assert not any(c[0] == "publish" for c in workflow.test_calls)


def test_publication_failure_retains_tested_candidate_for_retry(workflow, monkeypatch):
    workflow.stage(workflow.test_candidate)
    workflow.checks()
    workflow.evaluate()
    inspect_current(workflow)
    actual_publish = remediation.publish
    def unavailable(*a):
        raise RuntimeError("GitHub unavailable")
    monkeypatch.setattr(remediation, "publish", unavailable)
    with pytest.raises(RuntimeError):
        workflow.publish()
    assert not workflow.finished
    assert workflow.revision["results"]["gate"]["passed"]
    workflow.call("finish_repair", {"reason": "GitHub unavailable; retry publication later"})
    assert store.rows("SELECT status FROM repairs")[0]["status"] == "awaiting_github_access"
    payload = json.loads(store.rows("SELECT payload FROM repairs")[0]["payload"])
    workflow = repair_tools.RepairTools("repair", workflow.directory, payload, workflow.evidence)
    workflow.investigation = repair_skills.Investigation(workflow.evidence)
    workflow.restore_results()
    inspect_current(workflow)
    assert not workflow.finished
    monkeypatch.setattr(remediation, "publish", actual_publish)
    workflow.publish()
    assert workflow.finished
    assert not payload.get("publication_error")


def test_concurrent_metric_workspaces_do_not_share_candidate_state(workflow, tmp_path):
    other_path = tmp_path / "another-metric"
    other_path.mkdir()
    other = repair_tools.RepairTools("other", other_path, {"baseline_id": "baseline", "root_repair_id": "other"}, [])
    other.investigation = copy.deepcopy(workflow.investigation)
    workflow.stage(workflow.test_candidate)
    other.stage({**workflow.test_candidate, "prompt": "Other metric"})
    assert workflow.current()[0]["prompt"] != other.current()[0]["prompt"]
    assert workflow.revision["candidate_hash"] != other.revision["candidate_hash"]
