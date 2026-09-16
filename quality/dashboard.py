"""Read models for the demo dashboard; no manufactured metrics or event data."""
import base64
import json
import math
import uuid
from phoenix.client import Client

from quality import store
from quality.config import ARIZE_API_KEY, ARIZE_SPACE_ID, METRICS, MIN_SAMPLES, PERSISTENCE, PHOENIX, RECOVERY, THRESHOLD, WINDOW, agent_version
from quality.evaluation import evaluator_version
from quality.monitoring import current_window, summarize


def state():
    from quality.versions import saved_benchmarks
    saved = {record["id"]: record for record in saved_benchmarks()}
    active_evaluator = evaluator_version()
    serving_agent = store.setting("serving_agent", {"version": agent_version()})
    benchmarks = store.rows("SELECT * FROM benchmarks ORDER BY created")
    for benchmark in benchmarks:
        benchmark["manifest"] = json.loads(benchmark["manifest"])
        benchmark["requires_revalidation"] = benchmark["manifest"].get("evaluator_version") != active_evaluator
        benchmark["report"] = json.loads(benchmark["report"]) if benchmark["report"] else None
        counts = store.rows("SELECT COUNT(*) total,SUM(evaluated IS NOT NULL) evaluated FROM runs WHERE benchmark_id=?", (benchmark["id"],))[0]
        benchmark["progress"] = counts
        versions = store.rows("SELECT DISTINCT version FROM runs WHERE benchmark_id=?", (benchmark["id"],))
        benchmark["current_version"] = versions[0]["version"] if len(versions) == 1 else "pending"
        record = saved.get(benchmark["id"])
        benchmark["saved_version"] = {key: record[key] for key in ("version", "snapshot_hash", "completed")} if record else None
        benchmark["experiment_url"] = Client(base_url=PHOENIX).experiments.get_experiment_url(
            dataset_id=benchmark["phoenix_dataset"], experiment_id=benchmark["phoenix_experiment"])
        rows = store.rows("SELECT created,evaluation FROM runs WHERE benchmark_id=? ORDER BY created", (benchmark["id"],))
        benchmark["current"] = summarize(rows)
        benchmark["series"] = series(rows)
    # Retain recent conversations even while hundreds of validation turns arrive.
    latest = store.rows("SELECT * FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY source ORDER BY created DESC) position FROM runs) WHERE position<=30 ORDER BY created DESC")
    try:
        live = current_window("live", serving_agent["version"], evaluation_version=active_evaluator)
        live_status = "connected"
    except Exception:
        live, live_status = [], "Phoenix unavailable; live scores withheld"
    try:
        scenario = current_window("scenario", serving_agent["version"], evaluation_version=active_evaluator)
        scenario_status = "connected"
    except Exception:
        scenario, scenario_status = [], "Phoenix unavailable; scenario scores withheld"
    runs = []
    for row in latest:
        event = json.loads(row["event"])
        evaluation = json.loads(row["evaluation"]) if row["evaluation"] else None
        content = event["input"][-1].get("content", "") if isinstance(event["input"], list) and event["input"] else "[withheld]"
        runs.append({"id": row["id"], "created": row["created"], "source": row["source"],
                     "benchmark_id": row["benchmark_id"], "scenario_id": row["scenario_id"],
                     "question": str(content)[:160], "status": event["status"],
                     "metrics": evaluation["metrics"] if evaluation else None, "trace_id": event["trace_id"]})
        runs[-1]["evaluator_version"] = evaluation.get("version") if evaluation else None
        runs[-1]["requires_revalidation"] = bool(evaluation and evaluation.get("version") != active_evaluator)
    incidents = store.rows("SELECT * FROM incidents ORDER BY created DESC LIMIT 100")
    repairs = store.rows("SELECT * FROM repairs ORDER BY created DESC LIMIT 100")
    for row in incidents + repairs:
        row["payload"] = json.loads(row["payload"])
    for row in incidents:
        row["requires_revalidation"] = row["payload"].get("metric") != "privacy" and row["payload"].get("evaluator_version") != active_evaluator
    for row in repairs:
        baseline = next((b for b in benchmarks if b["id"] == row["payload"].get("baseline_id")), None)
        row["requires_revalidation"] = bool(baseline and baseline["requires_revalidation"])
    jobs = store.rows("SELECT kind,state,COUNT(*) n FROM jobs GROUP BY kind,state")
    emails = store.rows("SELECT e.*,r.accepted,r.transport,r.smtp_code,r.smtp_response FROM emails e LEFT JOIN email_receipts r ON r.id=e.id ORDER BY e.received DESC LIMIT 100")
    delivery_jobs = {f"<{uuid.uuid5(uuid.NAMESPACE_URL, j['key']).hex}@quality.local>": json.loads(j["payload"])
                     for j in store.rows("SELECT key,payload FROM jobs WHERE kind='email'")}
    for email in emails:
        email["incident_id"] = delivery_jobs.get(email["id"], {}).get("incident_id")
    from quality.versions import recent_updates
    from quality.full_evaluation import status as full_evaluation_status
    from quality.checkpoints import state as checkpoint_state
    return {"benchmarks": benchmarks, "live": summarize(live), "live_series": series(list(reversed(live))), "runs": runs,
            "scenario": summarize(scenario), "scenario_series": series(list(reversed(scenario))),
            "scenario_status": scenario_status, "scenario_campaign": store.setting("scenario_campaign"),
            "provider_block": store.setting("provider_block"),
            "full_evaluation": full_evaluation_status(),
            "checkpoints": checkpoint_state(),
            "incidents": incidents, "repairs": repairs, "jobs": jobs,
            "emails": emails,
            "phoenix_url": PHOENIX, "baseline_id": store.setting("baseline_id"),
            "auto_repair": store.setting("auto_repair", False),
            "live_status": live_status,
            "arize": {"configured": bool(ARIZE_API_KEY and ARIZE_SPACE_ID),
                      "delivery": store.setting("arize_delivery"), "project": store.setting("arize_project"),
                      "verified_trace": store.setting("arize_verified_trace")},
            "evaluator_version": active_evaluator,
            "serving_agent": {**serving_agent, "restart_required": serving_agent["version"] != agent_version()},
            "version_updates": recent_updates(active_evaluator, repairs),
            "monitor": {"threshold": THRESHOLD, "window": WINDOW, "minimum_samples": MIN_SAMPLES,
                        "max_age_hours": 24, "persistence": PERSISTENCE, "recovery_threshold": RECOVERY},
            "calibration": calibration()}


def run_history(source="online", benchmark_id=None, cursor=None, limit=20):
    """Stable pagination over recorded executions; never limited to the live window."""
    if source not in ("online", "live", "scenario", "benchmark", "validation", "all") or not 1 <= limit <= 100:
        raise ValueError("Invalid history filter")
    clauses, args = [], []
    if source == "online":
        clauses.append("source IN ('live','scenario')")
    elif source != "all":
        clauses.append("source=?")
        args.append(source)
    if benchmark_id:
        clauses.append("benchmark_id=?")
        args.append(benchmark_id)
    scope = " AND ".join(clauses) or "1=1"
    total = store.rows("SELECT COUNT(*) n FROM runs WHERE " + scope, args)[0]["n"]
    if cursor:
        try:
            before, identifier = json.loads(base64.urlsafe_b64decode(cursor).decode("utf-8"))
            if not isinstance(before, (int, float)) or not math.isfinite(before) or not isinstance(identifier, str):
                raise ValueError()
        except Exception:
            raise ValueError("Invalid history cursor") from None
        clauses.append("(created<? OR (created=? AND id<?))")
        args.extend((before, before, identifier))
    where = " AND ".join(clauses) or "1=1"
    rows = store.rows("SELECT * FROM runs WHERE " + where + " ORDER BY created DESC,id DESC LIMIT ?", (*args, limit+1))
    active = evaluator_version()
    items = []
    for row in rows[:limit]:
        event = json.loads(row["event"])
        evaluation = json.loads(row["evaluation"]) if row["evaluation"] else None
        conversation = event.get("input", [])
        question = conversation[-1].get("content", "") if isinstance(conversation, list) and conversation else "[withheld]"
        items.append({"id": row["id"], "created": row["created"], "source": row["source"],
                      "benchmark_id": row["benchmark_id"], "scenario_id": row["scenario_id"],
                      "version": row["version"], "question": str(question)[:240],
                      "status": event.get("status"), "trace_id": event.get("trace_id"),
                      "tool_count": len(event.get("tools", [])),
                      "metrics": evaluation.get("metrics") if evaluation else None,
                      "requires_revalidation": bool(evaluation and evaluation.get("version") != active)})
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last["created"], last["id"]]).encode()).decode()
    return {"items": items, "total": total, "next_cursor": next_cursor}


def series(rows):
    """Actual rolling evaluations, with no interpolation or prefilled history."""
    points = []
    for i, row in enumerate(rows):
        if not row.get("evaluation"):
            continue
        window = [r for r in rows[max(0, i-WINDOW+1):i+1] if r["created"] > row["created"]-86400]
        report = summarize(window)
        points.append({"at": row["created"], "samples": len(window),
                       "metrics": {name: report["metrics"][name]["pass_rate"] for name in METRICS}})
    return points


def calibration():
    feedback = store.rows("SELECT f.labels,r.evaluation FROM feedback f JOIN runs r ON r.id=f.run_id WHERE json_extract(r.evaluation,'$.version')=?", (evaluator_version(),))
    result = {}
    for metric in METRICS:
        counts = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
        for row in feedback:
            human = json.loads(row["labels"]).get(metric)
            predicted = json.loads(row["evaluation"] or "{}").get("metrics", {}).get(metric, {}).get("label")
            if human not in ("pass", "fail") or predicted not in ("pass", "fail"):
                continue
            counts[{("pass", "pass"): "tp", ("fail", "fail"): "tn", ("fail", "pass"): "fp", ("pass", "fail"): "fn"}[(human, predicted)]] += 1
        total = sum(counts.values())
        result[metric] = {**counts, "reviewed": total, "agreement": (counts["tp"]+counts["tn"])/total if total else None}
    return {"status": "human_labels_available" if feedback else "pending_human_review", "metrics": result}
