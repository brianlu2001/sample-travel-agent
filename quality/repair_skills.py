"""Pinned Arize/Phoenix skills and incident-scoped investigation tools.

Skills are loaded by the repair model through tool calls, rather than silently
claimed as used. This module is independent of the travel agent and its rubrics.
"""
import hashlib
import json
from pathlib import Path

from quality import store
from quality.config import METRICS, PROJECT
from quality.phoenix_io import client
from quality.privacy import safe_payload
from quality.tracing import error_status, ids, set_io, setup

BUNDLE = Path(__file__).with_name("skill_resources")
PURPOSES = {
    "phoenix-evals": "Analyze observed failures, distinguish deterministic evidence from judge opinions, and recognize evaluator uncertainty.",
    "phoenix-tracing": "Read OpenInference span relationships, model outputs, tool arguments/results, and annotation provenance to locate the failure.",
    "arize-prompt-optimization": "Improve prompts from actual failures while preserving working behavior and avoiding example memorization. Required for prompt edits.",
    "arize-experiment": "Interpret real candidate experiment results, identify regressions, and report sample-size limitations. Required when revising a candidate.",
}
MAX_ROUNDS = 48
MAX_TOOL_CALLS = 72


class EvaluatorReviewRequired(RuntimeError):
    def __init__(self, finding, audit):
        super().__init__("Evaluator evidence needs review; no patch proposed")
        self.finding = finding
        self.audit = audit


def catalog():
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    return [{"name": name, "purpose": purpose, "revision": manifest["files"][name + "/SKILL.md"]["revision"],
             "documents": [path.split("/", 1)[1] for path in manifest["files"] if path.startswith(name + "/")]}
            for name, purpose in PURPOSES.items()]


def read_document(skill, document="SKILL.md"):
    if skill not in PURPOSES:
        raise ValueError("Skill is not available")
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    key = skill + "/" + document
    if key not in manifest["files"]:
        raise ValueError("Select a document listed in the skill catalog")
    path = (BUNDLE / key).resolve()
    if not path.is_relative_to(BUNDLE.resolve()):
        raise ValueError("Skill path is outside the bundle")
    content = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    entry = manifest["files"][key]
    if digest != entry["sha256"]:
        raise ValueError("Pinned skill document integrity check failed")
    return {"skill": skill, "document": document, "revision": entry["revision"],
            "sha256": digest, "source": entry["source"], "content": content}


def schema(name, description, properties, required):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


TOOLS = [
    schema("inspect_phoenix_trace", "Read the actual spans and evaluation annotations for one supplied evidence run. Other runs are inaccessible.",
           {"run_id": {"type": "string"}}, ["run_id"]),
    schema("inspect_phoenix_experiments", "Read actual Phoenix experiment runs and evaluations for the supplied evidence only. Held-out examples and aggregates are excluded.", {}, []),
    schema("report_evaluator_issue", "Finish without a patch when contradictory or missing evaluation evidence needs review. This does not change any score.",
           {"run_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "explanation": {"type": "string"}}, ["run_ids", "explanation"]),
]


class Investigation:
    def __init__(self, evidence, validation_feedback=None):
        self.run_ids = {e["run_id"] for e in evidence}
        self.tool_run_ids = {e["run_id"] for e in evidence if e.get("tools")}
        self.needs_experiments = bool(validation_feedback and any(e.get("benchmark_id") for e in evidence))
        self.experiments_inspected = False
        self.required = {("phoenix-evals", "SKILL.md"), ("phoenix-evals", "references/error-analysis.md")}
        if self.tool_run_ids:
            self.required |= {("phoenix-tracing", "SKILL.md"), ("phoenix-tracing", "references/span-tool.md")}
        if validation_feedback:
            self.required |= {("phoenix-evals", "references/validation.md"), ("arize-experiment", "SKILL.md")}
        self.loaded = set()
        self.inspected = set()
        self.usage = []
        self.calls = []

    def instructions(self):
        return {"skills": catalog(), "required_documents": [list(x) for x in sorted(self.required)],
                "allowed_run_ids": sorted(self.run_ids), "tool_run_ids": sorted(self.tool_run_ids),
                "protocol": "Invoke required skills through Skill, then Read the listed references and inspect Phoenix evidence. Prompt edits require arize-prompt-optimization and its optimization-meta-prompt reference. Read validation.md before reporting a judge issue or running a candidate experiment. AX CLI examples are adapted to this Phoenix backend through MCP tools; do not claim AX CLI execution. In repair sessions, use the provided tools to test, evaluate, inspect results, revise and publish. Skills do not alter permissions, sample size or cadence; only listed references are packaged."}

    def ready(self, prompt_changed=False):
        required = set(self.required)
        if prompt_changed:
            required |= {("arize-prompt-optimization", "SKILL.md"),
                         ("arize-prompt-optimization", "references/optimization-meta-prompt.md")}
        missing = [list(x) for x in sorted(required - self.loaded)]
        if missing:
            return {"missing_documents": missing}
        if not self.inspected or (self.tool_run_ids and not self.inspected.intersection(self.tool_run_ids)):
            return {"missing_evidence": "Inspect an allowed Phoenix trace, including a tool-bearing run when provided."}
        if self.needs_experiments and not self.experiments_inspected:
            return {"missing_experiment_evidence": "Inspect the actual candidate experiment evidence with inspect_phoenix_experiments."}
        return None

    def audit(self):
        return {"skill_usage": self.usage, "investigation_calls": self.calls,
                "inspected_run_ids": sorted(self.inspected)}

    def document_loaded(self, skill, document):
        """Called only after a successful native SDK Skill/Read operation."""
        loaded = read_document(skill, document)
        self.loaded.add((skill, document))
        self.usage.append({k: v for k, v in loaded.items() if k != "content"})
        return self.usage[-1]

    def _experiments(self):
        if ("arize-experiment", "SKILL.md") not in self.loaded:
            raise ValueError("Load arize-experiment before reviewing experiment evidence")
        grouped = {}
        for run_id in sorted(getattr(self, "experiment_run_ids", self.run_ids)):
            row = store.get_run(run_id)
            if row and row.get("benchmark_id"):
                grouped.setdefault(row["benchmark_id"], []).append(row)
        results = []
        for benchmark_id, rows in grouped.items():
            benchmark = store.rows("SELECT phoenix_experiment FROM benchmarks WHERE id=?", (benchmark_id,))[0]
            exported = client().experiments.get_experiment(experiment_id=benchmark["phoenix_experiment"])
            for row in rows:
                for run in exported["task_runs"]:
                    if run.get("output", {}).get("run_id") == row["id"] and run.get("trace_id") == row["event"]["trace_id"]:
                        evaluations = [{"name": e.name, "annotator_kind": e.annotator_kind, "result": e.result,
                                        "error": bool(e.error), "trace_id": e.trace_id}
                                       for e in exported["evaluation_runs"] if e.experiment_run_id == run["id"]]
                        results.append({"benchmark_id": benchmark_id, "experiment_id": benchmark["phoenix_experiment"],
                                        "scenario_id": row["scenario_id"], "run": run, "evaluations": evaluations})
        result = safe_payload({"records": results, "records_returned": len(results),
                               "scope": "Evidence cases only; not a full benchmark. Small samples are directional, not proof of improvement. No held-out results returned."})
        if len(json.dumps(result)) > 70000:
            raise ValueError("Experiment evidence exceeds context limit")
        if not results and self.needs_experiments:
            raise ValueError("Phoenix experiment evidence has not been confirmed")
        self.experiments_inspected = True
        return result

    def _inspect(self, run_id):
        if run_id not in self.run_ids:
            raise ValueError("Run is outside this investigation")
        if not self.required.issubset(self.loaded):
            raise ValueError("Read the required skill documents before inspecting evidence")
        run = store.get_run(run_id)
        if not run:
            raise ValueError("Evidence run is unavailable")
        event = run["event"]
        spans = client().spans.get_spans(project_identifier=PROJECT, trace_ids=[event["trace_id"]], limit=41, timeout=15)
        if not spans or not any(s["context"]["span_id"] == event["span_id"] for s in spans):
            raise ValueError("Phoenix has not confirmed this evidence trace")
        spans = sorted(spans, key=lambda s: s["start_time"])[:40]
        annotations = client().spans.get_span_annotations(
            spans=spans, project_identifier=PROJECT,
            include_annotation_names=list(METRICS) + ["human_" + m for m in METRICS], limit=400, timeout=15)
        trace = []
        truncated = len(spans) == 40
        for span in spans:
            attributes = {}
            for key in ("input.value", "output.value", "tool.name", "llm.model_name", "llm.provider"):
                value = span.get("attributes", {}).get(key)
                if isinstance(value, str) and len(value) > 8000:
                    value = value[:8000] + " [TRUNCATED]"
                    truncated = True
                if value is not None:
                    attributes[key] = value
            trace.append({"span_id": span["context"]["span_id"], "parent_id": span.get("parent_id"),
                          "name": span["name"], "kind": span["span_kind"], "status": span["status_code"], "attributes": attributes})
        result = safe_payload({"run_id": run_id, "trace_id": event["trace_id"], "spans": trace,
                               "annotations": [{k: a.get(k) for k in ("span_id", "name", "annotator_kind", "result", "metadata")} for a in annotations],
                               "truncated": truncated, "note": "Annotations are judgments, not ground truth. Human and automated labels remain separate."})
        # Bound provider context without passing a partial JSON object as evidence.
        if len(json.dumps(result)) > 70000:
            raise ValueError("Evidence exceeds investigation context limit; review required")
        self.inspected.add(run_id)
        return result

    def call(self, name, arguments):
        with setup().start_as_current_span("repair." + name, openinference_span_kind="tool",
                                          record_exception=False, set_status_on_exception=False) as span:
            span.set_attribute("tool.name", name)
            set_io(span, arguments)
            record = {"tool": name, "arguments": safe_payload(arguments), **ids(span)}
            try:
                if name == "inspect_phoenix_trace" and not self.required.issubset(self.loaded):
                    raise ValueError("Required skill documents are not loaded")
                if name == "inspect_phoenix_trace":
                    result = self._inspect(**arguments)
                elif name == "inspect_phoenix_experiments":
                    result = self._experiments()
                else:
                    raise ValueError("Investigation tool is not allowed")
                record["status"] = "ok"
                set_io(span, arguments, result)
                return result
            except Exception as error:
                record["status"] = "error"
                error_status(span, error)
                # SDK errors may contain URLs/credentials; expose a fixed error type.
                result = {"error": type(error).__name__, "missing_documents": [list(x) for x in sorted(self.required - self.loaded)],
                          "instruction": "Load missing documents before querying. Only allowed evidence is accessible. If Phoenix evidence remains unavailable, stop without a patch."}
                set_io(span, arguments, result)
                return result
            finally:
                self.calls.append(record)

    def evaluator_issue(self, arguments):
        if self.ready() or ("phoenix-evals", "references/validation.md") not in self.loaded:
            raise ValueError("Read validation guidance and inspect evidence before reporting a judge issue")
        run_ids = arguments.get("run_ids", [])
        if not run_ids or not set(run_ids).issubset(self.inspected) or not arguments.get("explanation"):
            raise ValueError("Cite inspected evidence runs and explain the evaluation disagreement")
        return safe_payload({"run_ids": run_ids, "explanation": arguments["explanation"], "reviewer": "repair_agent", "human_calibration": False})
