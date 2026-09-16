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
  No proposal may start until that measurement finishes. Earlier sealed baselines
  and rejected proposals remain intact; the serving agent was not modified.

## Current pause and resume

Anthropic returned HTTP 400 with an insufficient-credit-balance message. Paid
work is paused. The baseline remains incomplete, and both repair requests remain
`waiting_baseline`. Some current judgments are unknown because provider calls
failed; they are not counted as passes. No new PR is claimed for these requests.

After credits are restored:

```powershell
.\.venv\Scripts\python.exe -m scripts.demo resume-provider
```

This resumes the existing baseline and unfinished judgments, reusing completed
cases and scores. Baseline completion wakes both reserved repairs. A draft PR
requires independent checks and its targeted experiment; human review controls
merging and deployment. `sync-prs` confirms actual GitHub closures locally, or a
signed webhook can perform that confirmation in a deployed environment.

See [architecture](architecture.md) and [deployment](deployment.md) for the workflow
and remaining enterprise deployment limits.
