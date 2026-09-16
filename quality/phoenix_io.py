"""Phoenix is the source of live quality scores, not a parallel metrics database."""
import json
from datetime import datetime, timezone
from functools import lru_cache

from phoenix.client import Client

from quality import store
from quality.config import METRICS, PHOENIX, PROJECT


@lru_cache(maxsize=1)
def client():
    return Client(base_url=PHOENIX)


def live_window(version, evaluation_version, limit, cutoff, source="live"):
    # The journal selects distinct conversations (including unexported requests).
    # Scores are read exclusively from their native Phoenix span annotations.
    recorded = store.rows("""SELECT * FROM (
        SELECT id,created,event,ROW_NUMBER() OVER (
            PARTITION BY COALESCE(json_extract(event,'$.conversation_id'),id)
            ORDER BY created DESC,id DESC) position
        FROM runs WHERE source=? AND version=? AND created>?
        ) WHERE position=1 ORDER BY created DESC,id DESC LIMIT ?""",
        (source, version, cutoff, limit))
    if not recorded:
        return []
    events = {r["id"]: json.loads(r["event"]) for r in recorded}
    expected_spans = {e["span_id"]: e["trace_id"] for e in events.values()}
    # Source/version are already selected by the journal. Redaction can mask a
    # version hash in exported metadata, so it must not be an additional join key.
    # Native trace/span IDs survive redaction and identify the exact executions.
    spans = client().spans.get_spans(
        project_identifier=PROJECT, parent_id="null", span_kind="AGENT",
        trace_ids=[e["trace_id"] for e in events.values()],
        start_time=datetime.fromtimestamp(cutoff, timezone.utc), limit=limit, timeout=5)
    spans = [span for span in spans if (context := span.get("context", {})).get("span_id") in expected_spans
             and expected_spans[context["span_id"]] == context.get("trace_id")]
    matched_ids = {span["context"]["span_id"] for span in spans}
    annotations = client().spans.get_span_annotations(
        spans=spans, project_identifier=PROJECT,
        include_annotation_names=list(METRICS), limit=limit*len(METRICS), timeout=5) if spans else []
    scores = {}
    for annotation in annotations:
        if annotation["span_id"] in matched_ids and (annotation.get("metadata") or {}).get("evaluator_version") == evaluation_version:
            scores.setdefault(annotation["span_id"], {})[annotation["name"]] = annotation["result"]
    return [{"id": r["id"], "trace_id": events[r["id"]]["trace_id"],
             "conversation_id": events[r["id"]].get("conversation_id", r["id"]),
             "span_id": events[r["id"]]["span_id"], "created": r["created"],
             "evaluation": {"version": evaluation_version, "metrics": scores[events[r["id"]]["span_id"]]}
             if events[r["id"]]["span_id"] in scores else None} for r in recorded]
