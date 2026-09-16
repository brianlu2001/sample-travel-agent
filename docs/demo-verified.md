# Verified demo outcome

Verified 2026-09-15T23:40:12.864183+00:00.

[Draft PR #2](https://github.com/brianlu2001/sample-travel-agent/pull/2) is open and unmerged. PR #1 is closed and unmerged.

## Completed steps

1. Corrected the evaluator and passed 60 regression checks across 17 preserved actual responses. Phoenix runs custom travel rubrics; representative human calibration remains pending.
2. Simplified the dashboard to quality metrics, PR/escalation history, and paginated trace history. Tool calls, email receipts and version updates are available on demand.
3. Ran 10 fresh conversations (11 actual turns). Correctness at 6/10 triggered the 85% warning automatically. The worker sent email, measured a baseline before proposing a fix, rejected one candidate, validated a revision and opened draft PR #2.

## Measured before and after

60 scenarios, two actual executions per experiment: 120 final scenario outcomes. All four metrics use the same evaluator revision.

| Metric | Original baseline | Validated revision | Change |
|---|---:|---:|---:|
| correctness | 60.8% (73/120) | 86.7% (104/120) | +25.8 pp |
| groundedness | 34.3% (24/70) | 90.1% (64/71) | +55.9 pp |
| topic_relevance | 100.0% (120/120) | 100.0% (120/120) | +0.0 pp |
| task_completion | 40.0% (44/110) | 58.6% (65/111) | +18.6 pp |

[Baseline experiment](http://127.0.0.1:6006/datasets/RGF0YXNldDoz/compare?experimentId=RXhwZXJpbWVudDoxMA==) · [Validated revision experiment](http://127.0.0.1:6006/datasets/RGF0YXNldDoz/compare?experimentId=RXhwZXJpbWVudDoxMg==).

Both native Phoenix experiments contain 120 task runs and 480 metric evaluations. No unknown or pending judgments remain in either sealed comparison. Not-applicable judgments are excluded from each metric’s denominator.

The first candidate was rejected for relevance and held-out relevance/completion regressions. Its evidence remains unchanged. The revision used original incident failures plus failed development cases from that candidate; held-out examples and scores were excluded from proposal generation.

Baseline sealed at 2026-09-15T23:20:05.772451+00:00, before any candidate proposal. Its evidence hash is `3e08cdb4f8877b2e2c8893d0d822829b7d750f4c8f18d0acc6442f84e9486971`.

## Actual email evidence

Recipient: `travel-agent-dev@example.com`. Local SMTP acceptance: `250 Message accepted for local delivery`.

Message-ID: `<8bfc3f92f52e5786a296622e9591279c@quality.local>`.

Received at 2026-09-15T23:03:46.161012+00:00; sender recorded acceptance at 2026-09-15T23:03:46.164897+00:00.

This is an actual SMTP exchange with the demo inbox; external inbox delivery is not claimed. Open the current correctness escalation in the dashboard to read it.

## Recovery and review boundaries

The Anthropic credit outage interrupted provider calls. Completed cases and judgments were reused after credits were restored; failed attempts remain in trace history. The baseline contains 134 recorded turns including failed attempts and an unfinished multi-turn case retry; its sealed score uses the 120 final scenario outcomes. Each candidate recorded 128 actual turns.

The original agent version remains `fcfe8d95359f4baf0db7fdbc8827f6e53595b15eca93818a974db8153692655a`. No proposal was merged or deployed. Task completion is still 58.6% on this reference set, so the agent is not fully capable or production-ready. These are observed automated benchmark results, not statistically established improvements or measured booking outcomes.

66 tests passed; Ruff passed. See [architecture](architecture.md), [metric definitions](metrics.md), [evaluator audit](evaluator-audit.md), and [requirements and skills](requirements.md). Presentation and formal implementation specification remain deferred.
