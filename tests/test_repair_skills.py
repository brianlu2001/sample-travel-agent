import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from quality import repair_skills as skills, remediation, repair_runtime as runtime


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    span = SimpleNamespace(set_attribute=lambda *a: None, set_status=lambda *a: None, end=lambda: None)
    tracer = SimpleNamespace(start_as_current_span=lambda *a, **k: nullcontext(span), start_span=lambda *a, **k: span)
    for module in (skills, remediation, runtime):
        monkeypatch.setattr(module, "setup", lambda: tracer)
        monkeypatch.setattr(module, "set_io", lambda *a: None)
        monkeypatch.setattr(module, "ids", lambda *a: {"trace_id": "isolated", "span_id": "isolated"})
    monkeypatch.setattr(skills, "error_status", lambda *a: None)
    monkeypatch.setattr(remediation, "STATE", tmp_path)
    directory = tmp_path / "benchmarks" / "baseline"
    directory.mkdir(parents=True)
    (directory / "tools.py").write_text("# frozen tool source", encoding="utf-8")
    (directory / "prompt.py").write_text("SYSTEM_PROMPT='Test'", encoding="utf-8")
    monkeypatch.setattr(skills.store, "get_run", lambda identifier: {
        "event": {"trace_id": "trace", "span_id": "root"}})
    spans = [dict(context={"span_id": "root"}, start_time="2026-09-16", parent_id=None,
                  name="agent", span_kind="AGENT", status_code="OK",
                  attributes={"output.value": "Please contact person@example.com"}),
             dict(context={"span_id": "tool"}, start_time="2026-09-17", parent_id="root",
                  name="lookup", span_kind="TOOL", status_code="OK", attributes={"output.value": "42"})]
    annotations = [{"span_id": "root", "name": "correctness", "annotator_kind": "LLM", "result": {"label": "fail"}},
                   {"span_id": "root", "name": "human_correctness", "annotator_kind": "HUMAN", "result": {"label": "pass"}}]
    monkeypatch.setattr(skills, "client", lambda: SimpleNamespace(spans=SimpleNamespace(
        get_spans=lambda **kw: spans, get_span_annotations=lambda **kw: annotations)))
    return {"run_id": "allowed", "tools": [{"name": "lookup"}]}


def load_required(session):
    for skill, document in sorted(session.required):
        session.document_loaded(skill, document)


def test_pinned_bundles_are_complete_for_the_runtime_catalog():
    assert {s["name"] for s in skills.catalog()} == {"phoenix-evals", "phoenix-tracing", "arize-prompt-optimization", "arize-experiment"}
    for entry in skills.catalog():
        for document in entry["documents"]:
            loaded = skills.read_document(entry["name"], document)
            assert loaded["source"].startswith("https://raw.githubusercontent.com/Arize-ai/")
            assert entry["revision"] in loaded["source"]
            assert len(loaded["sha256"]) == 64 and loaded["content"]


def test_skills_load_only_when_the_investigation_requires_them():
    plain = skills.Investigation([{"run_id": "a"}])
    assert all(s == "phoenix-evals" for s, _ in plain.required)
    tools = skills.Investigation([{"run_id": "a", "tools": ["lookup"]}], {"previous_candidate": {}})
    assert ("phoenix-tracing", "references/span-tool.md") in tools.required
    assert ("phoenix-evals", "references/validation.md") in tools.required


def test_skill_traversal_and_tampering_are_blocked(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        skills.read_document("phoenix-evals", "../../.env")
    with pytest.raises(ValueError):
        skills.read_document("phoenix-cli")
    manifest = json.loads((skills.BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "phoenix-evals").mkdir()
    (tmp_path / "phoenix-evals" / "SKILL.md").write_text("altered guidance", encoding="utf-8")
    monkeypatch.setattr(skills, "BUNDLE", tmp_path)
    with pytest.raises(ValueError, match="integrity"):
        skills.read_document("phoenix-evals")


def test_trace_access_is_scoped_redacted_and_preserves_annotation_provenance(isolated):
    session = skills.Investigation([isolated])
    assert session.ready()
    assert "error" in session.call("inspect_phoenix_trace", {"run_id": "allowed"})
    load_required(session)
    assert session.ready()  # Reading documentation alone is insufficient.
    assert "error" in session.call("inspect_phoenix_trace", {"run_id": "held-out-or-other-tenant"})
    result = session.call("inspect_phoenix_trace", {"run_id": "allowed"})
    assert "person@example.com" not in json.dumps(result)
    assert {a["annotator_kind"] for a in result["annotations"]} == {"LLM", "HUMAN"}
    assert session.ready() is None
    assert "error" in session.call("delete_spans", {})
    assert session.audit()["inspected_run_ids"] == ["allowed"]



def test_model_cannot_skip_skills_or_phoenix_inspection_before_proposing(isolated, tmp_path):
    investigation = skills.Investigation([isolated])
    session = runtime.Session(investigation, tmp_path, "Original prompt")
    candidate = {"prompt": "Travel only", "functions": {}, "summary": "Test patch", "rationale": "Recorded evidence"}
    assert asyncio.run(session.call("propose_patch", candidate))["isError"]
    load_required(investigation)
    assert asyncio.run(session.call("propose_patch", candidate))["isError"]
    assert "error" not in investigation.call("inspect_phoenix_trace", {"run_id": "allowed"})
    assert asyncio.run(session.call("propose_patch", candidate))["isError"]  # Prompt edits need the optimization skill.
    for doc in ("SKILL.md", "references/optimization-meta-prompt.md"):
        investigation.document_loaded("arize-prompt-optimization", doc)
    assert not asyncio.run(session.call("propose_patch", candidate))["isError"]
    assert session.candidate == candidate
    assert asyncio.run(session.call("propose_patch", {**candidate, "prompt": "overwrite"}))["isError"]
    assert session.candidate == candidate


def test_judge_disagreement_returns_review_instead_of_patch(isolated, tmp_path):
    investigation = skills.Investigation([isolated])
    session = runtime.Session(investigation, tmp_path, "Original prompt")
    load_required(investigation)
    investigation.call("inspect_phoenix_trace", {"run_id": "allowed"})
    arguments = {"run_ids": ["allowed"], "explanation": "Recorded answer and label disagree."}
    assert asyncio.run(session.call("report_evaluator_issue", arguments))["isError"]
    investigation.document_loaded("phoenix-evals", "references/validation.md")
    assert not asyncio.run(session.call("report_evaluator_issue", arguments))["isError"]
    assert session.finding["human_calibration"] is False
    assert session.candidate is None


def test_sdk_native_skills_are_audited_only_after_success_and_reads_are_confined(isolated, tmp_path):
    investigation = skills.Investigation([isolated])
    session = runtime.Session(investigation, tmp_path, "Original")
    def before(name, arguments, identifier="tool1"):
        return asyncio.run(session.before_tool({"tool_name": name, "tool_input": arguments}, identifier, {}))["hookSpecificOutput"]["permissionDecision"]
    assert before("Bash", {"command": "cat .env"}) == "deny"
    assert before("Read", {"file_path": str(tmp_path.parent / ".env")}) == "deny"
    assert before("Skill", {"skill": "arize-admin"}) == "deny"
    assert before("Skill", {"skill": "phoenix-evals"}) == "allow"
    assert not investigation.loaded
    asyncio.run(session.after_tool({"hook_event_name": "PostToolUse", "tool_name": "Skill"}, "tool1", {}))
    assert ("phoenix-evals", "SKILL.md") in investigation.loaded
    path = tmp_path / ".claude/skills/phoenix-evals/references/error-analysis.md"
    assert before("Read", {"file_path": str(path), "limit": 1}) == "deny"
    assert before("Read", {"file_path": str(path)}) == "allow"
    asyncio.run(session.after_tool({"hook_event_name": "PostToolUseFailure", "tool_name": "Read"}, "tool1", {}))
    assert ("phoenix-evals", "references/error-analysis.md") not in investigation.loaded
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        before("Read", {"file_path": str(path)})


def test_sdk_environment_does_not_inherit_publish_credentials(isolated, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-github")
    monkeypatch.setenv("ARIZE_API_KEY", "secret-arize")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured-provider")
    session = runtime.Session(skills.Investigation([isolated]), tmp_path, "Original")
    options = session.options("Test")
    assert options.env["GITHUB_TOKEN"] == options.env["ARIZE_API_KEY"] == ""
    assert options.env["ANTHROPIC_API_KEY"] == "configured-provider"
    assert options.tools == ["Skill", "Read"] and options.permission_mode == "dontAsk"
    assert options.setting_sources == ["project"] and options.strict_mcp_config
    assert options.max_turns == skills.MAX_ROUNDS and options.max_budget_usd == 3
    assert set(options.skills) == set(skills.PURPOSES)

def test_unavailable_phoenix_never_unlocks_patch_generation(isolated, monkeypatch):
    session = skills.Investigation([isolated])
    load_required(session)
    def unavailable():
        raise RuntimeError("private credential must not be exposed")
    monkeypatch.setattr(skills, "client", unavailable)
    result = session.call("inspect_phoenix_trace", {"run_id": "allowed"})
    assert "private credential" not in json.dumps(result)
    assert session.ready() and not session.inspected



def test_tool_budget_stops_repeated_attempts(isolated, tmp_path, monkeypatch):
    session = runtime.Session(skills.Investigation([isolated]), tmp_path, "Original")
    monkeypatch.setattr(runtime, "MAX_TOOL_CALLS", 1)
    event = {"tool_name": "mcp__phoenix__inspect_phoenix_trace", "tool_input": {"run_id": "allowed"}}
    assert asyncio.run(session.before_tool(event, "1", {}))["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert asyncio.run(session.before_tool(event, "2", {}))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_experiment_tool_excludes_other_cases_and_preserves_remote_scores(isolated, monkeypatch):
    session = skills.Investigation([isolated])
    monkeypatch.setattr(skills.store, "get_run", lambda _: {"id": "allowed", "benchmark_id": "baseline", "scenario_id": "development-1", "event": {"trace_id": "trace"}})
    monkeypatch.setattr(skills.store, "rows", lambda *a: [{"phoenix_experiment": "px-experiment"}])
    monkeypatch.setattr(skills.store, "setting", lambda key: "approved-remote-run" if key.endswith(":1") else None)
    evaluation = SimpleNamespace(experiment_run_id="approved-remote-run", name="correctness", annotator_kind="LLM", result={"score": 0}, error=None, trace_id="judge")
    remote = {"task_runs": [{"id": "approved-remote-run", "output": {"reply": "actual answer", "run_id": "allowed"}, "trace_id": "trace"}, {"id": "held-out", "output": {"reply": "secret holdout", "run_id": "private"}, "trace_id": "trace2"}], "evaluation_runs": [evaluation]}
    monkeypatch.setattr(skills, "client", lambda: SimpleNamespace(experiments=SimpleNamespace(get_experiment=lambda **kw: remote)))
    assert "error" in session.call("inspect_phoenix_experiments", {})
    session.document_loaded("arize-experiment", "SKILL.md")
    result = session.call("inspect_phoenix_experiments", {})
    assert result["records_returned"] == 1
    assert result["records"][0]["evaluations"][0]["result"]["score"] == 0
    assert "secret holdout" not in json.dumps(result)


def test_review_disagreements_survive_passing_judges_without_exposing_holdouts(isolated, monkeypatch):
    manifest = {"case_metadata": [{"id": "dev", "split": "development"}, {"id": "holdout", "split": "held_out"}]}
    monkeypatch.setattr(remediation.store, "rows", lambda *a: [{"manifest": json.dumps(manifest)}])
    def recorded(identifier):
        return {"id": identifier, "benchmark_id": "candidate", "scenario_id": identifier,
                "event": {"input": "Question", "output": "Claim", "tools": []},
                "evaluation": {"metrics": {"groundedness": {"label": "pass"}}, "tool_diagnostics": []}}
    monkeypatch.setattr(remediation.store, "get_run", recorded)
    monkeypatch.setattr(remediation, "validate_tools", lambda tools: ([], []))
    prior = {"targeted_benchmark_id": "candidate", "review_findings": [
        {"run_id": "dev", "reviewer": "assistant", "finding": "Unsupported claim"},
        {"run_id": "holdout", "reviewer": "assistant", "finding": "Private holdout"}]}
    evidence = remediation.reviewed_evidence(prior)
    assert len(evidence) == 1 and evidence[0]["run_id"] == "dev"
    assert evidence[0]["metrics"]["groundedness"]["label"] == "pass"
    assert evidence[0]["review_finding"]["reviewer"] == "assistant"


def test_revisions_require_confirmed_experiment_evidence(isolated):
    session = skills.Investigation([{**isolated, "benchmark_id": "candidate"}], {"previous_candidate": {}})
    load_required(session)
    session.call("inspect_phoenix_trace", {"run_id": "allowed"})
    assert "missing_experiment_evidence" in session.ready()
    session.experiments_inspected = True
    assert session.ready() is None
