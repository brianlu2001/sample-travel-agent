"""Version history from running processes and recorded evidence, never invented releases."""
import json
import time

from quality import store
from quality.config import fingerprint


def measurement_context(manifest):
    """Agent changes are compared within the same dataset/judge/configuration."""
    return fingerprint({key: manifest.get(key) for key in (
        "fixture_version", "dataset_version", "evaluator_version", "privacy_version",
        "repetitions", "models", "packages")})


def archive_benchmark(con, benchmark_id):
    """Save the completed evidence atomically with sealing; never replace a version."""
    if con.execute("SELECT 1 FROM benchmark_versions WHERE benchmark_id=?", (benchmark_id,)).fetchone():
        return
    row = con.execute("SELECT * FROM benchmarks WHERE id=? AND status='complete'", (benchmark_id,)).fetchone()
    if not row or not row["report"]:
        return
    manifest, report = json.loads(row["manifest"]), json.loads(row["report"])
    observed = con.execute("SELECT DISTINCT version FROM runs WHERE benchmark_id=?", (benchmark_id,)).fetchall()
    version = observed[0]["version"] if len(observed) == 1 else (
        manifest["agent_version"] if not manifest.get("candidate") else None)
    record = {"id": benchmark_id, "kind": row["kind"], "parent_id": row["parent_id"],
              "version": version, "completed": row["completed"],
              "context": measurement_context(manifest), "manifest": manifest, "report": report,
              "phoenix_dataset": row["phoenix_dataset"], "phoenix_experiment": row["phoenix_experiment"]}
    record["snapshot_hash"] = fingerprint(record)
    con.execute("INSERT INTO benchmark_versions VALUES(?,?)", (benchmark_id, json.dumps(record)))


def archive_benchmarks():
    """Backfill only sealed, recorded experiments; no model runs or new telemetry."""
    with store.connection() as con:
        for row in con.execute("SELECT id FROM benchmarks WHERE status='complete' ORDER BY completed,id").fetchall():
            archive_benchmark(con, row["id"])


def saved_benchmarks():
    records = [json.loads(row["record"]) for row in store.rows("SELECT record FROM benchmark_versions")]
    return sorted(records, key=lambda record: (record["completed"] or 0, record["id"]))


def baseline_for(manifest):
    """First sealed baseline in this measurement context remains the fixed anchor."""
    context = measurement_context(manifest)
    # Derive context from the original manifest, without rewriting historical
    # records. Acceptance-policy changes do not change already measured scores.
    return next((r["id"] for r in saved_benchmarks() if r["kind"] == "baseline" and measurement_context(r["manifest"]) == context), None)


def register_running_agent(snapshot):
    """Called at API startup, after importing the code that will serve requests."""
    with store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        found = con.execute("SELECT value FROM settings WHERE key='agent_version_history'").fetchone()
        history = json.loads(found["value"]) if found else []
        previous = history[-1] if history else None
        changed = not previous or any(previous.get(k) != snapshot.get(k) for k in ("version", "model"))
        if changed:
            at = time.time()
            provenance = "API startup observed this loaded version"
            if previous is None:
                recorded = con.execute("SELECT MIN(created) at FROM runs WHERE source='live' AND version=?", (snapshot["version"],)).fetchone()
                if recorded["at"] is not None:
                    at = recorded["at"]
                    provenance = "First recorded live execution; deployment time was not recorded"
            changed_files = [name for name, digest in snapshot["files"].items()
                             if previous and previous.get("files", {}).get(name) != digest]
            history.append({**snapshot, "at": at, "previous_version": previous["version"] if previous else None,
                            "changed_files": changed_files, "provenance": provenance})
            history = history[-30:]
            con.execute("INSERT INTO settings VALUES('agent_version_history',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(history),))
        con.execute("INSERT INTO settings VALUES('serving_agent',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({**snapshot, "started": time.time()}),))


def recent_updates(active_evaluator, repairs):
    history = store.setting("agent_version_history", [])
    updates = []
    for record in saved_benchmarks():
        if record["kind"] not in ("baseline", "checkpoint"):
            continue
        updates.append({"kind": record["kind"], "at": record["completed"], "version": record["version"],
                        "status": "saved", "summary": "Sealed full evaluation · " + record["id"][:8],
                        "detail": "Immutable scores and measurement configuration; not an agent deployment",
                        "requires_revalidation": record["manifest"].get("evaluator_version") != active_evaluator})
    for index, entry in enumerate(history):
        changes = ", ".join(entry["changed_files"])
        if index and history[index-1].get("model") != entry.get("model"):
            changes = ", ".join(filter(None, [changes, "model configuration"]))
        updates.append({"kind": "agent", "at": entry["at"], "version": entry["version"],
                        "previous_version": entry["previous_version"],
                        "status": "running" if index == len(history)-1 else "previous",
                        "summary": "Changed " + changes if changes else "Agent code first observed",
                        "detail": entry["provenance"], "model": entry["model"]})
    # Preserve the history even when a live run has been reassessed by a newer judge.
    evaluations = store.rows("""SELECT result FROM evaluation_history
        UNION ALL SELECT evaluation AS result FROM runs WHERE evaluation IS NOT NULL""")
    first_seen = {}
    for row in evaluations:
        result = json.loads(row["result"])
        version, started = result.get("version"), result.get("started")
        if version and started and (version not in first_seen or started < first_seen[version]["started"]):
            first_seen[version] = result
    previous = None
    for version, result in sorted(first_seen.items(), key=lambda item: item[1]["started"]):
        updates.append({"kind": "evaluator", "at": result["started"], "version": version,
                        "previous_version": previous, "status": "current" if version == active_evaluator else "previous",
                        "summary": "Evaluator revision observed" if previous else "Initial evaluator observed",
                        "detail": "First use in recorded evaluations; separate from agent deployment",
                        "model": result.get("judge_model")})
        previous = version
    for repair in repairs:
        payload = repair["payload"]
        benchmark_id = payload.get("candidate_benchmark_id")
        versions = store.rows("SELECT DISTINCT version FROM runs WHERE benchmark_id=?", (benchmark_id,)) if benchmark_id else []
        updates.append({"kind": "candidate", "at": repair["updated"],
                        "version": versions[0]["version"] if len(versions) == 1 else payload.get("candidate_hash"),
                        "previous_version": None, "status": repair["status"],
                        "summary": payload.get("summary", "Repair investigation"),
                        "detail": "Proposed change; not a running-agent update",
                        "pr_url": payload.get("pr_url"), "requires_revalidation": repair.get("requires_revalidation", False)})
    return sorted(updates, key=lambda entry: entry["at"], reverse=True)[:20]
