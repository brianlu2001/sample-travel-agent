# Evaluator audit

The evaluation framework is `arize-phoenix-evals` (Phoenix `ClassificationEvaluator` and its provider abstraction). It handles structured judgments; OpenInference records actual judge calls. The four travel rubrics and independent fixture adapter are application-specific definitions, not claimed to be stock Arize metrics.

Phoenix ships generic Correctness, Faithfulness and Completeness evaluators. Their generic definitions do not directly encode this customer’s static-inventory limitations, allowed itinerary suggestions, travel-domain boundaries, masked content, or not-applicable/unknown labels. We retain Phoenix’s framework with explicit custom rubrics, consistent with the Phoenix evaluation skill.

## Fixes

- Judge visible conversation and the final response; internal tool payloads are not user instructions.
- Never inject a scenario’s future final goal into an earlier turn.
- Recompute independent evidence for earlier tool lookups used in follow-up answers.
- Include the fixture catalogue so a reversed flight is recognized as a misapplied record, rather than called an invented identifier.
- Treat itinerary drafts separately from final-answer day coverage.
- A booking refusal is incomplete for a booking request, even if it repeats useful flight details.
- Itinerary suggestions without material factual claims are consistently not applicable for groundedness.
- Check machine-verifiable day failures before calling the judge.

## Recorded verification

Evaluator `5864c929135a4732f9da305dffc7ab01742f791bce438c22fcdd5e05897c2969` passed 60/60 explicit regression comparisons on 17 preserved, real agent responses. [Native Phoenix audit experiment](http://127.0.0.1:6006/datasets/RGF0YXNldDo0/compare?experimentId=RXhwZXJpbWVudDo5).

The expected labels were authored during development; they are not human calibration or a statistical estimate of judge accuracy. The audit covers the reported Tokyo issue, one/two/three/four-day plans, changing requirements, privacy masks, wrong flight directions, invalid hotel stays, weather errors, booking refusal, and unrelated requests. No fabricated agent responses or traces were used.

PR #1 was closed, unmerged, at the user’s request because it used the previous evaluator. Historical benchmarks remain immutable. A new compatible baseline must be measured before the next candidate proposal.

Requirements: assessment PDF pages 1–3 and the customer discovery notes prioritize the feedback loop, measurable quality, real telemetry, PII redaction and human review. Production should add representative human labels and repeat this audit whenever the evaluator changes.

References: [Phoenix evaluators](https://github.com/Arize-ai/phoenix/blob/main/packages/phoenix-evals/README.md), [Phoenix evaluation skill](../.agents/skills/phoenix-evals/SKILL.md).
