"""Real judge executions against explicit labels on preserved real agent answers.

This audits known failures; references are developer-authored, not human labels.
It creates a native Phoenix experiment and never rewrites sealed benchmarks.
"""
import concurrent.futures
import json
import time
from datetime import datetime, timezone

from phoenix.client import Client

from quality import store
from quality.config import PHOENIX, ROOT, STATE, fingerprint
from quality.evaluation import assess_event, evaluator_version

# Reference labels are the only authored data. Every run ID resolves to a real,
# previously captured response; no fabricated response enters this experiment.
REFERENCES = {
    "3e93d047730f41ff8208b486f2713408": ("pass", "not_applicable", "pass", "pass"),
    "e5f0270461b545d687e3d86098191f86": ("pass", "not_applicable", "pass", "pass"),
    "d9b6538aeaad4cb389c6e10006b753ab": ("fail", "fail", "pass", "fail"),
    "23658356177748eda1a47aac95318251": (None, None, "pass", "fail"),
    "43782150851d40a884a0b6575a1c0220": ("fail", "fail", "pass", "fail"),
    "0cc32316ffcb4341afa5b6895b19f4e6": ("pass", "pass", "pass", "pass"),
    "ff8f720ff4d34b77b1a8384b82a64224": ("pass", "not_applicable", "pass", "pass"),
    "4e0ebec4f23d43a69c43be56fc3bfb73": ("fail", "fail", "pass", "fail"),
    "f1f35b839e6b40d0acd04146993cac97": ("pass", "not_applicable", "pass", "not_applicable"),
    "383e66e170974c6b95a6720517ea62d1": ("pass", "not_applicable", "pass", "pass"),
    "ad8302b4a30444fca7647a5ac1237b27": ("pass", "not_applicable", "pass", "pass"),
    "6b53a650c7fa480bb5606145d6500e61": ("pass", "not_applicable", "pass", "pass"),
    "36af12aa59964be3a443c6b9d4413fde": ("fail", "fail", "pass", "fail"),
    "01670e2258db4fd295d87089f0c7e566": ("fail", "fail", "pass", "fail"),
    "d2c91759e3fd4b948cbe8882a2e012c7": ("pass", "not_applicable", "pass", "pass"),
    "858d94a446cb48b0bc7e46b1f55dd50c": (None, None, "fail", None),
    "9d771ec91f744b5c8bee65e01c3213d7": (None, None, "fail", None),
}
NAMES = ("correctness", "groundedness", "topic_relevance", "task_completion")


def source_rows():
    """Use preserved evidence on a fresh checkout without inventing agent runs."""
    archive = json.loads((ROOT / "datasets" / "evaluator-recorded-responses.json").read_text(encoding="utf-8"))["events"]
    rows = {}
    for key in REFERENCES:
        row = store.get_run(key)
        event = row["event"] if row else archive.get(key)
        if not event or event.get("id") != key or not event.get("privacy_ok"):
            raise ValueError("Audit requires its actual redacted source responses")
        rows[key] = {"event": event}
    return rows


def run():
    store.init()
    version = evaluator_version()
    rows = source_rows()
    client = Client(base_url=PHOENIX)
    dataset = client.datasets.create_dataset(name="travel-evaluator-audit-" + fingerprint(REFERENCES)[:12],
        examples=[{"input": {"run_id": key, "conversation": row["event"]["input"], "final_response": row["event"]["output"]},
                   "output": {name: label for name, label in zip(NAMES, REFERENCES[key]) if label},
                   "metadata": {"run_id": key, "source_trace_id": row["event"]["trace_id"],
                                "provenance": "actual_recorded_response", "reference_author": "developer; human review pending"}}
                  for key, row in rows.items()],
        dataset_description="Evaluator regression audit. All answers are actual recorded executions. Expected labels are developer-authored; this is not human calibration.")
    experiment = client.experiments.create(dataset_id=dataset.id, dataset_version_id=dataset.version_id,
        experiment_name="judge-audit-" + version[:12], repetitions=1,
        experiment_metadata={"evaluator_version": version, "purpose": "known evaluator failure regression audit"})
    example_ids = {e["metadata"]["run_id"]: e["node_id"] for e in dataset}
    results = []
    def one(item):
        key, row = item
        result = assess_event(row["event"], key, assessment_source="evaluator_audit")
        comparisons = [{"metric": name, "expected": label, "actual": result["metrics"][name]["label"],
                        "passed": result["metrics"][name]["label"] == label}
                       for name, label in zip(NAMES, REFERENCES[key]) if label]
        logged = client.experiments.log_run(experiment_id=experiment["id"], dataset_example_id=example_ids[key],
            repetition_number=1, trace_id=result["trace_id"], output=result["metrics"],
            start_time=datetime.fromtimestamp(result["started"], timezone.utc),
            end_time=datetime.fromtimestamp(result["finished"], timezone.utc))
        client.experiments.log_evaluation(experiment_run_id=logged["id"], name="reference_agreement", annotator_kind="CODE",
            score=sum(c["passed"] for c in comparisons)/len(comparisons),
            label="pass" if all(c["passed"] for c in comparisons) else "fail", explanation=json.dumps(comparisons),
            start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc))
        print(json.dumps({"run": key, "checks": comparisons}), flush=True)
        return {"run_id": key, "source_trace_id": row["event"]["trace_id"], "assessment": result, "checks": comparisons}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, rows.items()))
    checks = [check for result in results for check in result["checks"]]
    report = {"status": "passed" if all(c["passed"] for c in checks) else "failed",
              "evaluator_version": version, "at": time.time(), "responses": len(rows),
              "checks": len(checks), "passed_checks": sum(c["passed"] for c in checks),
              "human_calibration": "pending; regression references are developer-authored",
              "experiment_url": client.experiments.get_experiment_url(dataset_id=dataset.id, experiment_id=experiment["id"]),
              "results": results}
    target = STATE / "audits" / (version + ".json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    store.set_setting("evaluator_audit", {k:v for k,v in report.items() if k != "results"})
    if report['status'] == 'passed':
        from quality.config import agent_version
        store.enqueue('monitor', 'audit-passed:'+version+':'+str(report['at']),
                      {'source':'live','version':agent_version(),'evaluation_version':version})
    print(json.dumps({k:v for k,v in report.items() if k != "results"}), flush=True)


if __name__ == "__main__":
    run()
