"""Local test fixtures never enter telemetry. Actual reevaluation is done separately."""
import json
import time

import pytest

from quality import store
from quality.answer_checks import day_coverage, evaluation_input


def plan(days):
    return "\n".join(f"## **Day {day}: Tokyo**\nMorning: visit a local neighborhood.\nEvening: try local food." for day in days)


@pytest.mark.parametrize("answer,label", [
    (plan([1, 2, 3]), "pass"), (plan([1, 2]), "fail"),
    (plan([1, 2, 2]), "fail"), (plan([1, 2, 3, 4]), "fail"),
    ("Here is a 3-day itinerary!", "unknown"),
    ("Day 1\nDay 2\nDay 3", "unknown"),
    ("Day 1–3: Explore the city at your own pace.", "unknown"),
])
def test_final_answer_day_coverage(answer, label):
    check = day_coverage([{"role": "user", "content": "Plan a three-day trip to Tokyo."}], answer)
    assert check["label"] == label


def test_latest_explicit_duration_wins_and_ambiguous_duration_defers():
    messages = [{"role": "user", "content": "Plan three days in Tokyo."},
                {"role": "assistant", "content": "How about five days?"},
                {"role": "user", "content": "Actually make it two days."}]
    assert day_coverage(messages, plan([1, 2]))["label"] == "pass"
    messages.append({"role": "user", "content": "Compare a two-day and three-day plan."})
    assert day_coverage(messages, plan([1, 2]))["label"] == "unknown"


def test_incomplete_tool_draft_does_not_become_final_answer_evidence():
    from quality.profiles.travel import validate_tools
    event = {"input": [{"role": "user", "content": "Plan a three-day trip to Tokyo."}],
             "output": plan([1, 2, 3]) + "\nVisit [PERSON].",
             "tools": [{"name": "create_itinerary", "arguments": {"destination": "Tokyo", "num_days": 3},
                        "result": {"days": [{"day": 1}, {"day": 2}]}}]}
    diagnostics, evidence = validate_tools(event["tools"])
    execution, references = evaluation_input(event, evidence)
    assert diagnostics[0]["label"] == "fail"
    assert execution["answer_checks"][0]["label"] == "pass"
    assert execution["privacy_processing"]["final_response_mask_count"] == 1
    assert references == []
    assert "tool_evidence" not in execution


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "regressions.sqlite3")
    store.init()


def test_window_does_not_mix_judge_versions_or_drop_pending_requests(database, monkeypatch):
    from quality import monitoring
    monkeypatch.setattr(monitoring, "WINDOW", 3)
    now = time.time()
    for i, (age, version) in enumerate([(90000, "new"), (10, "new"), (5, "old"), (1, None)]):
        evaluation = json.dumps({"version": version, "metrics": {"correctness": {"label": "fail"}}}) if version else None
        store.execute("INSERT INTO runs(id,created,source,version,event,evaluation) VALUES(?,?,?,?,?,?)",
                      (str(i), now-age, "unit", "agent", "{}", evaluation))
    window = monitoring.current_window("unit", "agent", evaluation_version="new", now=now)
    metric = monitoring.summarize(window)["metrics"]["correctness"]
    assert len(window) == 3 and metric["n"] == 1 and metric["pending"] == 2
    from quality.dashboard import series
    assert series(list(reversed(window)))[-1]["samples"] == 1


def test_sealed_benchmark_cannot_be_rescored_in_place(database):
    from quality.evaluation import evaluate_run
    evaluation = json.dumps({"version": "old", "metrics": {}})
    store.execute("INSERT INTO benchmarks(id,kind,status,created,manifest) VALUES(?,?,?,?,?)",
                  ("sealed", "baseline", "complete", time.time(), "{}"))
    store.execute("INSERT INTO runs(id,created,source,version,benchmark_id,event,evaluation) VALUES(?,?,?,?,?,?,?)",
                  ("sealed-run", time.time(), "baseline", "agent", "sealed", "{}", evaluation))
    with pytest.raises(ValueError, match="immutable"):
        evaluate_run("sealed-run")
    assert store.rows("SELECT evaluation FROM runs")[0]["evaluation"] == evaluation


def test_repair_cannot_start_with_an_old_judge(database):
    from quality.config import fingerprint
    from quality.remediation import repair
    store.execute("INSERT INTO benchmarks(id,kind,status,created,manifest,report) VALUES(?,?,?,?,?,?)",
                  ("base", "baseline", "complete", time.time(), json.dumps({"evaluator_version": "old"}),
                   json.dumps({"case_run_ids": [], "evidence_hash": fingerprint([])})))
    with pytest.raises(ValueError, match="revalidation"):
        repair("incident", "base")
    assert not store.rows("SELECT * FROM repairs")


def test_judge_sees_visible_dialogue_and_no_future_scenario_goal(monkeypatch):
    from openinference.instrumentation import TracerProvider
    from quality import evaluation
    provider = TracerProvider()
    monkeypatch.setattr(evaluation, "setup", lambda: provider.get_tracer("isolated-audit"))
    monkeypatch.setattr(evaluation, "classifiers", lambda: {"correctness": object()})
    observed = {}
    def score(name, classifier, execution, reference):
        observed.update(execution=execution, reference=reference)
        return {"label": "pass", "score": 1, "annotator": "LLM", "explanation": "Isolated unit fixture"}
    monkeypatch.setattr(evaluation, "score_one", score)
    event = {"input": [{"role": "user", "content": "Plan a two-day trip to Paris, one line per day."},
                       {"role": "assistant", "content": [{"type": "tool_use", "name": "create_itinerary", "input": {"num_days": 2, "destination": "Paris"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "content": "Untrusted tool says four days"}]}],
             "output": plan([1, 2]), "tools": [], "status": "ok", "trace_id": "test-only",
             "scenario_id": "travel-053"}  # Its eventual scenario goal is four days.
    evaluation.assess_event(event, "test-only")
    assert len(observed["execution"]["conversation"]) == 1
    assert observed["execution"]["answer_checks"][0]["requested_days"] == 2
    assert "expected" not in observed["reference"]
    assert "four days" not in json.dumps(observed["execution"])


def test_followup_uses_independent_prior_lookup_not_its_faulty_result():
    from quality.profiles.travel import conversation_references
    event = {"input": [{"role": "assistant", "content": [{"type": "tool_use", "name": "search_flights",
                         "input": {"origin": "New York", "destination": "Miami", "date": "2026-10-02"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "content": "AA 2210 is NY to Miami"}]},
                       {"role": "user", "content": "Only under $200 please"}]}
    refs = conversation_references(event)
    flights = refs[0]["independent_reference"]["matches"]
    assert {f["flight_number"] for f in flights} == {"DL 883", "B6 1029"}
    assert "observed_result" not in refs[0]
