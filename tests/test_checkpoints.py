"""Cadence/decision contracts in isolated storage. No test telemetry or paid calls."""
import copy
import json
import uuid

import pytest

from quality import checkpoints as cp, checkpoint_policy as policy, full_evaluation as full, store
from quality.config import METRICS
from quality.monitoring import summarize
from quality.versions import archive_benchmarks, baseline_for

CONFIG = {"agent_version": "a"*64, "evaluator_version": "judge", "dataset_version": "dataset",
          "fixture_version": "fixture", "privacy_version": "privacy", "validation_policy_version": "legacy",
          "repetitions": 2, "models": {"agent": "model", "judge": "judge"}, "packages": {}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "checkpoints.sqlite3")
    monkeypatch.setattr(cp, "configuration", lambda: copy.deepcopy(CONFIG))
    monkeypatch.setattr(full, "configuration", lambda: copy.deepcopy(CONFIG))
    monkeypatch.setitem(policy.POLICY, "bootstrap_samples", 400)
    store.init()
    store.set_setting("serving_agent", {"version": CONFIG["agent_version"]})


def measured(identifier, version=None, at=100, kind="baseline", failures=0, suite="full"):
    rows, examples = [], []
    for i in range(20):
        scenario = "case-"+str(i)
        examples.append({"id": scenario, "split": "held_out" if i < 10 else "development"})
        for repetition in range(2):
            run_id = f"{identifier}-{i}-{repetition}"
            evaluation = {"metrics": {name: {"label": "fail" if name == "correctness" and i < failures else "pass"} for name in METRICS}}
            event = {"input": [], "output": "Unit fixture, never exported", "tools": []}
            store.execute("INSERT INTO runs(id,created,source,version,benchmark_id,scenario_id,event,evaluation) VALUES(?,?,?,?,?,?,?,?)",
                          (run_id, at, "benchmark", version or CONFIG["agent_version"], identifier, scenario, json.dumps(event), json.dumps(evaluation)))
            rows.append(store.get_run(run_id))
    report = {**summarize(rows), "case_run_ids": [r["id"] for r in rows], "tool_contract_failures": 0, "privacy_failures": 0}
    manifest = {**CONFIG, "examples": examples, "suite": suite, "candidate": "unit-only" if kind == "candidate" else None}
    store.execute("INSERT INTO benchmarks(id,kind,status,created,completed,manifest,report) VALUES(?,?,?,?,?,?,?)",
                  (identifier, kind, "complete", at, at, json.dumps(manifest), json.dumps(report)))
    archive_benchmarks()
    return rows


def target(version="b"*64):
    cp.register("candidate:root", {"version": version, "candidate": "unit-only", "configuration": CONFIG,
                                    "baseline_id": "baseline", "label": "Candidate"})
    return next(t for t in cp.targets() if t["id"] == "candidate:root")


def test_completion_regression_does_not_gate_new_checkpoints():
    before = [{"scenario_id": str(i), "evaluation": {"metrics": {
        name: {"label": "pass"} for name in METRICS}}} for i in range(12)]
    after = copy.deepcopy(before)
    for row in after:
        row["evaluation"]["metrics"]["task_completion"]["label"] = "fail"
    result = policy.compare(before, after)
    assert "task_completion" not in result["metrics"]
    assert result["conclusion"] != "regressed"


def test_retired_pr_target_is_preserved_but_not_scheduled():
    record = target()
    cp.register(record["id"], {**record, "retired": True, "retired_reason": "merged"})
    assert not cp.targets()
    assert store.rows("SELECT * FROM checkpoint_targets")


def test_daily_cadence_requires_elapsed_day_and_unmeasured_version():
    measured("baseline")
    t = target()
    assert cp.schedule_state(t, now=86499)["state"] == "waiting"
    assert cp.schedule_state(t, now=86500)["state"] == "due"
    measured("targeted", version=t["version"], at=50000, kind="targeted", suite="targeted")
    assert cp.schedule_state(t, now=86500)["state"] == "due"
    measured("full", version=t["version"], at=86600, kind="candidate")
    assert cp.schedule_state(t, now=900000)["state"] == "up_to_date"


def test_restart_does_not_duplicate_a_queued_checkpoint_and_failure_needs_attention():
    measured("baseline")
    t = target()
    store.set_setting("checkpoint_schedule_enabled", True)
    cp.tick(now=86500)
    cp.tick(now=86501)
    assert len(store.rows("SELECT * FROM jobs")) == 1
    store.execute("UPDATE jobs SET state='dead'")
    cp.tick(now=1000000)
    assert cp.schedule_state(t, 1000000)["state"] == "needs_attention"
    assert len(store.rows("SELECT * FROM jobs")) == 1


def test_changed_policy_keeps_original_baseline():
    measured("baseline")
    assert baseline_for({**CONFIG, "validation_policy_version": "new-decision-rules"}) == "baseline"
    assert baseline_for({**CONFIG, "evaluator_version": "new-judge"}) is None


def test_superseded_queued_candidate_does_not_run(monkeypatch):
    from quality import benchmarks, runtime
    measured("baseline")
    t = target()
    full.request(uuid.uuid4(), t["version"], t["id"], scheduled=True)
    job = store.claim(("full_evaluation",))
    target("c"*64)
    monkeypatch.setattr(runtime, "prepare_agent", lambda: {"version": CONFIG["agent_version"]})
    monkeypatch.setattr(benchmarks, "create", lambda **kw: pytest.fail("Superseded request must not spend credits"))
    assert full.execute(job["payload"]) == {"superseded": True}


def test_small_drop_is_recorded_as_inconclusive_and_repetitions_are_clustered():
    before = measured("before")
    after = measured("after", version="b"*64, kind="candidate", failures=1)
    result = policy.compare(before, after)
    metric = result["metrics"]["correctness"]
    assert metric["paired_scenarios"] == 20
    assert metric["observed_decline"] and metric["delta"] == pytest.approx(-.05)
    assert metric["conclusion"] == "inconclusive"


def test_confirmed_regression_creates_one_incident_email_and_followup(monkeypatch):
    from quality.evaluation import evaluator_version
    measured("baseline")
    measured("candidate", version="b"*64, kind="candidate", failures=20)
    t = {**target(), "repair_id": "prior"}
    store.execute("INSERT INTO repairs VALUES(?,?,?,?,?,?)", ("prior", "initial", "pr_open", 1, 1, json.dumps({"revision_number": 1})))
    store.set_setting("auto_repair", True)
    store.set_setting("evaluator_audit", {"status": "passed", "evaluator_version": evaluator_version()})
    decision = cp.record_result("candidate", t)
    cp.record_result("candidate", t)
    assert decision["conclusion"] == "regressed"
    assert len(store.rows("SELECT * FROM incidents")) == 1
    assert len(store.rows("SELECT * FROM jobs WHERE kind='email'")) == 1
    jobs = store.rows("SELECT * FROM jobs WHERE kind='repair'")
    assert len(jobs) == 1 and json.loads(jobs[0]["payload"])["revision_of"] == "prior"


def test_proposal_limit_and_newer_revision_stop_automatic_followups():
    from quality.evaluation import evaluator_version
    measured("baseline")
    measured("candidate", version="b"*64, kind="candidate", failures=20)
    t = {**target(), "repair_id": "prior"}
    store.execute("INSERT INTO repairs VALUES(?,?,?,?,?,?)", ("prior", "initial", "pr_open", 1, 1, json.dumps({"revision_number": 3})))
    store.set_setting("auto_repair", True)
    store.set_setting("evaluator_audit", {"status": "passed", "evaluator_version": evaluator_version()})
    cp.record_result("candidate", t)
    assert not store.rows("SELECT * FROM jobs WHERE kind='repair'")


def test_human_approval_requires_uncertainty_ack_and_becomes_comparison_anchor():
    measured("baseline")
    measured("candidate", version="b"*64, kind="candidate", failures=1)
    t = target()
    d = cp.record_result("candidate", t)
    assert d["conclusion"] == "inconclusive"
    with pytest.raises(ValueError, match="explicit acceptance"):
        cp.approve("candidate", t["version"], "", False)
    cp.approve("candidate", t["version"], "Reviewed these isolated test cases", True)
    assert cp.last_approved(CONFIG, cp.full_records(CONFIG)) == "candidate"
    assert baseline_for(CONFIG) == "baseline"
    target("c"*64)
    with pytest.raises(ValueError, match="newer revision"):
        cp.approve("candidate", t["version"], "Reviewed", True)


def test_targeted_selection_includes_incident_and_no_holdout():
    from quality.targeted import select
    from quality.evaluation import scenarios
    held_out = {s["id"] for s in scenarios() if s["split"] == "held_out"}
    evidence = [{"run_id": "isolated", "input": [{"role": "user", "content": "Plan a short visit to Paris."}]}]
    examples = select(evidence)
    assert len(examples) == 15
    assert examples[0]["id"].startswith("incident-")
    assert all(e["split"] == "development" and e["id"] not in held_out for e in examples)
    assert len({e["category"] for e in examples}) >= 7


@pytest.mark.parametrize("invariants_pass", [True, False])
@pytest.mark.parametrize("operator_review", [True, False])
def test_repair_uses_targeted_signals_and_only_hard_gates_block(tmp_path, monkeypatch, invariants_pass, operator_review):
    from types import SimpleNamespace
    from quality import remediation, evaluation, config, benchmarks, repair_tools
    from quality.config import fingerprint
    rows = measured("baseline")
    report = json.loads(store.rows("SELECT report FROM benchmarks WHERE id='baseline'")[0]["report"])
    report["evidence_hash"] = fingerprint([{k: r[k] for k in ("id", "event", "evaluation")} for r in rows])
    store.execute("UPDATE benchmarks SET report=? WHERE id='baseline'", (json.dumps(report),))
    incident_data = {"source": "live"}
    if operator_review:
        incident_data["trigger"] = "operator_review"
    store.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,NULL)", ("incident", "incident", "open", 1, 1, json.dumps(incident_data)))
    monkeypatch.setattr(remediation, "STATE", tmp_path)
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(evaluation, "evaluator_version", lambda: "judge")
    monkeypatch.setattr(remediation, "agent_version", lambda: CONFIG["agent_version"])
    evidence = [{"run_id": "isolated", "input": [{"role": "user", "content": "Plan a visit to Paris"}]}]
    monkeypatch.setattr(remediation, "live_evidence", lambda _: evidence)
    candidate = {"prompt": "Travel only", "functions": {}, "summary": "Isolated test patch", "rationale": "Test"}
    def sdk_loop(*args, workflow):
        workflow.investigation = SimpleNamespace(loaded={("arize-experiment", "SKILL.md"), ("phoenix-evals", "references/validation.md")}, usage=[])
        workflow.stage(copy.deepcopy(candidate))
        if not workflow.call("run_candidate_checks", {})["passed"]:
            workflow.call("finish_repair", {"reason": "Hard checks failed; review needed"})
            return
        workflow.call("run_targeted_evaluation", {})
        workflow.call("publish_draft_pr", {})
    monkeypatch.setattr(remediation, "propose", sdk_loop)
    monkeypatch.setattr(repair_tools.RepairTools, "allow_results", lambda *a: None)
    monkeypatch.setattr(repair_tools.RepairTools, "require_review", lambda *a: None)
    monkeypatch.setattr(remediation.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps({"passed": invariants_pass})))
    monkeypatch.setattr(remediation, "candidate_files", lambda *a: {"agent/tools.py": "# test", "agent/prompt.py": "# test"})
    monkeypatch.setattr(remediation, "verify_artifact", lambda *a: {"passed": True})
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs["kind"] == "targeted" and kwargs["repetitions"] == 1
        assert len(kwargs["examples"]) == 15
        return "targeted"
    monkeypatch.setattr(benchmarks, "create", create)
    # Low semantic scores are recorded development signals, not automatic rejection.
    monkeypatch.setattr(benchmarks, "run", lambda *a, **kw: {"metrics": {name: {"pass_rate": .2, "pass": 3, "n": 15} for name in METRICS}, "evidence_hash": "unit-only", "case_run_ids": []})
    monkeypatch.setattr(remediation, "publish", lambda *a: "https://github.com/unit/test/pull/1")
    monkeypatch.setattr(cp, "recover_pr_head", lambda *a: None)
    identifier = remediation.repair("incident", "baseline")
    row = store.rows("SELECT * FROM repairs WHERE id=?", (identifier,))[0]
    assert row["status"] == ("pr_open" if invariants_pass else "awaiting_human_evidence")
    assert len(calls) == (1 if invariants_pass else 0)
    assert json.loads(row["payload"])["trigger"] == ("operator_review" if operator_review else "live_chat_incident")
    if invariants_pass:
        body = (tmp_path / "repairs" / identifier / "pr-description.md").read_text(encoding="utf-8")
        assert ("operator-requested review" in body) == operator_review
        assert ("Triggered by a rolling live traffic quality incident" in body) != operator_review


def test_stale_pr_head_blocks_checkpoint_approval(monkeypatch):
    measured("baseline")
    measured("candidate", version="b"*64, kind="candidate", failures=1)
    t = target()
    cp.record_result("candidate", t)
    def changed(_):
        raise ValueError("PR head changed")
    monkeypatch.setattr(cp, "verify_target", changed)
    with pytest.raises(ValueError, match="PR head changed"):
        cp.approve("candidate", t["version"], "Reviewed", True)
    assert not store.rows("SELECT * FROM checkpoint_approvals")
