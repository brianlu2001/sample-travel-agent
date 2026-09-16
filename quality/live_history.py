"""Persistent live quality cohorts, separate from the active alert window.

Read recorded evaluations across deployments and retain previous judge versions.
No model calls, synthetic telemetry, or changes to monitoring decisions.
"""
import json

from quality import store
from quality.config import WINDOW, fingerprint
from quality.monitoring import summarize


def performance_history(active_agent, active_evaluator):
    rows = store.rows("""
        WITH assessments AS (
            SELECT id,created,version,COALESCE(json_extract(event,'$.conversation_id'),id) conversation_id,
                   COALESCE(json_extract(evaluation,'$.version'),?) evaluator_version,
                   evaluation,COALESCE(evaluated,created) assessed_at,1 current_assessment
            FROM runs WHERE source='live' AND benchmark_id IS NULL
            UNION ALL
            SELECT r.id,r.created,r.version,COALESCE(json_extract(r.event,'$.conversation_id'),r.id),
                   h.version,h.result,h.started,0
            FROM evaluation_history h JOIN runs r ON r.id=h.run_id
            WHERE r.source='live' AND r.benchmark_id IS NULL
        ), judgments AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY id,evaluator_version
                ORDER BY current_assessment DESC,assessed_at DESC) judgment_rank FROM assessments
        ), conversations AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY version,evaluator_version,conversation_id
                ORDER BY created DESC,id DESC) turn_rank FROM judgments WHERE judgment_rank=1
        ), windows AS (
            SELECT *,COUNT(*) OVER(PARTITION BY version,evaluator_version) conversation_count,
                   ROW_NUMBER() OVER(PARTITION BY version,evaluator_version ORDER BY created DESC,id DESC) window_rank
            FROM conversations WHERE turn_rank=1
        ) SELECT id,created,version,evaluator_version,evaluation,conversation_count
          FROM windows WHERE window_rank<=? ORDER BY created DESC,id DESC
    """, (active_evaluator, WINDOW))
    groups = {}
    for row in rows:
        key = (row["version"], row["evaluator_version"])
        groups.setdefault(key, []).append(row)
    cohorts = []
    for (version, judge), sample in groups.items():
        cohorts.append({"id": fingerprint([version, judge]), "version": version, "evaluator_version": judge,
                        "current_agent": version == active_agent, "current_evaluator": judge == active_evaluator,
                        "last_seen": sample[0]["created"], "first_in_window": sample[-1]["created"],
                        "conversation_count": sample[0]["conversation_count"], "report": summarize(sample),
                        "run_ids": [r["id"] for r in sample]})
    cohorts.sort(key=lambda c: (c["last_seen"], c["current_evaluator"], c["id"]), reverse=True)
    return {"cohorts": cohorts, "window": WINDOW, "retention": "All recorded versions; no 24-hour history cutoff",
            "source": "Recorded evaluations in the durable journal; warning decisions use the active Phoenix window"}


def assessment_for(row, version):
    """Resolve a previous judge's latest assessment without overwriting a score."""
    current = json.loads(row["evaluation"]) if isinstance(row.get("evaluation"), str) else row.get("evaluation")
    if not version or current and current.get("version") == version:
        return current
    archived = store.rows("SELECT result FROM evaluation_history WHERE run_id=? AND version=? ORDER BY started DESC LIMIT 1",
                          (row["id"], version))
    return json.loads(archived[0]["result"]) if archived else None
