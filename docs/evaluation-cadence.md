# Active evaluation cadence

## Revision checks and daily checkpoints

| Trigger | Evaluation | Decision |
| --- | --- | --- |
| Each candidate revision | Independent tool invariants, artifact tests, up to 15 development scenarios including up to 3 incident reproductions; one execution each | Hard contract/privacy failures block. Semantic scores guide development. A draft PR may open with full evaluation pending. |
| 24 hours since the last compatible full result AND an unmeasured version | Full 60-scenario set, two executions each, on the latest registered candidate or running agent | Original baseline and last human-approved checkpoint comparisons; human review before release. |
| Run full evaluation button | Full evaluation of the selected target without waiting for the schedule | A separate immutable measurement; no automatic deployment. |
| Every conversation | Existing Phoenix evaluations and 20-conversation / 85% monitoring | Deduplicated live incident, SMTP alert and evidence-driven proposal. |

The existing worker checks checkpoint eligibility every 30 seconds. Unchanged
versions do not rerun daily. Targeted checks do not reset the full-checkpoint clock.
A queued candidate superseded before execution is skipped. Once execution begins,
the frozen candidate completes; a newer revision remains eligible for a later
checkpoint. The worker serializes repairs and full experiments. Failed jobs require
operator attention rather than automatically consuming another full run.

Enable the schedule on a fresh installation with:

```powershell
.venv\Scripts\python.exe -m scripts.demo enable-checkpoints
```

The local demo services must remain running. SQLite retains schedule state across
restarts. This is application orchestration in the existing worker, not a Codex
reminder or a separate scheduling service.

## Acceptance and uncertainty

There is no required five-point improvement on every revision. Full checkpoints
compare cumulative results against both the original compatible baseline and the
last explicitly human-approved checkpoint. With no human-approved checkpoint,
both comparisons use the original baseline.

Each metric records pooled rates, every observed decline, changes in applicability,
and paired scenario-level changes. Two generations of one scenario remain one
cluster. The current policy uses 4,000 deterministic bootstrap resamples, at least
10 fully evaluable paired scenarios, and a two-percentage-point practical margin.
The interval tails use 0.05 / 16 to conservatively account for four metrics, two
cohorts (overall and held out), and two comparators. Intervals describe this small
benchmark; they do not establish judge accuracy or population-level guarantees.

A metric is regressed when its entire interval is below -2 points. It is improved
when the interval is above +2 points with unchanged applicability. It is stable
when the interval excludes a decline worse than -2 points. Other cases are
inconclusive. Unknown/pending evaluations and deterministic privacy/tool failures
block approval. A checkpoint is improved only when its aggregate comparisons
support improvement without unresolved metric uncertainty. Small held-out samples
will often make the overall conclusion inconclusive, requiring human judgment.

Every confirmed metric regression opens a deduplicated incident and actual SMTP
alert. The workflow proposes a follow-up using development failures only. A chain
allows at most three proposals; lack of usable development evidence or exhaustion
of this limit leaves the issue for human review. Inconclusive changes do not
trigger endless retesting or a new automatic proposal. No held-out case contents
or scores are supplied to the repair model.

## PRs, approvals and versions

Revisions preserve earlier function fixes and update the same generated PR branch.
External edits to that branch cause the workflow to stop rather than overwrite them.
New PR heads receive a pending full-checkpoint status. Full results bind to the
recorded PR head; a changed head invalidates approval. A successful evaluation
still requires human review. Inconclusive approval requires an explicit uncertainty
acknowledgment and review note. The dashboard records approval and updates the GitHub
status where configured; it never merges or deploys code. Branch protection is not
configured by this demo; enforce the status as a required check in production.

Dashboard: **Benchmarks → Evaluate → Run full evaluation**. The target selector is
independent of the saved-experiment history selector. **Review checkpoint** appears
for results measured under this policy. **Saved version** exposes immutable results,
manifest, evidence hash and Phoenix identifiers. Historical results and PR #2 retain
their original acceptance policy. They are not retroactively reclassified.

Acceptance-rule changes do not require rerunning an otherwise compatible baseline.
Original scores remain immutable; new decisions are recorded separately with a policy
version. `.quality/` and Phoenix need backups; immutable records do not prevent disk loss.

## Phoenix and verification

This follows the repository's Phoenix Evals skill: invariants gate, signals trend.
Phoenix stores real experiments, traces and metric annotations; our worker defines
the customer-specific cadence and approval rules. See [Phoenix experiments](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments).

Repeated adaptive holdout reuse can still overfit. Limited checkpoint use helps
manage exposure; fresh, reviewed cases and customer calibration remain future work.
See [Dwork et al.](https://arxiv.org/abs/1506.02629).

89 tests pass, including time/version eligibility, restart deduplication, superseded
requests, paired scenario counts, uncertain declines, regression-to-email/repair
routing, proposal limits, targeted-only revisions, and human approval. Unit fixtures
use isolated temporary storage and never export telemetry. The existing real PR #2
benchmark remains the prior end-to-end proof; the new daily timer is verified with
a controlled test clock rather than backdating real events or waiting a day.
