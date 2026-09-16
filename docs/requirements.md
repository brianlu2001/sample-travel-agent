# Assessment requirements — demo evidence

## 1. Tracing

OpenInference instruments the actual Anthropic calls and agent/tool spans. PII is
redacted locally before persistence and export. Phoenix stores the traces; Arize
AX receives an authenticated mirror. We verified the reported Tokyo trace by
reading it from both Phoenix and Arize AX with the original trace/span IDs.

Relevant files: `quality/runtime.py`, `quality/tracing.py`, `quality/arize_export.py`.
OpenInference supplies AI semantics/instrumentation; OTLP is its standard transport.

## 2. Evaluation and business purpose

| Evaluation | Why it matters to this customer |
| --- | --- |
| Correctness | Wrong cities, directions, dates or durations create unusable trips and rework. |
| Groundedness | Unsupported fares, availability and weather undermine trust and can cause bad purchasing decisions. |
| Topic relevance | Keeps the travel assistant useful within its supported service and prevents unrelated task execution. |
| Task completion proxy | Checks whether the user received a usable deliverable rather than only an accurate refusal or clarification. |

Use deterministic checks for explicit final-answer day coverage and tool contracts;
use independent Phoenix LLM rubrics for semantic constraints. An internal tool
failure does not itself fail the final answer. Privacy masks are pipeline processing,
not missing output. Unknown and not-applicable labels are explicit. Human calibration
is pending; these scores are automated estimates, not established ground truth.

Relevant files: `quality/evaluation.py`, `quality/answer_checks.py`,
`quality/profiles/travel.py`. Full definitions: [metrics](metrics.md).

## 3. Repeatable automation

One restartable worker evaluates every recorded real chat and publishes annotations
to Phoenix. It polls the latest live-chat window every five seconds. A qualifying
breach raises a deduplicated incident, sends local SMTP email, and starts a repair
investigation using those live failures. A current baseline is retained; hard checks
and a targeted development experiment precede the draft PR. Full checkpoints run
daily when a version changed, or manually. Human review controls rollout.

The synthetic reference set is only for before/after validation. Its real model
executions remain labeled as benchmark traffic and cannot trigger the live workflow.
SQLite leases/retries are sufficient for this local POC; no separate orchestration
platform is needed to meet the repeatability requirement.

Relevant files: `quality/worker.py`, `quality/phoenix_io.py`,
`quality/monitoring.py`, `quality/remediation.py`, `quality/benchmarks.py`.

## 4. Actual skills and CLI usage

| Skill/tool used | How it helped |
| --- | --- |
| [Phoenix tracing skill](../.agents/skills/phoenix-tracing/SKILL.md) | OpenInference span kinds, trace/session relationships, masking before export, and Phoenix setup. |
| [Phoenix evaluation skill](../.agents/skills/phoenix-evals/SKILL.md) | Code checks for invariants, LLM judges for nuance, binary labels, versioned experiments and human calibration. It also guided the reported judge-error investigation. |
| [Phoenix CLI skill](../.agents/skills/phoenix-cli/SKILL.md) | Inspect real recorded traces to compare agent/LLM/tool output rather than relying only on dashboard summaries. |
| Phoenix CLI 1.18.2 | Ran `trace get ca3cbfb0075b6797dbf9488f4e553e88 --format raw --no-progress` against local Phoenix to inspect the reported failure. |

The skills are development guidance; they do not create traces or substitute for
actually executing and verifying the application.

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
