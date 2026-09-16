"""Real benchmark executions, immutable manifests and Phoenix experiments."""
import argparse
import concurrent.futures
import importlib.metadata
import json
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

from phoenix.client import Client

from quality import store
from quality.config import PHOENIX, ROOT, STATE, agent_version, file_hash, fingerprint, fixture_version
from quality.evaluation import evaluator_version, scenarios
from quality.monitoring import summarize
from quality.privacy import sanitize


def configuration(repetitions=2):
    """Comparable, non-secret identity of everything used to measure a version."""
    examples = scenarios()
    return {
        "agent_version": agent_version(), "fixture_version": fixture_version(),
        "dataset_version": fingerprint(examples), "evaluator_version": evaluator_version(),
        "privacy_version": file_hash([ROOT / "quality" / "privacy.py"]),
        "validation_policy_version": file_hash([ROOT / "quality" / "validation.py"]),
        "repetitions": repetitions, "scenario_count": len(examples),
        "models": {"agent": __import__('agent.config', fromlist=['MODEL']).MODEL,
                   "judge": __import__('quality.config', fromlist=['JUDGE_MODEL']).JUDGE_MODEL},
        "packages": {n: importlib.metadata.version(n) for n in ("anthropic", "arize-phoenix-evals", "arize-phoenix-client", "openinference-instrumentation-anthropic")},
    }


def create(kind="baseline", parent_id=None, candidate=None, repetitions=2, benchmark_id=None, examples=None):
    store.init()
    if benchmark_id:
        existing = store.rows("SELECT * FROM benchmarks WHERE id=?", (benchmark_id,))
        if existing:
            old = existing[0]
            old_manifest = json.loads(old["manifest"])
            if old["kind"] != kind or old["parent_id"] != parent_id or old_manifest.get("candidate_source", old_manifest["candidate"]) != candidate:
                raise ValueError("Benchmark identity conflicts with existing evidence")
            return benchmark_id
    suite = "full" if examples is None else "targeted"
    examples = scenarios() if examples is None else examples
    if not examples or (suite == "targeted" and any(e["split"] != "development" for e in examples)):
        raise ValueError("Targeted evaluations require development cases only")
    baseline = None
    if candidate:
        matches = store.rows("SELECT * FROM benchmarks WHERE id=? AND status='complete'", (parent_id,))
        if not matches:
            raise ValueError("A completed baseline is required before candidate execution")
        baseline = matches[0]
    manifest = {**configuration(repetitions),
        "candidate": candidate, "provenance": "real_model_and_tool_executions_on_synthetic_reference_inputs",
        "human_calibration": "pending_human_review",
        "suite": suite, "scenario_ids": [e["id"] for e in examples],
        "case_metadata": [{k: e[k] for k in ("id", "category", "split")} for e in examples],
        "extra_examples": sanitize([e for e in examples if e["id"] not in {s["id"] for s in scenarios()}]),
        "selection_hash": fingerprint(examples),
        "scenario_count": len(examples),
    }
    from quality.checkpoint_policy import version as policy_version
    manifest["acceptance_policy_version"] = policy_version()
    if baseline:
        old = json.loads(baseline["manifest"])
        for field in ("fixture_version", "dataset_version", "evaluator_version", "privacy_version", "models", "packages"):
            if manifest[field] != old[field]:
                raise ValueError(f"Comparison invalid: changed {field}")
    benchmark_id = benchmark_id or uuid.uuid4().hex
    directory = STATE / "benchmarks" / benchmark_id
    directory.mkdir(parents=True, exist_ok=True)
    if candidate:
        snapshot = json.loads(Path(candidate).read_text(encoding="utf-8"))
        manifest["candidate_source"] = candidate
        manifest["candidate_version"] = fingerprint(snapshot)
        manifest["candidate"] = str(directory / "candidate.json")
        (directory / "candidate.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    client = Client(base_url=PHOENIX)
    dataset = client.datasets.create_dataset(
        name=("travel-v1-" if suite == "full" else "travel-targeted-") + manifest["selection_hash"][:12] + "-" + manifest["privacy_version"][:8],
        examples=[{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, e["id"])),
                   "input": sanitize({"messages": e["messages"]}), "output": sanitize(e["reference"]),
                   "metadata": {"scenario_id": e["id"], "category": e["category"], "split": e["split"], "synthetic": "reference_only"}} for e in examples],
        dataset_description=f"{len(examples)} {suite} reference scenarios. Inputs are reference cases or redacted incident replays; all outputs and traces are actual executions. Reference labels await human review.", timeout=60)
    experiment = client.experiments.create(dataset_id=dataset.id, dataset_version_id=dataset.version_id,
                                           experiment_name=f"{kind}-{benchmark_id[:8]}", repetitions=repetitions,
                                           experiment_metadata={k:v for k,v in manifest.items() if k not in ("candidate", "candidate_source", "extra_examples")})
    # Experiment API uses the relay node ID; custom dataset IDs are not interchangeable.
    manifest["example_ids"] = {e["metadata"]["scenario_id"]: e["node_id"] for e in dataset}
    store.execute("INSERT INTO benchmarks(id,kind,parent_id,status,created,manifest,phoenix_dataset,phoenix_experiment) VALUES(?,?,?,?,?,?,?,?)",
                  (benchmark_id, kind, parent_id, "running", time.time(), json.dumps(manifest), dataset.id, experiment["id"]))
    directory = STATE / "benchmarks" / benchmark_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not candidate:
        for name in ("tools.py", "prompt.py", "loop.py", "config.py"):
            (directory / name).write_text((ROOT / "agent" / name).read_text(encoding="utf-8"), encoding="utf-8")
    return benchmark_id


def execute_scenario(benchmark_id, scenario, repetition, candidate=None):
    from quality.runtime import run_observed
    messages = []
    session = uuid.uuid4().hex
    event = None
    for message in scenario["messages"]:
        messages.append({"role": "user", "content": message})
        if candidate:
            from quality.sandbox import run_candidate_turn
            _, messages, event = run_candidate_turn(messages, candidate, benchmark_id, scenario["id"], session)
        else:
            _, messages, event = run_observed(messages, source="benchmark", conversation_id=session,
                                             benchmark_id=benchmark_id, scenario_id=scenario["id"])
    # Scenario output is the final turn; preceding turns remain real traces but
    # are not counted as additional cases in the benchmark's denominator.
    store.set_setting(f"case:{benchmark_id}:{scenario['id']}:{repetition}", event["id"])
    return event["id"]


def run(benchmark_id, concurrency=4):
    benchmark = store.rows("SELECT * FROM benchmarks WHERE id=?", (benchmark_id,))[0]
    if benchmark["status"] == "complete":
        from quality.versions import archive_benchmarks
        archive_benchmarks()
        return json.loads(benchmark["report"])
    if benchmark["status"] not in ("running", "evaluation_failed"):
        raise ValueError("This benchmark is not resumable")
    manifest = json.loads(benchmark["manifest"])
    by_id = {e["id"]: e for e in scenarios() + manifest.get("extra_examples", [])}
    examples = [by_id[i] for i in manifest["scenario_ids"]] if "scenario_ids" in manifest else manifest.get("examples", scenarios())
    def check_inputs():
        current = configuration(manifest["repetitions"])
        for key in current:
            if key == "scenario_count":
                continue
            if current[key] != manifest[key]:
                raise RuntimeError("Benchmark inputs changed: " + key)
        if manifest.get("candidate_version") and fingerprint(json.loads(Path(manifest["candidate"]).read_text(encoding="utf-8"))) != manifest["candidate_version"]:
            raise RuntimeError("Frozen candidate changed during execution")
        if not manifest["candidate"]:
            from quality.runtime import prepare_agent
            if prepare_agent()["version"] != manifest["agent_version"]:
                raise RuntimeError("Restart the worker to load the requested agent version")
    check_inputs()
    jobs = [(scenario, repetition) for repetition in range(1, manifest["repetitions"]+1) for scenario in examples]
    store.execute("UPDATE benchmarks SET status='running' WHERE id=? AND status='evaluation_failed'", (benchmark_id,))

    def one(args):
        check_inputs()
        scenario, repetition = args
        existing = store.setting(f"case:{benchmark_id}:{scenario['id']}:{repetition}")
        return existing or execute_scenario(benchmark_id, scenario, repetition, manifest["candidate"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one, item) for item in jobs]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            future.result()
            print(f"{benchmark['kind']} executed {i}/{len(jobs)} scenarios", flush=True)
    deadline = time.time() + 3600
    while time.time() < deadline:
        case_ids = [store.setting(f"case:{benchmark_id}:{s['id']}:{rep}") for s, rep in jobs]
        case_rows = [store.get_run(run_id) for run_id in case_ids]
        done = sum(bool(r["evaluation"]) and not any(v.get("error_type") for v in r["evaluation"]["metrics"].values()) for r in case_rows)
        print(f"{benchmark['kind']} evaluated {done}/{len(jobs)} scenarios", flush=True)
        if done == len(jobs):
            break
        dead = store.rows("SELECT id FROM jobs WHERE kind='evaluate' AND state='dead' AND json_extract(payload,'$.run_id') IN (SELECT id FROM runs WHERE benchmark_id=?)", (benchmark_id,))
        if dead:
            store.execute("UPDATE benchmarks SET status='evaluation_failed' WHERE id=?", (benchmark_id,))
            raise RuntimeError("Benchmark has dead evaluation jobs; inspect and retry before sealing")
        time.sleep(15)
    else:
        raise TimeoutError("Benchmark evaluations did not complete")
    # Guard against accidentally changing evaluation/data/source while running.
    check_inputs()
    if evaluator_version() != manifest["evaluator_version"] or fixture_version() != manifest["fixture_version"] or fingerprint(scenarios()) != manifest["dataset_version"]:
        raise RuntimeError("Benchmark inputs changed during execution")
    if not manifest["candidate"] and agent_version() != manifest["agent_version"]:
        raise RuntimeError("Baseline agent changed during execution")
    if file_hash([ROOT / "quality" / "privacy.py"]) != manifest["privacy_version"]:
        raise RuntimeError("Privacy policy changed during execution")
    client = Client(base_url=PHOENIX)
    exported = client.experiments.get_experiment(experiment_id=benchmark["phoenix_experiment"])
    remote_runs = {(r["dataset_example_id"], r["repetition_number"]): r for r in exported["task_runs"]}
    remote_evaluations = {(e.experiment_run_id, e.name) for e in exported["evaluation_runs"] if not e.error}
    for (scenario, repetition), row in zip(jobs, case_rows):
        logged_key = f"phoenix-run:{benchmark_id}:{scenario['id']}:{repetition}"
        if store.setting(logged_key):
            continue
        event = row["event"]
        logged = remote_runs.get((manifest["example_ids"][scenario["id"]], repetition))
        if logged and (logged["trace_id"] != event["trace_id"] or logged["output"].get("run_id") != event["id"]):
            raise ValueError("Phoenix run conflicts with local execution evidence")
        logged = logged or client.experiments.log_run(experiment_id=benchmark["phoenix_experiment"],
            dataset_example_id=manifest["example_ids"][scenario["id"]], output={"reply": event["output"], "run_id": event["id"]},
            start_time=datetime.fromtimestamp(event["created"], timezone.utc), end_time=datetime.fromtimestamp(event["finished"], timezone.utc),
            repetition_number=repetition, trace_id=event["trace_id"])
        for name, metric in row["evaluation"]["metrics"].items():
            if (logged["id"], name) in remote_evaluations:
                continue
            client.experiments.log_evaluation(experiment_run_id=logged["id"], name=name, annotator_kind=metric["annotator"],
                score=metric["score"], label=metric["label"], explanation=metric["explanation"], trace_id=row["evaluation"]["trace_id"],
                start_time=datetime.fromtimestamp(row["evaluation"]["started"], timezone.utc), end_time=datetime.fromtimestamp(row["evaluation"]["finished"], timezone.utc))
        store.set_setting(logged_key, logged["id"])
    report = summarize(case_rows)
    report["case_run_ids"] = case_ids
    report["by_category"] = {category: summarize([r for (s, _), r in zip(jobs, case_rows) if s["category"] == category])
                              for category in sorted({s["category"] for s in examples})}
    report["held_out"] = summarize([r for (s, _), r in zip(jobs, case_rows) if s["split"] == "held_out"])
    report["tool_contract_failures"] = sum(d["label"] == "fail" for r in case_rows for d in r["evaluation"]["tool_diagnostics"])
    all_events = [json.loads(r["event"]) for r in store.rows("SELECT event FROM runs WHERE benchmark_id=?", (benchmark_id,))]
    report["privacy_failures"] = sum(not e.get("privacy_ok", True) for e in all_events)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["evidence_hash"] = fingerprint([{k: r[k] for k in ("id", "event", "evaluation")} for r in case_rows])
    from quality.versions import archive_benchmark
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE benchmarks SET status='complete',completed=?,report=? WHERE id=?", (time.time(), json.dumps(report), benchmark_id))
        archive_benchmark(con, benchmark_id)
        from quality.repair_dispatch import baseline_ready
        baseline_ready(con, benchmark_id)
    (STATE / "benchmarks" / benchmark_id / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if benchmark["kind"] == "baseline":
        store.set_setting("baseline_id", benchmark_id)
    print(json.dumps({"benchmark_id": benchmark_id, "scores": {k:v["pass_rate"] for k,v in report["metrics"].items()}, "evidence_hash": report["evidence_hash"]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume")
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args()
    benchmark_id = args.resume or create(repetitions=args.repetitions)
    print("Benchmark ID: " + benchmark_id, flush=True)
    run(benchmark_id)


if __name__ == "__main__":
    main()
