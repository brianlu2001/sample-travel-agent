> For the current evaluator and completed feedback loop, see [verified demo outcome](demo-verified.md). The results below are historical.

# Measured demo results

Generated from the local execution database. No demonstration scores were injected.

> Historical results: the evaluator was corrected after a confirmed final-answer/tool-draft and redaction error. These scores and the PR acceptance gate require revalidation. They are preserved for audit and do not drive the live workflow.

| Metric | Baseline | Candidate |
|---|---:|---:|
| correctness | 52.5% (63/120 applicable) | 92.5% (111/120 applicable) |
| groundedness | 43.1% (44/102 applicable) | 91.7% (88/96 applicable) |
| topic_relevance | 100.0% (120/120 applicable) | 100.0% (120/120 applicable) |
| task_completion | 25.2% (28/111 applicable) | 77.1% (84/109 applicable) |

## Provenance

Each phase uses 60 synthetic reference scenarios × 2 actual executions. Four multi-turn scenarios produce 128 observed turns; only the final turn of each scenario contributes to the 120-case headline scores.

- Baseline: `6888900c8c444f24822f5ab3a8a8b531`; completed 2026-09-15T20:49:51.377127+00:00.
  - Evidence SHA-256: `94c45e611e0cf4a18a2b06b3e199f6372cdd85f400a2fda2fe323dc846be0dee`.
  - Tool contract failures across final-turn evidence: 80.
- Candidate: `376a93ce843347679769887a66572177`; completed 2026-09-15T21:08:58.060368+00:00.
  - Evidence SHA-256: `8a3ec309c837ccb9e83d823d1374c263e3609f9c2ccafe7cdd844a3792e05e24`.
  - Tool contract failures across final-turn evidence: 8.

## Improvement workflow

The sealed baseline predates the repair agent invocation.
Repair status: **pr_open**.

[Human-review draft PR](https://github.com/brianlu2001/sample-travel-agent/pull/1)

```json
{
  "passed": true,
  "reasons": [],
  "deltas": {
    "correctness": 0.4,
    "groundedness": 0.48529411764705876,
    "topic_relevance": 0.0,
    "task_completion": 0.5183899495826102
  },
  "interpretation": "Observed repeated benchmark results; not a claim of statistical significance or human-calibrated accuracy."
}
```

Prior proposal `58cb6a124c9246e38e0e1f92cb75ca68` remains **rejected**: topic_relevance: more than 2 percentage points regression.

## Held-out results

15 held-out scenarios × two executions. No held-out conversations were supplied to the repair model.

| Metric | Baseline | Final candidate |
|---|---:|---:|
| correctness | 60.0% (18/30 applicable) | 80.0% (24/30 applicable) |
| groundedness | 45.8% (11/24 applicable) | 73.9% (17/23 applicable) |
| topic_relevance | 100.0% (30/30 applicable) | 100.0% (30/30 applicable) |
| task_completion | 25.0% (7/28 applicable) | 57.1% (16/28 applicable) |

## Interpretation

Scores are automated judgments pending human calibration. Unknowns and non-applicable labels are excluded from pass-rate denominators and remain visible on the dashboard. The 15 held-out scenarios were not supplied to patch generation. Repetitions are correlated; these results do not establish statistical significance or booking conversion.

An earlier integration run was invalidated after a redaction defect was found. Its stored identifiers were masked, and the baseline was rerun before any improvement was allowed. Its scores are excluded from this comparison.
