"""Read the real Phoenix call tree for dashboard inspection."""
import json
from datetime import datetime

from quality.config import PROJECT
from quality.phoenix_io import client


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def trace_details(trace_id):
    try:
        recorded = client().spans.get_spans(project_identifier=PROJECT, trace_ids=[trace_id], limit=200, timeout=5)
    except Exception:
        return {"status": "unavailable", "spans": [], "message": "Phoenix is unavailable; recorded run evidence is still shown below."}
    recorded.sort(key=lambda span: span["start_time"])
    parents = {s["context"]["span_id"]: s["parent_id"] for s in recorded}
    spans = []
    for span in recorded:
        attrs = span["attributes"]
        parent, seen = span["parent_id"], set()
        while parent in parents and parent not in seen:
            seen.add(parent)
            parent = parents[parent]
        start, end = datetime.fromisoformat(span["start_time"]), datetime.fromisoformat(span["end_time"])
        spans.append({"id": span["context"]["span_id"], "parent_id": span["parent_id"], "depth": len(seen),
                      "name": span["name"], "kind": span["span_kind"], "status": span["status_code"],
                      "started": start.timestamp(), "duration_ms": round((end-start).total_seconds()*1000, 2),
                      "model": attrs.get("llm.model_name"), "tool": attrs.get("tool.name"),
                      "tokens": {name: attrs.get("llm.token_count."+name) for name in ("prompt", "completion", "total")},
                      "input": decode(attrs.get("input.value")), "output": decode(attrs.get("output.value")),
                      "error_type": attrs.get("error.type"), "attributes": attrs})
    return {"status": "ready" if spans else "pending", "spans": spans,
            "truncated": len(recorded) == 200,
            "message": "Actual Phoenix spans; expand a call to inspect its redacted input and output." if spans else "Trace export is pending."}
