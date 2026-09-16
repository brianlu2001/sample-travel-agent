"""Manual full checkpoints on the durable worker queue, separate from PR approval."""
import json
import uuid

from quality import store
from quality.benchmarks import configuration
from quality.config import fingerprint
from quality.versions import baseline_for


class EvaluationUnavailable(ValueError):
    pass


def request(request_id, expected_version, target_id="running", scheduled=False):
    identifier = uuid.UUID(str(request_id)).hex
    config = configuration()
    from quality.checkpoints import targets
    target = next((t for t in targets() if t["id"] == target_id), None)
    candidate = target.get("candidate") if target else None
    if target_id != "running" and not candidate:
        raise EvaluationUnavailable("The candidate target is no longer available")
    key = "full-evaluation:" + identifier
    with store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT payload FROM jobs WHERE key=?", (key,)).fetchone()
        if existing:
            payload = json.loads(existing["payload"])
            if payload.get("target_version", payload["configuration"]["agent_version"]) != expected_version:
                raise EvaluationUnavailable("This request ID belongs to another agent version")
            return {"id": identifier, "queued": True, "reused": True}
        serving = store.setting("serving_agent", {})
        if (not candidate and (config["agent_version"] != expected_version or serving.get("version") != expected_version)) or (candidate and (target["version"] != expected_version or target["configuration"] != config)):
            raise EvaluationUnavailable("Agent version changed. Restart the app and worker, then refresh the dashboard.")
        if store.setting("provider_block"):
            raise EvaluationUnavailable("Resolve the recorded provider block before starting a full evaluation.")
        if con.execute("SELECT 1 FROM jobs WHERE kind IN ('repair','full_evaluation') AND state IN ('pending','running')").fetchone():
            raise EvaluationUnavailable("An evaluation or repair is already queued or running.")
        if con.execute("SELECT 1 FROM benchmarks WHERE status='running'").fetchone():
            raise EvaluationUnavailable("A benchmark is already running.")
        anchor = baseline_for(config)
        if candidate and not anchor:
            raise EvaluationUnavailable("A candidate requires a compatible frozen baseline")
        payload = {"id": identifier, "configuration": config, "baseline_id": anchor,
                   "kind": "checkpoint" if anchor else "baseline", "candidate": candidate,
                   "target_id": target_id, "target_version": expected_version, "target": target,
                   "scheduled": scheduled}
        store.enqueue("full_evaluation", key, payload, con)
    return {"id": identifier, "queued": True, "reused": False}


def execute(payload):
    from quality import benchmarks
    from quality.runtime import prepare_agent
    identifier = payload["id"]
    existing = store.rows("SELECT status FROM benchmarks WHERE id=?", (identifier,))
    if existing and existing[0]["status"] == "complete":
        report = benchmarks.run(identifier)
        from quality.checkpoints import record_result
        record_result(identifier, payload.get("target"))
        return report
    expected = payload["configuration"]
    try:
        if fingerprint(configuration()) != fingerprint(expected) or prepare_agent()["version"] != expected["agent_version"]:
            raise EvaluationUnavailable("Queued evaluation configuration changed; refresh and request the current version.")
        from quality.checkpoints import targets, record_result, verify_target
        target = next((t for t in targets() if t["id"] == payload.get("target_id")), None)
        if payload.get("scheduled") and target and target["version"] != payload["target_version"]:
            store.set_setting("checkpoint-request:" + identifier, "superseded")
            return {"superseded": True}
        kwargs = {"candidate": payload["candidate"]} if payload.get("candidate") else {}
        if kwargs:
            from pathlib import Path
            if fingerprint(json.loads(Path(payload["candidate"]).read_text(encoding="utf-8"))) != payload["target_version"]:
                raise EvaluationUnavailable("Candidate changed after the checkpoint was requested")
            verify_target(payload.get("target"))
        benchmarks.create(kind=payload["kind"], parent_id=payload["baseline_id"], benchmark_id=identifier, **kwargs)
        report = benchmarks.run(identifier)
        if isinstance(report, dict) and "case_run_ids" in report:
            verify_target(payload.get("target"))
            record_result(identifier, payload.get("target"))
        return report
    except Exception:
        # Preserve partial evidence and make failure visible. An explicit retry
        # resumes completed cases; no automatic loop of expensive full runs.
        store.execute("UPDATE benchmarks SET status='evaluation_failed' WHERE id=? AND status='running'", (identifier,))
        raise


def status():
    jobs = store.rows("SELECT key,state,error,payload FROM jobs WHERE kind='full_evaluation' ORDER BY id DESC LIMIT 1")
    active = store.rows("SELECT kind FROM jobs WHERE kind IN ('repair','full_evaluation') AND state IN ('pending','running') LIMIT 1")
    running = store.rows("SELECT id FROM benchmarks WHERE status='running' LIMIT 1")
    latest = None
    if jobs:
        job = jobs[0]
        payload = json.loads(job["payload"])
        latest = {"id": payload["id"], "state": store.setting("checkpoint-request:" + payload["id"], job["state"]), "error": job["error"],
                  "version": payload.get("target_version", payload["configuration"]["agent_version"])}
    return {"busy": bool(active or running), "latest": latest, "repetitions": 2}
