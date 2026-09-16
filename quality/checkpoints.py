"""Daily, version-aware checkpoints in the existing local worker. No auto-deployment."""
import json
import time
import uuid

from quality import store
from quality.benchmarks import configuration
from quality.checkpoint_policy import POLICY, compare, version as policy_version
from quality.config import PRIMARY_METRICS, fingerprint
from quality.evaluation import scenarios
from quality.versions import baseline_for, measurement_context, saved_benchmarks


def targets():
    return [{"id": r["id"], "updated": r["updated"], **json.loads(r["payload"])}
            for r in store.rows("SELECT * FROM checkpoint_targets WHERE COALESCE(json_extract(payload,'$.retired'),0)=0 ORDER BY updated DESC")]


def register(identifier, payload):
    previous = next((t for t in targets() if t["id"] == identifier), None)
    if previous and all(previous.get(k) == payload.get(k) for k in payload):
        return
    store.execute("INSERT INTO checkpoint_targets VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,payload=excluded.payload",
                  (identifier, time.time(), json.dumps(payload)))


def register_candidate(repair_id):
    row = store.rows("SELECT * FROM repairs WHERE id=?", (repair_id,))[0]
    payload = json.loads(row["payload"])
    from quality.config import STATE
    path = STATE / "repairs" / repair_id / "candidate.json"
    if fingerprint(json.loads(path.read_text(encoding="utf-8"))) != payload["candidate_hash"]:
        raise ValueError("Candidate changed since its recorded checks")
    head_sha = payload.get("head_sha")
    if payload.get("pr_url") and not head_sha:
        known = next((t for t in targets() if t.get("repair_id") == repair_id and t["version"] == payload["candidate_hash"]), None)
        head_sha = known.get("head_sha") if known else None
        head_sha = head_sha or recover_pr_head(payload, path)
    register("candidate:" + payload.get("root_repair_id", repair_id), {
        "version": payload["candidate_hash"], "repair_id": repair_id,
        "candidate": str(path), "baseline_id": payload["baseline_id"],
        "configuration": configuration(), "pr_url": payload.get("pr_url"),
        "head_sha": head_sha, "label": "Latest candidate"})


def recover_pr_head(payload, path):
    """Bind a legacy proposal only after its actual files match the saved candidate."""
    import base64
    import httpx
    from quality.config import STATE
    from quality.remediation import credential, candidate_files, REGRESSION_TEST
    parts = payload["pr_url"].removeprefix("https://github.com/").split("/")
    if len(parts) != 4 or parts[2] != "pull":
        raise ValueError("Unsupported PR target")
    expected = candidate_files(json.loads(path.read_text(encoding="utf-8")), payload["baseline_id"])
    expected["tests/test_agent_regressions.py"] = REGRESSION_TEST
    for name in ("loop.py", "config.py"):
        expected["agent/"+name] = (STATE / "benchmarks" / payload["baseline_id"] / name).read_text(encoding="utf-8")
    with httpx.Client(headers={"Authorization": "Bearer " + (credential() or "")}, timeout=20) as client:
        prefix = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
        response = client.get(prefix + "/pulls/" + parts[3])
        response.raise_for_status()
        head = response.json()["head"]["sha"]
        for name, source in expected.items():
            content = client.get(prefix + "/contents/" + name, params={"ref": head})
            content.raise_for_status()
            if base64.b64decode(content.json()["content"]).decode("utf-8").replace("\r\n", "\n") != source:
                raise ValueError("PR files differ from the saved candidate")
    return head


def verify_target(target):
    """Bind candidate evidence to the PR revision, not an externally edited head."""
    if not target or not target.get("pr_url") or not target.get("head_sha"):
        return
    import httpx
    from quality.remediation import credential
    parts = target["pr_url"].removeprefix("https://github.com/").split("/")
    if len(parts) != 4 or parts[2] != "pull":
        raise ValueError("Unsupported PR target")
    with httpx.Client(headers={"Authorization": "Bearer " + (credential() or "")}, timeout=20) as client:
        response = client.get(f"https://api.github.com/repos/{parts[0]}/{parts[1]}/pulls/{parts[3]}")
        response.raise_for_status()
        if response.json().get("state", "open") != "open":
            raise ValueError("PR is closed; evaluate the running agent after deployment")
        if response.json()["head"]["sha"] != target["head_sha"]:
            raise ValueError("PR head changed outside this measured candidate")


def publish_status(target, decision, approved=False):
    if not target or not target.get("pr_url") or not target.get("head_sha"):
        return
    verify_target(target)
    import httpx
    from quality.remediation import credential
    parts = target["pr_url"].removeprefix("https://github.com/").split("/")
    state = "success" if approved else "failure" if decision["conclusion"] == "regressed" else "pending"
    description = "Human-approved full checkpoint" if approved else "Checkpoint: " + decision["conclusion"] + "; human review required"
    with httpx.Client(headers={"Authorization": "Bearer " + (credential() or "")}, timeout=20) as client:
        response = client.post(f"https://api.github.com/repos/{parts[0]}/{parts[1]}/statuses/{target['head_sha']}",
                               json={"state": state, "context": "quality/full-checkpoint", "description": description,
                                     "target_url": target["pr_url"]})
        response.raise_for_status()


def sync_running():
    config = configuration()
    serving = store.setting("serving_agent", {})
    if serving.get("version") == config["agent_version"]:
        register("running", {"version": serving["version"], "configuration": config,
                             "label": "Running agent", "candidate": None})


def full_records(config):
    return [r for r in saved_benchmarks() if r["manifest"].get("suite", "full") == "full"
            and measurement_context(r["manifest"]) == measurement_context(config)]


def schedule_state(target, now=None):
    now = time.time() if now is None else now
    if target["configuration"] != configuration():
        return {"state": "stale_configuration", "due_at": None}
    records = full_records(target["configuration"])
    measured = [r for r in records if r["version"] == target["version"]]
    if measured:
        return {"state": "up_to_date", "benchmark_id": measured[-1]["id"], "due_at": None}
    last = max((r["completed"] or 0 for r in records), default=0)
    due = last + POLICY["interval_seconds"] if last else now
    signature = fingerprint([target["id"], target["version"], measurement_context(target["configuration"])])
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, "daily-checkpoint:" + signature).hex
    jobs = store.rows("SELECT state FROM jobs WHERE key=?", ("full-evaluation:" + identifier,))
    state = "needs_attention" if jobs and jobs[0]["state"] == "dead" else jobs[0]["state"] if jobs else "due" if now >= due else "waiting"
    return {"state": state, "due_at": due, "request_id": identifier}


def tick(now=None):
    if not store.setting("checkpoint_schedule_enabled", False) or store.setting("provider_block"):
        return
    sync_running()
    from quality.full_evaluation import request, EvaluationUnavailable
    for target in targets():
        status = schedule_state(target, now)
        if status["state"] != "due":
            continue
        try:
            request(status["request_id"], target["version"], target_id=target["id"], scheduled=True)
        except EvaluationUnavailable:
            continue
        return  # At most one full experiment queued; the worker serializes it.


def _case_rows(record):
    return [store.get_run(identifier) for identifier in record["report"]["case_run_ids"]]


def last_approved(config, records):
    approved = store.rows("SELECT benchmark_id FROM checkpoint_approvals WHERE policy_version=? ORDER BY created DESC,benchmark_id", (policy_version(),))
    compatible = {r["id"] for r in records}
    return next((r["benchmark_id"] for r in approved if r["benchmark_id"] in compatible), baseline_for(config))


def record_result(benchmark_id, target=None):
    policy = policy_version()
    existing = store.rows("SELECT record FROM checkpoint_decisions WHERE benchmark_id=? AND policy_version=?", (benchmark_id, policy))
    if existing:
        decision = json.loads(existing[0]["record"])
        process_regressions(decision, target)
        approved = bool(store.rows("SELECT 1 FROM checkpoint_approvals WHERE benchmark_id=? AND policy_version=?", (benchmark_id, policy)))
        publish_status(target, decision, approved=approved)
        return decision
    records = saved_benchmarks()
    record = next(r for r in records if r["id"] == benchmark_id)
    if record["manifest"].get("suite", "full") != "full":
        raise ValueError("A targeted suite cannot establish a full checkpoint")
    compatible = full_records(record["manifest"])
    anchors = {"original": baseline_for(record["manifest"]), "last_approved": last_approved(record["manifest"], compatible)}
    after = _case_rows(record)
    metadata = record["manifest"].get("case_metadata", record["manifest"].get("examples", scenarios()))
    held_out = {e["id"] for e in metadata if e["split"] == "held_out"}
    comparisons = {}
    for name, identifier in anchors.items():
        if not identifier or identifier == benchmark_id:
            continue
        before = _case_rows(next(r for r in compatible if r["id"] == identifier))
        comparisons[name] = {"benchmark_id": identifier, "overall": compare(before, after),
                             "held_out": compare([r for r in before if r["scenario_id"] in held_out], [r for r in after if r["scenario_id"] in held_out])}
    blockers = []
    if record["report"].get("privacy_failures", 0):
        blockers.append("Privacy redaction failed during this experiment")
    if record["report"].get("tool_contract_failures", 0):
        blockers.append("Deterministic tool contract failures remain")
    if any(m.get("unknown", 0) or m.get("pending", 0) for name, m in record["report"]["metrics"].items() if name in PRIMARY_METRICS):
        blockers.append("Evaluation coverage is incomplete")
    conclusions = [value[cohort]["conclusion"] for value in comparisons.values() for cohort in ("overall", "held_out")]
    conclusion = "regressed" if blockers or "regressed" in conclusions else "improved" if conclusions and all(c == "improved" for c in conclusions) else "inconclusive"
    decision = {"benchmark_id": benchmark_id, "version": record["version"], "policy_version": policy,
                "policy": POLICY, "target_id": target.get("id") if target else "running",
                "comparisons": comparisons, "blockers": blockers, "conclusion": conclusion,
                "human_review_required": True}
    decision["pr_head_sha"] = target.get("head_sha") if target else None
    store.execute("INSERT OR IGNORE INTO checkpoint_decisions VALUES(?,?,?,?)", (benchmark_id, policy, time.time(), json.dumps(decision)))
    process_regressions(decision, target)
    publish_status(target, decision)
    return decision


def process_regressions(decision, target):
    """Any confirmed metric regression opens an incident; no held-out text enters repair."""
    failed = {}
    for comparison in decision["comparisons"].values():
        for cohort in ("overall", "held_out"):
            for name, metric in comparison[cohort]["metrics"].items():
                if metric["conclusion"] == "regressed":
                    failed[name] = metric
    if decision["blockers"]:
        failed["checkpoint_contract"] = {"delta": None}
    now = time.time()
    target_id = decision["target_id"]
    first_incident = None
    for name, metric in failed.items():
        key = fingerprint(["checkpoint", target_id, decision["policy_version"], name])
        old = store.rows("SELECT * FROM incidents WHERE fingerprint=?", (key,))
        identifier = old[0]["id"] if old else uuid.uuid4().hex
        payload = {"source": "checkpoint", "metric": name, "version": decision["version"],
                   "benchmark_id": decision["benchmark_id"], "policy_version": decision["policy_version"],
                   "delta": metric.get("delta"), "target_id": target_id,
                   "description": "Full checkpoint found a confirmed regression or hard contract failure. Human review is required."}
        with store.connection() as con:
            con.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(fingerprint) DO UPDATE SET status='open',updated=excluded.updated,payload=excluded.payload",
                        (identifier, key, "open", now, now, json.dumps(payload)))
            if not old or old[0]["status"] != "open":
                store.enqueue("email", "checkpoint-warning:" + identifier + ":" + decision["benchmark_id"], {"incident_id": identifier, "event": "regressed"}, con)
        first_incident = first_incident or identifier
    # A later confirmed stable/improved result resolves that metric's incident.
    for row in store.rows("SELECT * FROM incidents WHERE status='open' AND json_extract(payload,'$.source')='checkpoint' AND json_extract(payload,'$.target_id')=?", (target_id,)):
        payload = json.loads(row["payload"])
        name = payload["metric"]
        states = [c[cohort]["metrics"].get(name, {}).get("conclusion") for c in decision["comparisons"].values() for cohort in ("overall", "held_out")]
        if name not in failed and states and all(s in ("stable", "improved") for s in states):
            store.execute("UPDATE incidents SET status='resolved',updated=? WHERE id=?", (now, row["id"]))
            store.enqueue("email", "checkpoint-recovery:" + row["id"] + ":" + decision["benchmark_id"], {"incident_id": row["id"], "event": "recovered"})
    if not first_incident or not store.setting("auto_repair", False):
        return
    latest_target = next((t for t in targets() if t["id"] == target_id), None)
    if latest_target and latest_target["version"] != decision["version"]:
        return  # A newer revision already exists; do not propose against stale code.
    from quality.evaluation import evaluator_version
    audit = store.setting("evaluator_audit", {})
    if audit.get("status") != "passed" or audit.get("evaluator_version") != evaluator_version():
        return
    if target and target.get("repair_id"):
        prior = store.rows("SELECT payload FROM repairs WHERE id=?", (target["repair_id"],))[0]
        prior_payload = json.loads(prior["payload"])
        if prior_payload.get("revision_number", 1) >= POLICY["max_revisions"]:
            return
        arguments = {"incident_id": first_incident, "baseline_id": target["baseline_id"], "revision_of": target["repair_id"]}
    else:
        # A newly deployed version needs its own measured source baseline before repair.
        arguments = {"incident_id": first_incident, "live": True}
    store.enqueue("repair", "checkpoint-repair:" + decision["benchmark_id"], arguments)


def approve(benchmark_id, expected_version, note, accept_uncertainty=False):
    rows = store.rows("SELECT record FROM checkpoint_decisions WHERE benchmark_id=? AND policy_version=?", (benchmark_id, policy_version()))
    if not rows:
        raise ValueError("Run a full checkpoint under the current policy first")
    decision = json.loads(rows[0]["record"])
    if decision["version"] != expected_version or decision["blockers"] or decision["conclusion"] == "regressed":
        raise ValueError("This checkpoint is blocked or belongs to another version")
    if decision["conclusion"] == "inconclusive" and (not accept_uncertainty or not note.strip()):
        raise ValueError("An inconclusive result requires explicit acceptance and a review note")
    current = next((t for t in targets() if t["id"] == decision["target_id"]), None)
    if current and current["version"] != expected_version:
        raise ValueError("A newer revision exists; evaluate that exact version before approval")
    if current and measurement_context(current["configuration"]) != measurement_context(configuration()):
        raise ValueError("Measurement configuration changed; run a compatible checkpoint first")
    verify_target(current)
    publish_status(current, decision, approved=True)
    from quality.privacy import safe_payload
    store.execute("INSERT OR IGNORE INTO checkpoint_approvals VALUES(?,?,?,?)", (benchmark_id, policy_version(), time.time(), str(safe_payload(note))))
    return {"approved": True, "version": expected_version, "deployment": "not_performed"}


def state():
    return {"enabled": store.setting("checkpoint_schedule_enabled", False), "policy": POLICY,
            "policy_version": policy_version(),
            "targets": [{k: v for k, v in {**t, **schedule_state(t)}.items() if k not in ("candidate", "configuration")} for t in targets()],
            "decisions": [json.loads(r["record"]) for r in store.rows("SELECT record FROM checkpoint_decisions WHERE policy_version=?", (policy_version(),))],
            "approved": [r["benchmark_id"] for r in store.rows("SELECT benchmark_id FROM checkpoint_approvals WHERE policy_version=?", (policy_version(),))]}
