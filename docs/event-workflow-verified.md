# Event-driven repair verification — September 16, 2026

## Implemented

Phoenix annotation acknowledgment emits a durable monitoring event containing
the observed 20-conversation window. A metric breach reserves one repair request;
the database holds that metric through investigation and PR review. PostgreSQL
push notifications wake workers, with leases and retry recovery in the durable
queue. The dashboard's five-second refresh is separate from monitoring.

Two local repair processes are running. Kubernetes manifests define two repair
pods, with PostgreSQL coordination and a shared ReadWriteMany artifact volume.
No Kubernetes cluster was deployed. Concurrency, duplicate deliveries, review
locks, shared baseline completion, failure recovery and PostgreSQL notifications
were tested. Different metric workers can execute simultaneously; their artifacts
and PR branches remain separate.

## Real evidence

- The weather evaluator was corrected to use date-adjusted independent evidence.
  Hotel names hidden by redaction now produce inconclusive identity diagnostics,
  not invented-hotel failures. Impossible stays and unsupported prices still fail.
- Current evaluator `1c8682c64238e90b9f7be7840895b12548bb76fa565dd4a931a68fc5e37c8ff7`
  passed **60 developer-authored regression expectations on 17 actual recorded
  responses**. This is a regression audit; human calibration remains pending.
  [Phoenix audit experiment](http://127.0.0.1:6006/datasets/RGF0YXNldDo0/compare?experimentId=RXhwZXJpbWVudDoxNw==).
- Twenty additional conversations went through ordinary `POST /chat`, producing
  actual answers, tool calls, traces and evaluations in Live. Local request/result
  evidence is in `.quality/event_live_exercise/`; no agent outputs were fabricated.
- During reassessment, correctness fell below 85% with at least 10 applicable
  results. Event processing opened incident `31c404957ee14a1a83a01a9a22c76040`.
  Its recorded below-threshold measurement reached 11/13 (84.6%); later results
  recovered above the warning threshold without cancelling its investigation.
- The real local SMTP receipt is Message-ID
  `<0f7ba220cded55848da7e90c0fbd8ad0@quality.local>`, response **250 Message accepted
  for local delivery**. This proves delivery to the demo inbox, not an external mailbox.
- Correctness request `8b36207d9f095d3fb6a2e659fad8322f` came from that event.
  Groundedness request `0647a28204df513281654e14cd60f979` is an explicit requested
  review of actual failed response `8886e23f6f5441b7a7e6630301311eeb`. It is labeled
  separately and does not claim a groundedness threshold breach.
- Both requests reference the same baseline `c074fc95e62f5bb6bc9caa084237ed8b`.
  It completed before either proposal started. Earlier sealed baselines and
  rejected proposals remain intact; the serving agent was not modified.

## Completed after credits were restored

The preserved baseline resumed successfully: 60 reference scenarios, two actual
executions each. Its immutable results are correctness **114/120 (95%)**,
groundedness **68/70 (97.1%)**, and topic relevance **120/120 (100%)**, with no
unknown primary judgments. Groundedness excludes inapplicable responses. Two
deterministic tool-contract failures remain in the original agent. Historical
provider-error attempts are retained separately from the completed case results.

The baseline completion event woke both metric repair processes concurrently.
Each proposed and checked its own candidate against 15 targeted development
cases, including the actual incident prompts. Both passed independent tool
invariants and artifact tests, with no tool-contract or privacy failures in their
targeted experiments.

| Draft proposal | Targeted correctness | Targeted groundedness | Targeted relevance |
|---|---:|---:|---:|
| [Groundedness PR #3](https://github.com/brianlu2001/sample-travel-agent/pull/3) | 14/15 | 9/9 | 15/15 |
| [Correctness PR #4](https://github.com/brianlu2001/sample-travel-agent/pull/4) | 15/15 | 8/8 | 15/15 |

These are recorded LLM judgments on different development selections, **not
full-set improvement claims**. Both requests are `awaiting_review`; their
per-metric locks remain held. The two prompt proposals overlap and need human
reconciliation and validation before being combined. Full candidate checkpoints
remain subject to the daily cadence or manual full-evaluation button.

### Review exposed a judge disagreement

Both candidate flight answers still assert date-specific availability and then
disclaim it. The LLM judge passed these answers. A separate Codex assistant review
recorded this contradiction prominently in both draft PR descriptions and repair
details without overwriting the original scores. This is **not human calibration**.
Neither proposal should be approved as a verified fix on these scores alone.

The evidence is candidate run `ca8b2a25f1f74507900ce9775f5d9d3c` for groundedness
and `49c025ccbd164620a11edcc0f9658b24` for correctness. Local evidence is saved in
`.quality/event_live_exercise/candidate-review-findings.json`. The next quality
step is to calibrate the evaluator on this contradiction and revise the proposals;
the serving agent is unchanged.

CI also caught an unused date import in generated artifacts. The renderer now
distinguishes a local date parameter from the global datetime helper. Both PR
artifacts were corrected with unchanged candidate prompts/function bodies, then
artifact tests and lint reran. This packaging correction did not rerun or replace
their actual targeted results.

## Operational state

The live 20-conversation window is fully judged: correctness **18/20 (90%)**,
groundedness **16/17 (94.1%)**, and relevance **20/20 (100%)**. Actual local SMTP
delivery is recorded above. Normal API, Phoenix, evaluation, and two repair worker
processes remain available; temporary extra evaluation capacity was stopped after
the backlog cleared. No agent changes were merged or deployed.

Phoenix CLI fetched both original incident traces, including the groundedness
trace's agent, model and flight-tool spans. Concurrent repair leases were saved in
`.quality/event_live_exercise/parallel-repairs-verified.json`.

For another credit interruption, `python -m scripts.demo resume-provider` reuses
the preserved baseline and unfinished judgments. `sync-prs` confirms actual GitHub
closures locally, or a signed webhook performs that confirmation after deployment.

See [architecture](architecture.md) and [deployment](deployment.md) for the workflow
and remaining enterprise deployment limits.
