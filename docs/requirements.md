# Assessment requirements — demo evidence

## What the assessment actually mandates

The supplied `US_FDE_Interview_Screen.pdf`, pages 2–3, requires tracing,
appropriate evaluations, repeatable automation, and actual Arize/Phoenix skills
or CLI usage. Page 2 says: “You may use any orchestration tool or workflow system,
such as the Arize AX Airflow Provider or Kubernetes jobs.” These products are
examples; neither one is individually mandatory. The local demo uses a Python
worker. Following the customer's additional scalability request, the repository
also supplies Kubernetes deployments, a PostgreSQL queue and optional KEDA scaling.
See [deployment and capacity boundaries](deployment.md); no cluster is claimed to
be deployed.

Pages 3–4 require a working demo, a customer-facing presentation, a production
readiness plan and the codebase. There is no requirement to build a custom dashboard
or to use only the Arize UI. Our small dashboard is a customer-requested summary;
Phoenix remains the native trace, evaluation, dataset and experiment workbench.
The presentation and formal implementation specification remain deferred.

The PDF permits local execution but asks for a credible explanation of production
scale. The SQLite/threaded worker is a local POC. The PostgreSQL/Kubernetes profile
supports distributed evaluation workers; million-request capacity still requires
provider quota sizing, load tests, scalable export/monitoring and serving changes.

## 1. Tracing

OpenInference instruments the actual Anthropic calls and agent/tool spans. PII is
redacted locally before persistence and export. Phoenix stores the traces; Arize
AX receives an authenticated mirror. We verified the reported Tokyo trace by
reading it from both Phoenix and Arize AX with the original trace/span IDs.

Relevant files: `quality/runtime.py`, `quality/tracing.py`, `quality/arize_export.py`.
OpenInference supplies AI semantics/instrumentation; OTLP is its standard transport.

## 2. Evaluation and business purpose

Human feedback from our dashboard is stored in the separate `feedback` table and
exported as Phoenix `human_correctness`, `human_groundedness`, etc., with annotator
kind `HUMAN`. Automated annotations retain their original names and values. Live
rates use automated annotations; human labels support judge-agreement review and
do not silently override live scores. Resubmitting feedback replaces the latest
human review for that run; this POC does not retain a full human-review revision log.

| Evaluation | Why it matters to this customer |
| --- | --- |
| Correctness | Wrong cities, directions, dates or durations create unusable trips and rework. |
| Groundedness | Unsupported fares, availability and weather undermine trust and can cause bad purchasing decisions. |
| Topic relevance | Keeps the travel assistant useful within its supported service and prevents unrelated task execution. |
| Completion diagnostic | Retained for historical continuity; no longer a primary metric or automated escalation trigger. |

Use deterministic checks for explicit final-answer day coverage and tool contracts;
use independent Phoenix LLM rubrics for semantic constraints. An internal tool
failure does not itself fail the final answer. Privacy masks are pipeline processing,
not missing output. Unknown and not-applicable labels are explicit. Human calibration
is pending; these scores are automated estimates, not established ground truth.

Relevant files: `quality/evaluation.py`, `quality/answer_checks.py`,
`quality/profiles/travel.py`. Full definitions: [metrics](metrics.md).

## 3. Repeatable automation

Restartable workers evaluate every recorded real chat and publish annotations
to Phoenix. Each acknowledged evaluation emits a monitoring event carrying its
observed window; monitoring does not poll quality every five seconds. A qualifying
breach raises a deduplicated incident, sends local SMTP email, and starts a repair
investigation using those live failures. A current baseline is retained; hard checks
and a targeted development experiment precede the draft PR. Full checkpoints run
daily when a version changed, or manually. Human review controls rollout.

The synthetic reference set is only for before/after validation. Its real model
executions remain labeled as benchmark traffic and cannot trigger the live workflow.
SQLite leases/retries support the local POC. Kubernetes can run those workers with
shared PostgreSQL leases, retries and shutdown draining. The controller stays a
singleton to avoid concurrent schedulers; evaluation workers can scale independently.

Relevant files: `quality/worker.py`, `quality/phoenix_io.py`,
`quality/monitoring.py`, `quality/remediation.py`, `quality/benchmarks.py`,
`quality/postgres.py`, `deploy/kubernetes/`.

## 4. Actual skills and CLI usage

| Skill/tool used | How it helped |
| --- | --- |
| [Phoenix tracing skill](../.agents/skills/phoenix-tracing/SKILL.md) | OpenInference span kinds, trace/session relationships, masking before export, and Phoenix setup. |
| [Phoenix evaluation skill](../.agents/skills/phoenix-evals/SKILL.md) | Code checks for invariants, LLM judges for nuance, binary labels, versioned experiments and human calibration. It also guided the reported judge-error investigation. |
| [Phoenix CLI skill](../.agents/skills/phoenix-cli/SKILL.md) | Inspect real recorded traces to compare agent/LLM/tool output rather than relying only on dashboard summaries. |
| Phoenix CLI 1.18.2 | Ran `trace get ca3cbfb0075b6797dbf9488f4e553e88 --format raw --no-progress` against local Phoenix to inspect the reported failure. |

These development skills do not create traces or substitute for executing and
verifying the application. The automated repair agent now also has a dedicated
Claude Agent SDK runtime with four official skills, native skill loading, and
scoped Phoenix tools. See [runtime skills, triggers and boundaries](repair-runtime.md).

### Further work for a real customer

- Calibrate each judge against representative human reviews, including disagreements,
  redaction cases and unsupported capabilities; version the rubric when it changes.
- Tune window size and alert thresholds to real traffic volume and customer risk;
  track evaluation coverage and queue lag alongside the quality estimates.
- Reuse customer hosting, secrets management, CI runners and notification routing.
  Add safe rollout/rollback only after validated PR review, rather than building
  those production systems into this assessment demo.

The current campaign was also inspected with Phoenix CLI 1.18.2:
`trace get 73cb4248ffd7497e9f1baa5bd63e43c1 --include-annotations`.
It returned the actual agent root, two LLM calls and one flight-search tool call.
The redacted export is retained locally at `.quality/current-campaign-trace.json`.
