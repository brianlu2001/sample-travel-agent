# PR #3: operator-triggered SDK replay

On September 16, 2026, the operator requested a fresh Claude Agent SDK
investigation using previous recorded issues to redo PR #3. This was an explicit
replay, not a newly detected live threshold breach. No alert or email delivery is
claimed for the replay. The original baseline and historical evaluations remain
unchanged.

The existing `quality.remediation.propose` function started each native SDK
session. Codex invoked the existing constrained candidate validation, artifact
checks and targeted Phoenix experiment modules, then updated the existing GitHub
branch. This operator replay did not originate from an automatic repair queue job.
The supplied starting version included merged PR #4. The final agent change is
the exact SDK-returned flight instruction; all other deployed prompt instructions
and tools are preserved. Codex supplied evidence and review feedback, checked the
artifact and published it; Codex did not write the final replacement instruction.

## Actual investigations

| Investigation | Phoenix trace | SDK session | Recorded activity |
| --- | --- | --- | --- |
| First proposal | `3c51297a725f3d16c1548518405e046f` | `3d13d586-36e3-4d01-b21b-cd6b8dfd588c` | 20 SDK turns; 35 exported spans, including 9 LLM and 22 tool spans |
| Revision after response inspection | `858ef6eb552fab6b6bbe751a36e1b389` | `cd904e49-f28e-4e3b-bd10-8926afe1e608` | 20 SDK turns; 36 exported spans, including 10 LLM and 22 tool spans |

Both sessions actually invoked these four skills and read their required references:

- `phoenix-evals`: analyze the failures and evaluator disagreement.
- `phoenix-tracing`: inspect the original agent, model and tool evidence.
- `arize-prompt-optimization`: generate a focused prompt change.
- `arize-experiment`: read the previous candidate's real experiment evidence.

The tools successfully retrieved the scoped Phoenix traces and experiment
records. Premature operations were denied until required skill/evidence reads
completed. These denials are real trace events, not application outages.
Skill loading proves access to guidance, not correct reasoning.

## Validation and unresolved behavior

Both candidates received 15 actual development scenario executions, one each,
including the original incident reproduction. No full evaluation was run for
each revision. The existing daily/manual full-checkpoint policy remains in place.

| Candidate | Repair record | Targeted experiment |
| --- | --- | --- |
| First SDK proposal | `4a53b07cca0a40fd8754c8d52b832620` | `fa8d8871cf1f48c6a58641f71d07cca5` |
| Final SDK revision | `13861eda1fd04bc0bddcbb8cef4eaaab` | `b3f3b482600a4ca8918ed4031d558a63` |

The first proposal still said "Both options are available that day" in run
`780ba65d7a674196a69fee4531fe510c` (trace
`c62a99dcac7ab2c86b056430bbe50016`). Its judge incorrectly passed that response.
An explicitly identified **Codex assistant review, not human calibration**, was
passed back to the next SDK session. The SDK then acknowledged the contradiction
and proposed prohibiting availability assertions in headings and introductory
phrases, with the limitation preceding route data.

The final revision's incident replay, run `e2bad19bf3c7419289ff106995b042d9`
(trace `0531a6da9307e74bdab56fe099ace915`), removed the explicit affirmative
availability claim. However, it still introduced route examples "for" the
requested date and placed the limitation after the options. This is partial
behavioral evidence, not a verified complete resolution. Judge pass rates must
not hide this observation or be presented as human-calibrated accuracy.

The experiment IDs identify immutable measurements; PR #3 contains their final
scores. Small targeted samples are not comparable full-set improvement claims.
The final automated results were correctness 14/15, groundedness 9/9 applicable
(six not applicable), and topic relevance 15/15. No privacy or deterministic tool
contract failures were recorded. These scores do not resolve the response review
finding above. Full evaluation is pending the accepted daily/manual cadence.
The repair is a draft for human inspection and is not automatically merged or
deployed. Earlier failed proposals and assistant-authored intermediate attempts
are preserved separately; the final diff is the SDK revision.

## Useful next skills, not installed in this replay

- **arize-annotation:** route contradictory judgments to human review queues;
  preserve the distinction between human labels and agent-generated findings.
- **arize-dataset:** turn reviewed failures into versioned development examples
  while protecting the frozen baseline and held-out set.
- **arize-instrumentation-health:** diagnose missing, duplicate or disconnected
  spans before attributing missing evidence to agent behavior.
- **arize-prompts:** useful if prompt version management moves into Prompt Hub;
  Git remains the current source of deployed prompt versions.
- **arize-span-routing:** useful when the framework serves multiple agents or
  tenants with distinct observability projects.

These are recommendations for specific responsibilities. Their AX CLI commands
would need an explicit AX integration or equivalent scoped Phoenix tools; simply
bundling the documents does not implement those capabilities.
