"""Read models for the demo dashboard; no manufactured metrics or event data."""
import base64
import json
import math
import uuid
from phoenix.client import Client

from quality import store
from quality.config import ARIZE_API_KEY, ARIZE_SPACE_ID, METRICS, PRIMARY_METRICS, MIN_SAMPLES, PERSISTENCE, PHOENIX, RECOVERY, THRESHOLD, WINDOW, agent_version
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
    repair_requests = store.rows('SELECT * FROM repair_requests ORDER BY created DESC LIMIT 100')
    for request in repair_requests:
        request['detail'] = json.loads(request['detail'])
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
    from quality.email_history import delivery_history
    from quality.live_history import performance_history
    return {"benchmarks": benchmarks, "live": summarize(live), "live_series": series(list(reversed(live))), "runs": runs,
            "provider_block": store.setting("provider_block"),
            "full_evaluation": full_evaluation_status(),
            "checkpoints": checkpoint_state(),
            "incidents": incidents, "repairs": repairs, "jobs": jobs,
            "repair_requests": repair_requests,
            "emails": emails,
            "email_events": delivery_history(),
            "live_history": performance_history(serving_agent["version"], active_evaluator),
            "phoenix_url": PHOENIX, "baseline_id": store.setting("baseline_id"),
            "auto_repair": store.setting("auto_repair", False),
            "live_status": live_status,
            "arize": {"configured": bool(ARIZE_API_KEY and ARIZE_SPACE_ID),
                      "delivery": store.setting("arize_delivery"), "project": store.setting("arize_project"),
                      "verified_trace": store.setting("arize_verified_trace")},
            "evaluator_version": active_evaluator,
            "serving_agent": {**serving_agent, "restart_required": serving_agent["version"] != agent_version()},
            "version_updates": recent_updates(active_evaluator, repairs),
            "monitor": {"delivery_mode": "evaluation_events", "metrics": PRIMARY_METRICS, "threshold": THRESHOLD, "window": WINDOW, "minimum_samples": MIN_SAMPLES,
                        "max_age_hours": 24, "persistence": PERSISTENCE, "recovery_threshold": RECOVERY},
            "calibration": calibration()}


def run_history(source="online", benchmark_id=None, cursor=None, limit=20,
                metric=None, label=None, evaluator="all", conversation_id=None, agent_version=None, evaluation_version=None):
    """Stable pagination over recorded executions; never limited to the live window."""
    if source not in ("online", "live", "scenario", "benchmark", "validation", "all") or not 1 <= limit <= 100:
        raise ValueError("Invalid history filter")
    if metric is not None and metric not in METRICS:
        raise ValueError("Invalid metric filter")
    if label not in (None, "pass", "fail", "unknown", "not_applicable", "pending") or evaluator not in ("all", "current"):
        raise ValueError("Invalid evaluation filter")
    active = evaluator_version()
    clauses, args = [], []
    if source == "online":
        clauses.append("source IN ('live','scenario')")
    elif source != "all":
        clauses.append("source=?")
        args.append(source)
    if benchmark_id:
        clauses.append("benchmark_id=?")
        args.append(benchmark_id)
    if conversation_id:
        clauses.append("COALESCE(json_extract(event,'$.conversation_id'),id)=?")
        args.append(conversation_id)
    if agent_version:
        clauses.append("version=?")
        args.append(agent_version)
    # Resolve the selected historical judge before filtering labels or pagination.
    # Current/pending assessments remain visible without inventing a historical judgment.
    relation = "runs"
    relation_args = []
    if evaluation_version:
        relation = """(SELECT r.id,r.created,r.source,r.version,r.benchmark_id,r.scenario_id,r.event,r.evaluated,
            CASE WHEN json_extract(r.evaluation,'$.version')=? THEN r.evaluation
                 ELSE (SELECT h.result FROM evaluation_history h WHERE h.run_id=r.id AND h.version=?
                       ORDER BY h.started DESC LIMIT 1) END evaluation
            FROM runs r) selected_runs"""
        relation_args = [evaluation_version, evaluation_version]
        clauses.append("(evaluation IS NOT NULL OR ?=?)")
        args.extend((evaluation_version, active))
    if evaluator == "current":
        clauses.append("(evaluation IS NULL OR json_extract(evaluation,'$.version')=?)")
        args.append(active)
    if label:
        # Filter before counting and pagination. Missing judgments are pending;
        # N/A and unknown remain distinct from both passes and failures.
        selected = (metric,) if metric else PRIMARY_METRICS
        clauses.append("(" + " OR ".join("COALESCE(json_extract(evaluation,?),'pending')=?" for _ in selected) + ")")
        for name in selected:
            args.extend((f"$.metrics.{name}.label", label))
    elif metric:
        clauses.append("json_extract(evaluation,?) IS NOT NULL")
        args.append(f"$.metrics.{metric}")
    scope = " AND ".join(clauses) or "1=1"
    total = store.rows("SELECT COUNT(*) n FROM " + relation + " WHERE " + scope, [*relation_args, *args])[0]["n"]
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
    rows = store.rows("SELECT * FROM " + relation + " WHERE " + where + " ORDER BY created DESC,id DESC LIMIT ?", (*relation_args, *args, limit+1))
    items = []
    for row in rows[:limit]:
        event = json.loads(row["event"])
        evaluation = json.loads(row["evaluation"]) if row["evaluation"] else None
        conversation = event.get("input", [])
        question = conversation[-1].get("content", "") if isinstance(conversation, list) and conversation else "[withheld]"
        items.append({"id": row["id"], "created": row["created"], "source": row["source"],
                      "benchmark_id": row["benchmark_id"], "scenario_id": row["scenario_id"],
                      "version": row["version"], "question": str(question)[:240],
                      "conversation_id": event.get("conversation_id") or row["id"],
                      "status": event.get("status"), "trace_id": event.get("trace_id"),
                      "tool_count": len(event.get("tools", [])),
                      "metrics": evaluation.get("metrics") if evaluation else None,
                      "evaluator_version": evaluation.get("version") if evaluation else None,
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
