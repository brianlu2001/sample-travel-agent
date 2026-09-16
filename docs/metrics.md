# How ongoing quality is measured

Each real agent turn produces a sanitized response, conversation and tool trace.
The three primary metrics are correctness, groundedness and topic relevance.
Completion remains a diagnostic for continuity; it no longer triggers new live
alerts or checkpoint regressions. The audited four-output judge is unchanged, so
existing baseline scores and the evaluator audit remain compatible.
An asynchronous worker applies four independent Phoenix evaluators using Claude
Sonnet 4.6 at temperature zero. That reduces randomness; it does not guarantee a
correct judgment. No human calibration accuracy has yet been established.

| Metric | Passing behavior | Important boundary |
| --- | --- | --- |
| Correctness | Satisfies the latest request and constraints: destination, direction, dates, duration, budget, supported capabilities. | A necessary clarification or honest limitation can pass. |
| Groundedness | Material factual claims agree with independent fixture evidence, with limitations disclosed. | Suggested itinerary activities and general tips need no database entry. No material factual claims means not applicable. |
| Topic relevance | Travel assistance, greetings, relevant clarifications, or a polite redirect from unrelated tasks. | An inaccurate travel answer can still be relevant. |
| Completion diagnostic | Provides the useful deliverable the user requested. | Clarification alone fails completion even if correct. Unrelated requests are not applicable. This is not measured booking conversion or satisfaction. |

## How a judgment is reached

1. Inspect **this turn’s user-visible response** against the current conversation.
   “Final response” here means after this turn’s tool calls, not the last turn of
   the entire conversation. Every turn is evaluated asynchronously. Later explicit
   user constraints override earlier ones.
2. Run a conservative code check for explicit requested duration versus numbered
   day sections in that final response. Empty headings and unsupported formats are
   inconclusive, not automatic passes. Missing/duplicate numbered days fail
   correctness and completion when the explicit format can be checked.
3. Give the semantic judges independent facts for flight, hotel and weather calls.
   Never treat the generic `create_itinerary` draft as authoritative evidence for
   what activities/days the final answer is allowed to suggest. Its contract
   failure is retained separately in tool diagnostics.
4. Return `pass`, `fail`, `not_applicable`, or `unknown`, with an explanation and
   evaluator version. Privacy markers are pipeline processing, not output defects.
   If masking destroys decisive evidence, the judge must use unknown rather than
   reconstructing private text. Provider failures are unknown and retried.
5. Preserve original evaluations when reassessing live traffic. Sealed benchmark
   evaluations cannot be overwritten; a changed judge requires a new comparison.

There is no deterministic checker for every semantic constraint yet. The code
day check covers an explicit numbered format; the LLM handles other formats and
remaining constraints. A passing day check proves coverage only, not the quality
of the activities. Human review remains necessary to assess evaluator reliability.

## Rolling calculation and alerts

The durable execution journal selects the latest response per conversation, and
the monitor reads its current-version evaluation annotations from Phoenix. Offline benchmark scores cannot trigger live repairs. SQLite holds
the execution/retry journal and incident state, not the authoritative live scores.

- Live window: latest **20 conversations within 24 hours**, same agent version.
  Each conversation contributes only its latest response, including pending evaluations.
  Only the active evaluator version contributes scores. Older evaluations count
  as pending reassessment, not passing or failing. Benchmarks are separate cohorts.
- Each rate is **pass / (pass + fail)**. Unknown, pending and not-applicable
  judgments are displayed but excluded from the applicable denominator.
- Coverage is **(pass + fail + not applicable) / requests**. High rates with low
  coverage can mislead. The UI displays applicable and excluded counts. Wilson intervals remain in the
  API evidence; they do not measure judge error or repeated-case correlation.
- Each of the three primary metrics needs **10 applicable results**. A rate below
  **85%** opens a deduplicated incident immediately after evaluation and delivers
  local SMTP email. A rate at least **95%** resolves it. This is an operational threshold, not proof
  of statistical population drift. Completion labels remain inspectable without creating new alerts.
- Jobs run asynchronously. Dashboard refresh is every **5 seconds**; results may
  take longer while provider requests complete or retry.
- Human labels are kept separately. Judge agreement is calculated only against
  labels for the active evaluator. Versioning prevents silent mixing of rubrics;
  it does not establish accuracy without representative human review.

## LLM versus programmatic evaluation

| Component | Implementation |
| --- | --- |
| Correctness semantics | Phoenix `ClassificationEvaluator` with a custom travel rubric and Claude Sonnet 4.6. |
| Explicit numbered-day coverage | Code: detects decisive missing or duplicated day sections and overrides correctness to fail; passing this check does not prove overall correctness. |
| Groundedness semantics | Phoenix LLM judge compares the response with independently derived fixture evidence. Evidence construction and tool-contract checks are code. |
| Topic relevance | Phoenix LLM judge checks travel scope or an appropriate redirect. |
| Completion diagnostic | Existing LLM rubric with the same deterministic day/error overrides; retained outside headline metrics and new policy decisions. |
| Privacy/error handling, counts and alerts | Programmatic redaction checks, error classification, pass-rate denominators, windows, threshold checks and deduplication. |
| Human feedback | Separate human labels, never presented as LLM or programmatic judgments. |

## Intermediate turns and scope boundary

**Implemented:** every user-visible turn is evaluated; trace history can filter by
metric, label and evaluator version. The Conversation action lists earlier turns
even when the latest response passes. Missing evaluations are pending, separate
from unknown and not applicable. Completion can be explicitly selected as a diagnostic.

**Explicitly out of scope:** a separate conversation-review evaluator. The customer
declined it to keep the demonstration simple and reusable across agent types.
Retain per-turn evaluation and conversation inspection without adding a frustration
score, session-specific repair trigger, or new orchestration service for session review.
Evaluate the request known at each turn; do not impose future user requests on an
earlier response. Turn count alone is not proof of frustration.

Phoenix already groups these traces through `session.id`. See the official
[session documentation](https://arize.com/docs/phoenix/tracing/llm-traces/sessions).

## Confirmed evaluator defect: Tokyo three-day trace

Trace `ca3cbfb0075b6797dbf9488f4e553e88` requested a three-day Tokyo plan. The
tool draft had two days, but the final response explicitly contained Days 1, 2
and 3. The old judge conflated the draft with the answer and treated `[PERSON]`
privacy masks as unfilled content. Those reasons do not justify failing the answer.

The corrected evaluator separates these inputs, checks final day coverage in code,
and explains privacy processing to the judge. It does not guess or restore masked
place names. Previous benchmark scores and repair gates remain historical and
require revalidation. Live incidents now trigger the workflow. A current baseline
is measured before proposing a fix whenever the old baseline is incompatible. This fix is not a claim that all judge errors
have been eliminated.

## Current evaluator audit

See [evaluator audit](evaluator-audit.md): 60 regression expectations across 17
actual saved responses passed. These developer-authored expectations are not a
substitute for representative customer calibration. PR #1 was closed because it
relied on previous evaluator results. Fresh before/after measurements are required.

## Current checkpoint policy

Each candidate revision runs deterministic invariants, artifact tests and a targeted
development suite of up to 15 scenarios with one execution each. Semantic scores
guide development and do not require a five-point improvement on every revision.
A draft PR can open after these checks, with full evaluation pending.

Full checkpoints run after both 24 hours since the last compatible full result and
a changed, unmeasured version, or on explicit manual request. They compare against
the original baseline and the most recently human-approved checkpoint. Repeated
executions are clustered by scenario for uncertainty estimates. Conclusions are
improved, inconclusive or regressed; an inconclusive result needs explicit human
acceptance and a note. Hard contract failures and confirmed regressions block approval.
Every metric decline remains visible in the comparison record. See
[evaluation cadence](evaluation-cadence.md) for the exact policy and limits.

## Historical policy used for PR #2

Before the new baseline is measured, the comparison policy is frozen in its manifest.
A candidate needs at least a five-percentage-point gain in any of the four metrics,
no metric decline greater than two points, no held-out decline greater than five
points, and no increase in unknown judgments or pending evaluations. Independent
tool invariants and tests of the proposed files must pass. These gates do not
claim statistical significance. Floating-point rounding does not change boundaries.
