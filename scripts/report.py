"""Export measured results only. Never creates traces, evaluations or labels."""
import json
from datetime import datetime, timezone

from quality import store
from quality.config import ROOT


def main():
    baseline_id = store.setting("baseline_id")
    benchmarks = store.rows("SELECT * FROM benchmarks WHERE status='complete' ORDER BY created")
    baseline = next(b for b in benchmarks if b["id"] == baseline_id)
    candidate = next((b for b in reversed(benchmarks) if b["parent_id"] == baseline_id), None)
    lines = ["# Measured demo results", "", "Generated from the local execution database. No demonstration scores were injected.", ""]
    from quality.evaluation import evaluator_version
    if json.loads(baseline["manifest"])["evaluator_version"] != evaluator_version():
        lines += ["> Historical results: the evaluator was corrected after a confirmed final-answer/tool-draft and redaction error. These scores and the PR acceptance gate require revalidation. They are preserved for audit and do not drive the live workflow.", ""]
    lines += ["| Metric | Baseline | Candidate |", "|---|---:|---:|"]
    before = json.loads(baseline["report"])
    after = json.loads(candidate["report"]) if candidate else None
    def rate(metric):
        return "—" if metric["pass_rate"] is None else f"{metric['pass_rate']:.1%} ({metric['pass']}/{metric['n']} applicable)"
    for name, metric in before["metrics"].items():
        lines.append(f"| {name} | {rate(metric)} | {rate(after['metrics'][name]) if after else 'Pending'} |")
    lines += ["", "## Provenance", "", "Each phase uses 60 synthetic reference scenarios × 2 actual executions. Four multi-turn scenarios produce 128 observed turns; only the final turn of each scenario contributes to the 120-case headline scores.", ""]
    for item in [baseline, *([candidate] if candidate else [])]:
        report = json.loads(item["report"])
        stamp = datetime.fromtimestamp(item["completed"], timezone.utc).isoformat()
        lines += [f"- {item['kind'].title()}: `{item['id']}`; completed {stamp}.",
                  f"  - Evidence SHA-256: `{report['evidence_hash']}`.",
                  f"  - Tool contract failures across final-turn evidence: {report['tool_contract_failures']}."]
    repairs = store.rows("SELECT * FROM repairs WHERE json_extract(payload,'$.baseline_id')=? ORDER BY created DESC", (baseline_id,))
    if repairs:
        repair = repairs[0]
        assert baseline["completed"] < repair["created"], "Baseline must precede improvement"
        payload = json.loads(repair["payload"])
        lines += ["", "## Improvement workflow", "", "The sealed baseline predates the repair agent invocation.",
                  f"Repair status: **{repair['status']}**."]
        if payload.get("pr_url"):
            lines += ["", f"[Human-review draft PR]({payload['pr_url']})"]
        if payload.get("gate"):
            lines += ["", "```json", json.dumps(payload["gate"], indent=2), "```"]
        for prior in repairs[1:]:
            previous = json.loads(prior["payload"])
            lines += ["", f"Prior proposal `{prior['id']}` remains **{prior['status']}**: " + "; ".join(previous.get("gate", {}).get("reasons", [])) + "."]
    if after:
        lines += ["", "## Held-out results", "", "15 held-out scenarios × two executions. No held-out conversations were supplied to the repair model.", "",
                  "| Metric | Baseline | Final candidate |", "|---|---:|---:|"]
        for name, metric in before["held_out"]["metrics"].items():
            lines.append(f"| {name} | {rate(metric)} | {rate(after['held_out']['metrics'][name])} |")
    lines += ["", "## Interpretation", "", "Scores are automated judgments pending human calibration. Unknowns and non-applicable labels are excluded from pass-rate denominators and remain visible on the dashboard. The 15 held-out scenarios were not supplied to patch generation. Repetitions are correlated; these results do not establish statistical significance or booking conversion.", "", "An earlier integration run was invalidated after a redaction defect was found. Its stored identifiers were masked, and the baseline was rerun before any improvement was allowed. Its scores are excluded from this comparison.", ""]
    path = ROOT / "docs" / "demo-results.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
