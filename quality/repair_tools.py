"""Durable, constrained tools for a single SDK-owned repair loop.

The model chooses the next action. These functions enforce artifact identity,
fixed checks, development-only experiments, revision limits and draft-only GitHub.
"""
import json
import subprocess
import sys
import uuid

from quality import store
from quality.benchmarks import configuration
from quality.checkpoint_policy import POLICY
from quality.config import PRIMARY_METRICS, ROOT, fingerprint
from quality.privacy import safe_payload
from quality.sandbox import validate_candidate
from quality.repair_skills import schema


TOOLS = [
    schema("get_repair_state", "Read the saved candidate, checks and development results; use to resume interrupted work.", {}, []),
    schema("run_candidate_checks", "Run fixed independent invariants and regression tests on the current candidate. No arbitrary commands.", {}, []),
    schema("run_targeted_evaluation", "Run or resume the fixed development-only Phoenix experiment for this checked candidate. Repeated calls reuse the same results. May take several minutes.", {}, []),
    schema("publish_draft_pr", "Publish the exact checked/evaluated candidate as a draft PR, or update its existing open PR. Requires Phoenix result inspection. Never merges.", {}, []),
    schema("finish_repair", "Stop with an unresolved issue for operator review when no publishable fix is supported. Does not release the metric reservation or claim success.",
           {"reason": {"type": "string", "minLength": 1}}, ["reason"]),
]


class RepairTools:
    def __init__(self, repair_id, directory, payload, evidence, prior=None):
        self.repair_id, self.directory, self.payload = repair_id, directory, payload
        self.evidence, self.prior = evidence, prior
        self.baseline_id = payload["baseline_id"]
        self.saved = payload.setdefault("sdk_workflow", {"configuration": configuration(), "revisions": []})
        # A new host invocation is an explicit operational retry. Keep the
        # measured candidate, but let the SDK retry a blocked GitHub operation.
        if self.saved.get("status") == "awaiting_github_access":
            self.saved.pop("outcome", None)
        self.investigation = None

    @property
    def finished(self):
        return bool(self.saved.get("outcome"))

    @property
    def revision(self):
        return self.saved["revisions"][-1] if self.saved["revisions"] else None

    def save(self, status):
        from quality.remediation import update
        self.saved["status"] = status
        update(self.repair_id, status, self.payload)

    def current(self):
        if configuration() != self.saved["configuration"]:
            raise ValueError("Configuration changed; operator must rebase and measure the current agent")
        if not self.revision:
            raise ValueError("Stage a candidate with propose_patch first")
        path = self.directory / self.revision["path"]
        candidate = json.loads(path.read_text(encoding="utf-8"))
        validate_candidate(candidate)
        if fingerprint(candidate) != self.revision["candidate_hash"]:
            raise ValueError("Candidate changed since it was staged")
        return candidate, path

    def state(self):
        candidate = self.current()[0] if self.revision else None
        return {"status": self.saved.get("status", "diagnosing"), "outcome": self.saved.get("outcome"),
                "revisions_used": len(self.saved["revisions"]), "max_revisions": POLICY["max_revisions"],
                "candidate": candidate, "current_revision": self.revision,
                "pr_url": self.payload.get("pr_url"), "full_evaluation": "Daily or manual only"}

    def stage(self, candidate):
        from quality.remediation import STATE
        candidate = dict(candidate)
        previous = self.current()[0] if self.revision else None
        if not previous and self.prior:
            previous = json.loads((STATE / "repairs" / self.payload["revision_of"] / "candidate.json").read_text(encoding="utf-8"))
        candidate["functions"] = {**(previous or {}).get("functions", {}), **candidate["functions"]}
        # Redact before storage AND validation: the measured artifact is the saved artifact.
        candidate = safe_payload(candidate)
        validate_candidate(candidate)
        digest = fingerprint(candidate)
        if self.revision and digest == self.revision["candidate_hash"]:
            return self.state()
        if len(self.saved["revisions"]) >= POLICY["max_revisions"]:
            raise ValueError("Three candidate revisions exhausted; publish a checked candidate or finish_repair")
        if self.revision and self.revision.get("targeted_benchmark_id"):
            self.require_review()
        revision = {"number": len(self.saved["revisions"])+1, "candidate_hash": digest}
        path = self.directory / "revisions" / str(revision["number"]) / "candidate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        revision["path"] = path.relative_to(self.directory).as_posix()
        self.saved["revisions"].append(revision)
        # Checkpoint/dashboard compatibility; historical revision artifacts are retained.
        (self.directory / "candidate.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        for key in ("invariant_gate", "artifact_gate", "gate", "targeted_benchmark_id"):
            self.payload.pop(key, None)
        self.payload.update(candidate_hash=digest, summary=candidate["summary"], rationale=candidate["rationale"])
        self.save("validating")
        return self.state()

    def files(self):
        from quality.remediation import candidate_files, REGRESSION_TEST
        files = candidate_files(self.current()[0], self.baseline_id)
        files["tests/test_agent_regressions.py"] = REGRESSION_TEST
        return files

    def checks(self):
        from quality.remediation import verify_artifact
        _, path = self.current()
        if "checks" not in self.revision:
            checked = subprocess.run([sys.executable, "-X", "utf8", "-m", "quality.validation", str(path)],
                                     cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=30)
            invariants = json.loads(checked.stdout) if checked.returncode == 0 else {"passed": False, "reason": "Constrained validation failed"}
            files = self.files()
            artifact = verify_artifact(files, path.parent)
            self.revision.update(checks={"invariants": invariants, "artifact": artifact,
                                        "passed": bool(invariants.get("passed") and artifact.get("passed"))},
                                 files_hash=fingerprint(files))
            self.payload.update(invariant_gate=invariants, artifact_gate=artifact)
            self.save("validating")
        return self.revision["checks"]

    def require_checks(self):
        self.current()
        if not self.revision.get("checks", {}).get("passed") or fingerprint(self.files()) != self.revision.get("files_hash"):
            raise ValueError("The exact candidate artifact must pass run_candidate_checks first")

    def evaluate(self):
        from quality.benchmarks import create, run
        from quality.targeted import select
        self.require_checks()
        required = {("arize-experiment", "SKILL.md"), ("phoenix-evals", "references/validation.md")}
        if not required.issubset(self.investigation.loaded):
            raise ValueError("Load arize-experiment and phoenix-evals references/validation.md before evaluation")
        # A fixed id survives a crash after experiment creation but before acknowledgment.
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, "sdk-targeted:"+self.repair_id+":"+self.revision["candidate_hash"]).hex
        self.revision["targeted_benchmark_id"] = identifier
        self.payload["targeted_benchmark_id"] = identifier
        self.save("experimenting")
        create(kind="targeted", parent_id=self.baseline_id, candidate=str(self.current()[1]),
               repetitions=POLICY["targeted_repetitions"], benchmark_id=identifier,
               examples=select(self.evidence, POLICY["targeted_cases"]))
        report = run(identifier, concurrency=3)
        self.require_checks()
        # Never pass a generic benchmark report (which can contain holdouts) to the SDK.
        self.allow_results(identifier, report)
        blockers = []
        if report.get("privacy_failures"):
            blockers.append("Privacy redaction failed")
        if report.get("tool_contract_failures"):
            blockers.append("Deterministic tool contracts failed")
        if any(m.get("unknown") or m.get("pending") for n, m in report["metrics"].items() if n in PRIMARY_METRICS):
            blockers.append("Targeted evaluation is incomplete")
        self.revision["results"] = {"metrics": report["metrics"], "evidence_hash": report["evidence_hash"],
                                    "case_run_ids": report["case_run_ids"], "gate": {"passed": not blockers, "reasons": blockers,
                                    "stage": "targeted", "full_checkpoint": "pending"}}
        self.payload["gate"] = self.revision["results"]["gate"]
        self.save("experimenting")
        return {**self.revision["results"], "instruction": "Inspect actual Phoenix experiments and a candidate trace before revising or publishing. Semantic scores are development signals, not greedy acceptance gates."}

    def allow_results(self, identifier, report):
        benchmark = store.rows("SELECT * FROM benchmarks WHERE id=?", (identifier,))[0]
        manifest = json.loads(benchmark["manifest"])
        if (benchmark["kind"] != "targeted" or benchmark["parent_id"] != self.baseline_id or
                manifest.get("candidate_version") != self.revision["candidate_hash"] or
                not manifest.get("case_metadata") or any(c["split"] != "development" for c in manifest["case_metadata"])):
            raise ValueError("Only this candidate's development results may enter the repair context")
        allowed = {c["id"] for c in manifest["case_metadata"]}
        rows = [store.get_run(i) for i in report["case_run_ids"]]
        if (not rows or any(not r or r["benchmark_id"] != identifier or r["scenario_id"] not in allowed for r in rows) or
                fingerprint([{k: r[k] for k in ("id", "event", "evaluation")} for r in rows]) != report["evidence_hash"]):
            raise ValueError("Targeted evidence changed or includes an unapproved run")
        investigation = self.investigation
        investigation.run_ids.update(r["id"] for r in rows)
        investigation.tool_run_ids.update(r["id"] for r in rows if r["event"].get("tools"))
        # Prioritize failures, while keeping actual trace access to every development case.
        rows.sort(key=lambda r: not any(m["label"] == "fail" for n, m in r["evaluation"]["metrics"].items() if n in PRIMARY_METRICS))
        current_ids = {r["id"] for r in rows}
        if getattr(investigation, "candidate_run_ids", set()) != current_ids:
            investigation.experiments_inspected = False
        investigation.candidate_run_ids = current_ids
        investigation.experiment_run_ids = {r["id"] for r in rows[:3]}
        investigation.needs_experiments = True

    def restore_results(self):
        if self.revision and self.revision.get("results"):
            self.allow_results(self.revision["targeted_benchmark_id"], self.revision["results"])

    def require_review(self):
        self.restore_results()
        investigation = self.investigation
        if not investigation.experiments_inspected or not investigation.inspected.intersection(getattr(investigation, "candidate_run_ids", set())):
            raise ValueError("Inspect Phoenix experiment results and at least one current candidate trace before revising or publishing")

    def publish(self):
        from quality.remediation import candidate_files, publish, REGRESSION_TEST
        from quality.checkpoints import register_candidate, publish_status, targets
        self.require_checks()
        if not self.revision.get("results", {}).get("gate", {}).get("passed"):
            raise ValueError("Complete this candidate's targeted evaluation and hard gates before publishing")
        self.require_review()
        candidate, path = self.current()
        files = self.files()
        previous_files = None
        if self.prior and self.prior.get("pr_url"):
            previous_path = self.directory.parent / self.payload["revision_of"] / "candidate.json"
            previous_files = candidate_files(json.loads(previous_path.read_text(encoding="utf-8")), self.baseline_id)
            previous_files["tests/test_agent_regressions.py"] = REGRESSION_TEST
        body = self.description(candidate)
        (self.directory / "pr-description.md").write_text(body, encoding="utf-8")
        (self.directory / "candidate.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self.save("awaiting_review")
        try:
            self.payload["pr_url"] = publish(self.payload["root_repair_id"], files, body, self.baseline_id, previous_files)
        except Exception as error:
            self.payload["publication_error"] = type(error).__name__
            self.save("awaiting_github_access")
            raise
        self.payload.pop("publication_error", None)
        self.payload.update(store.setting("publication:"+self.payload["root_repair_id"], {}))
        self.saved["outcome"] = "draft_pr"
        self.save("pr_open")
        try:
            register_candidate(self.repair_id)
            target = next(t for t in targets() if t.get("repair_id") == self.repair_id)
            publish_status(target, {"conclusion": "pending_full_evaluation"})
        except Exception as error:
            self.payload["checkpoint_registration_error"] = type(error).__name__
            self.save("pr_open")  # An existing PR must never be reported as unpublished.
        return {"pr_url": self.payload["pr_url"], "status": "draft", "instruction": "Stop. Human review and merge required."}

    def description(self, candidate):
        results = self.revision["results"]
        trigger = ("Triggered by an operator-requested review of recorded failures." if self.payload.get("trigger") == "operator_review" else
                   "Triggered by a rolling live traffic quality incident." if self.payload.get("trigger") == "live_chat_incident" else
                   "Triggered by the recorded quality incident.")
        rows = ["| Metric | Targeted candidate results |", "|---|---:|"]
        for name, m in results["metrics"].items():
            rows.append(f"| {name} | {m['pass_rate']:.1%} ({m['pass']}/{m['n']}) |" if m.get("pass_rate") is not None else f"| {name} | N/A |")
        skills = "\n".join(f"- `{s['skill']}/{s['document']}` — [pinned source]({s['source']})" for s in self.investigation.usage)
        findings = safe_payload(self.payload.get("review_findings", []))
        return (f"## Problem and resulting behavior\n\n{candidate['summary']}\n\n{candidate['rationale']}\n\n{trigger}\n\n"
                "## Validation\n\n"+"\n".join(rows)+
                f"\n\nFixed development-only check, one execution per case. Baseline `{self.baseline_id}`; experiment `{self.revision['targeted_benchmark_id']}`; evidence SHA-256 `{results['evidence_hash']}`. "
                "Independent invariants and artifact tests passed. Targeted scores are development signals, not a full-set improvement claim. "
                "LLM judgments await human calibration. Full evaluation remains daily (24 hours AND a new version) or manual.\n\n"
                f"## Arize/Phoenix skills used\n\n{skills}\n\n"
                f"## Review\n\nRecorded findings (unchanged reviewer provenance): {json.dumps(findings)}\n\n"
                "One SDK session controls investigation, tests, targeted evaluation and this draft publication. Human review and approval are required; merge and deployment are not automated.\n")

    def call(self, name, arguments):
        if self.finished:
            raise ValueError("Repair already finished")
        if name == "get_repair_state":
            self.restore_results()
            return self.state()
        if name == "run_candidate_checks":
            return self.checks()
        if name == "run_targeted_evaluation":
            return self.evaluate()
        if name == "publish_draft_pr":
            return self.publish()
        if name == "finish_repair":
            if not arguments.get("reason", "").strip():
                raise ValueError("Explain why review is needed")
            self.saved["outcome"] = "needs_attention"
            self.payload["summary"] = safe_payload(arguments["reason"])
            self.save("awaiting_github_access" if self.payload.get("publication_error") else "awaiting_human_evidence")
            return {"status": "needs_attention", "instruction": "Stop. No merge or quality success claimed."}
        raise ValueError("Unknown repair tool")
