"""Metric-specific rolling windows, persisted incident state and notification outbox."""
import json
import math
import time
import uuid

from quality import store
from quality.config import METRICS, PRIMARY_METRICS, MIN_SAMPLES, PERSISTENCE, RECOVERY, THRESHOLD, WINDOW, fingerprint


def summarize(run_rows):
    metrics = {}
    for name in METRICS:
        labels = [(json.loads(r["evaluation"]) if isinstance(r.get("evaluation"), str) else r.get("evaluation") or {}).get("metrics", {}).get(name, {}).get("label", "pending") for r in run_rows]
        counts = {label: labels.count(label) for label in ("pass", "fail", "unknown", "not_applicable", "pending")}
        n = counts["pass"] + counts["fail"]
        rate = counts["pass"] / n if n else None
        interval = None
        if n:
            z = 1.96
            center = (rate + z*z/(2*n))/(1+z*z/n)
            delta = z*math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/(1+z*z/n)
            interval = [max(0, center-delta), min(1, center+delta)]
        metrics[name] = {**counts, "n": n, "pass_rate": rate, "wilson_95": interval,
                         "coverage": (len(labels)-counts["pending"]-counts["unknown"])/len(labels) if labels else None}
    return {"total": len(run_rows), "metrics": metrics}


def current_window(source, version, benchmark_id=None, evaluation_version=None, now=None):
    """Latest response per conversation, including pending evaluations."""
    if evaluation_version is None:
        from quality.evaluation import evaluator_version
        evaluation_version = evaluator_version()
    cutoff = (time.time() if now is None else now) - 86400
    if source == "scenario":
        cutoff = max(cutoff, store.setting("scenario_campaign", {}).get("created", 0))
    if source in ("live", "scenario"):
        from quality.phoenix_io import live_window
        return live_window(version, evaluation_version, WINDOW, cutoff, source=source)
    recent = store.rows("SELECT * FROM runs WHERE source=? AND version=? AND benchmark_id IS ? AND created>? ORDER BY created DESC LIMIT ?",
                        (source, version, benchmark_id, cutoff, WINDOW))
    for row in recent:
        evaluation = json.loads(row["evaluation"]) if row["evaluation"] else None
        if evaluation and evaluation.get("version") != evaluation_version:
            # Older judgments are visible in history, but pending for the active judge.
            row["evaluation"] = None
    return recent


def monitor(source, version, benchmark_id=None, evaluation_version=None, window_snapshot=None):
    if evaluation_version is None:
        from quality.evaluation import evaluator_version
        evaluation_version = evaluator_version()
    if source not in ("live", "scenario") or benchmark_id:
        return {"status": "offline_experiment_only"}
    # Benchmark runs and live user traffic never share a denominator.
    scope = f"{source}:{version}:{benchmark_id or 'live'}:{evaluation_version}"
    recent = current_window(source, version, benchmark_id, evaluation_version) if window_snapshot is None else window_snapshot
    report = summarize(recent)
    store.set_setting(source + "_monitor", {"status": "connected", "at": time.time(),
                                     "source": "Phoenix span annotations", "requests": len(recent),
                                     "evaluated_run_ids": [r["id"] for r in recent if
                                         len((r.get("evaluation") or {}).get("metrics", {})) == 4]})
    evaluated_total = store.rows("SELECT COUNT(DISTINCT COALESCE(json_extract(event,'$.conversation_id'),id)) n FROM runs WHERE source=? AND version=? AND benchmark_id IS ? AND json_extract(evaluation,'$.version')=?",
                                 (source, version, benchmark_id, evaluation_version))[0]["n"]
    previous = store.rows("SELECT state FROM monitors WHERE scope=?", (scope,))
    state = json.loads(previous[0]["state"]) if previous else {}
    for name in PRIMARY_METRICS:
        metric = report["metrics"][name]
        key = fingerprint([scope, name])
        incident = store.rows("SELECT * FROM incidents WHERE fingerprint=?", (key,))
        if metric["n"] < MIN_SAMPLES or metric["pass_rate"] is None:
            state.pop(name, None)
            continue
        if metric["pass_rate"] < THRESHOLD:
            entry = state.setdefault(name, {})
            entry.setdefault("breach_at", evaluated_total)
            entry.pop("recovery_at", None)
            if evaluated_total - entry["breach_at"] >= PERSISTENCE:
                payload = {"scope": scope, "source": source, "version": version, "benchmark_id": benchmark_id,
                           "episode_id": uuid.uuid4().hex,
                           "evaluator_version": evaluation_version,
                           "metric": name, "measurement": metric, "threshold": THRESHOLD,
                           "window": WINDOW, "min_samples": MIN_SAMPLES,
                           "window_run_ids": [r["id"] for r in recent if r.get("id")],
                           "window_trace_ids": [r["trace_id"] for r in recent],
                           "failing_run_ids": [r["id"] for r in recent if r.get("id") and
                                               (r.get("evaluation") or {}).get("metrics", {}).get(name, {}).get("label") == "fail"],
                           "description": f"{source} traffic quality threshold breach; not a statistical claim of population drift."}
                now = time.time()
                if not incident or incident[0]["status"] == "resolved":
                    from quality.repair_dispatch import active
                    in_progress = active(source, name)
                    incident_id = incident[0]["id"] if incident else uuid.uuid4().hex
                    if in_progress and incident and in_progress['incident_id'] == incident_id:
                        # Recovery/rebreach must not replace the evidence being
                        # investigated or create another episode during review.
                        payload = json.loads(incident[0]['payload'])
                        payload['measurement'] = metric
                    with store.connection() as con:
                        con.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET status='open',updated=excluded.updated,payload=excluded.payload",
                                    (incident_id, key, "open", now, now, json.dumps(payload)))
                        if not in_progress:
                            store.enqueue("email", f"incident:{incident_id}:{payload['episode_id']}", {"incident_id": incident_id, "event": "opened"}, con)
                else:
                    # Keep the triggering conversations stable while new chats
                    # arrive and the repair's before/after experiment is running.
                    original = json.loads(incident[0]["payload"])
                    original["measurement"] = metric
                    store.execute("UPDATE incidents SET updated=?,payload=? WHERE id=?", (now, json.dumps(original), incident[0]["id"]))
        elif metric["pass_rate"] >= RECOVERY:
            entry = state.setdefault(name, {})
            entry.pop("breach_at", None)
            if "recovery_at" not in entry:
                entry["recovery_at"] = evaluated_total
            if evaluated_total-entry["recovery_at"] >= PERSISTENCE:
                if incident and incident[0]["status"] == "open":
                    store.execute("UPDATE incidents SET status='resolved',updated=? WHERE id=?", (time.time(), incident[0]["id"]))
                    store.enqueue("email", f"recovery:{incident[0]['id']}:{entry['recovery_at']}", {"incident_id": incident[0]["id"], "event": "recovered"})
                state.pop(name, None)
        else:
            state.pop(name, None)
    store.execute("INSERT INTO monitors VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET state=excluded.state", (scope, json.dumps(state)))
    schedule_repair()
    return report


def schedule_repair():
    from quality.repair_dispatch import schedule
    for incident in store.rows("SELECT * FROM incidents WHERE status='open' ORDER BY created"):
        schedule(incident)


def evaluation_ready(payload):
    """Runs only after Phoenix acknowledges annotations; stale events cannot alert."""
    from quality.evaluation import evaluator_version
    from quality.config import agent_version
    if (not store.setting('monitor_enabled', True) or payload['source'] not in ('live','scenario')
            or payload['evaluation_version'] != evaluator_version()
            or payload['version'] != store.setting('serving_agent', {}).get('version',agent_version())):
        return
    try:
        return monitor(payload['source'],payload['version'],evaluation_version=payload['evaluation_version'],
                       window_snapshot=payload.get('window_snapshot'))
    except Exception as error:
        store.set_setting('live_monitor', {'status':'unavailable','at':time.time(),'error_type':type(error).__name__})
        raise  # Durable retry; no lost edge when Phoenix is briefly unavailable.
