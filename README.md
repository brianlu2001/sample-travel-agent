# Travel Agent Quality Lab

A working travel-agent demo with Phoenix tracing, evaluations, a live dashboard, alerts, and an evidence-driven repair workflow. The original agent stays active; improvements run separately and become a draft PR for human review.

## Open the demo

- Chat: http://127.0.0.1:8000/
- Quality dashboard: http://127.0.0.1:8000/dashboard
- Phoenix: http://127.0.0.1:6006/
- API docs: http://127.0.0.1:8000/docs

The agent has four tools: flight lookup, hotel lookup, fixture weather, and itinerary assembly. Its static data cannot establish live availability or bookings.

## Setup

Use Python 3.13 and uv. Phoenix has its own environment to preserve the assessed agent's Anthropic SDK.

```powershell
uv sync --locked --extra dev --python 3.13
uv venv .phoenix-venv --python 3.13
uv pip sync --python .phoenix-venv/Scripts/python.exe requirements-phoenix.txt
```

Copy `.env.example` to `.env` only if it does not already exist. Set `ANTHROPIC_API_KEY=your_key` on one line. Both `.env` and `.env.txt` are ignored by Git; only `.env` is loaded. The spaCy redaction model is installed with the dependencies and runs locally.

```powershell
.\.venv\Scripts\python.exe -m scripts.demo start
```

Services run in the background on loopback. On Linux/macOS use the corresponding `bin/python` paths. The complete demo was exercised on Windows; CI defines isolated checks on Linux.

## Run the workflow

### 1. Enable the live feedback loop

```powershell
.\.venv\Scripts\python.exe -m scripts.demo enable-repair
```

Use the chat UI normally. Each completed turn produces a real OpenInference trace;
Phoenix evaluators assess its final answer asynchronously. The dashboard and
monitor read scores from **Phoenix span annotations**. The dashboard refreshes every
five seconds; monitoring runs on acknowledged evaluation events, without a quality-poll timer.
Workers prioritize live judgments over queued offline benchmark evaluations.

The live window holds the latest **20 conversations within 24 hours**, separated by
agent and evaluator version. After **10 applicable judgments** for any of the three primary
metrics, a rate **below 85%** opens a deduplicated incident and sends email to the
local SMTP inbox. Recovery requires **95%**. Unknown/pending/not-applicable results
never count as passes. Only conversations submitted through the chat API enter this
window; offline benchmarks are excluded. Operator-generated test messages sent to
that same API also exercise the live pipeline.

### 2. A live flag starts the repair workflow

1. Preserve the flagged real conversations and trace IDs as diagnosis evidence.
2. Measure a current-agent reference baseline if a compatible one is unavailable.
3. Propose a minimal fix from the sanitized **live failures**.
4. Validate the proposed functions and run up to 15 targeted development scenarios
   in Phoenix, including incident reproductions. Held-out cases stay out of diagnosis.
5. Open/update a draft PR after hard checks pass. Full evaluation follows the daily
   checkpoint policy or a manual request. Human review controls merge and deployment.

The 60 reference scenarios are regression tests, **not the live monitoring feed**.
Full benchmarks run for the initial baseline, scheduled checkpoints and explicit
operator requests. They make real provider calls.
A frozen baseline always precedes patch generation. One active investigation per
metric groups repeated flags until its PR is reviewed. Two repair workers can
investigate different metrics concurrently, sharing a compatible measured baseline.
Each produces separate artifacts and a draft PR. Closing or merging a PR releases
its metric lock after GitHub state is confirmed.

A benchmark manifest records source, fixture, dataset, redactor and evaluator
hashes, models, repetitions and Phoenix experiment IDs. Its original evidence is
sealed; changing a judge does not rewrite history.

The repair model cannot edit evaluators, thresholds, reference data, dependencies,
or credentials. GitHub credentials use the existing credential helper or
`GITHUB_TOKEN`/`GH_TOKEN`. Failed publication preserves the patch locally.

### 3. Inspect and calibrate

The dashboard opens on **Live**, with **Benchmarks** as the other metric view.
Trace history defaults to live conversations. Inspect a request for its final answer,
judgments, preserved prior judgments and separate tool diagnostics. Human labels calibrate judges;
they do not silently rewrite benchmark results.

**Inspect → Trace call tree** reads the real Phoenix spans. Expand an agent, LLM,
or tool call to see its redacted inputs/outputs, parent span, duration, execution
status, model and recorded token counts. A tool exception retains the input and
error type without exporting its private exception message. Execution `OK` means
the call completed; quality failures remain in evaluations/tool diagnostics.

**Recent version updates** shows the loaded agent fingerprint, evaluator revisions,
and proposed candidate changes with PR links and review status. Times are observed
execution/startup times, not invented deployment dates. Unchanged restarts do not
create releases. Candidate experiments never mark a patch as deployed. New serving
code is recorded when the API loads it; editing files alone displays a restart notice.

**Email delivery evidence** shows actual SMTP inbox receipts for
`travel-agent-dev@example.com`: sender, recipient, received time, Message-ID and
the sender's recorded SMTP response. A `250` acceptance plus a matching inbox row
proves local delivery. It does not prove delivery to an external team mailbox.
Older receipts remain visible without inventing missing sender responses.
Configure `QUALITY_SMTP_*` for an external relay; relay acceptance alone is not
proof of final external inbox delivery.

### Exercise the live dashboard

```powershell
$conversationId = [guid]::NewGuid().ToString()
$body = @{
    conversation_id = $conversationId
    message = 'Compare New York to Los Angeles flights for October 3, 2026. Include flight numbers and prices.'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/chat -ContentType application/json -Body $body
```

Use the chat UI or `POST /chat` for demonstrations. Reuse the conversation ID for
follow-up messages; create a new UUID for each new conversation. Each request runs
the actual agent, tools, tracing, evaluation and rolling-window monitoring pipeline.
Wait for evaluations to finish when checking metrics. Include factual lookup
questions to exercise groundedness; responses without checkable claims can be
not applicable and do not satisfy its 10-sample minimum.

There is no separate demo-scenario dashboard view. Earlier development-stream
records retain their original provenance and remain accessible through **All
executions** in trace history, labeled **Archived simulation**. They are not
retroactively counted as live chats. Reference benchmark traffic also stays separate.

### Hosted tracing

`ARIZE_SPACE_ID`, `ARIZE_API_KEY`, and `ARIZE_OTLP_ENDPOINT` in the ignored `.env`
enable a redacted OTLP mirror to Arize AX. `/v1` is normalized to the HTTP
`/v1/traces` endpoint. The key is never included in traces or job payloads.
Phoenix remains the OSS workbench for evaluations, annotations and experiments;
Arize AX is the authenticated hosted tracing destination. Their credentials are
not interchangeable. Local Phoenix does not require a key with authentication
disabled. The dashboard reports actual collector delivery and readback status.

### Operations

```powershell
.\.venv\Scripts\python.exe -m scripts.demo status
.\.venv\Scripts\python.exe -m scripts.demo retry-failed
.\.venv\Scripts\python.exe -m scripts.demo resume-repair
.\.venv\Scripts\python.exe -m scripts.demo revise-repair
.\.venv\Scripts\python.exe -m scripts.demo sync-prs
.\.venv\Scripts\python.exe -m scripts.report
.\.venv\Scripts\python.exe -m scripts.demo stop
```

Retries reuse validated candidates and completed cases. A hard-check rejection is
terminal for that candidate. `revise-repair` starts a distinct development revision.
Full checkpoint regressions can trigger further revisions, capped at three proposals
per chain. It does not resample an unchanged candidate until it passes. Model calls
and SMTP are at-least-once operations. Leases and idempotency keys reduce duplicates;
the local inbox deduplicates Message-ID.

The launcher starts a controller/evaluator process and two independent repair
processes. The Kubernetes profile uses two repair pods and PostgreSQL push wakeups.
To automate PR lifecycle updates outside loopback, configure a signed GitHub
`pull_request` webhook at `/quality/github/webhook` and `QUALITY_GITHUB_WEBHOOK_SECRET`.
The local `sync-prs` command confirms actual GitHub state without exposing this app.

## Evaluation policy

| Metric | What it measures |
|---|---|
| Correctness | Current trip constraints, direction, dates, duration, budget, multi-turn changes |
| Groundedness | Factual support from independent fixture evidence and known data limitations |
| Topic relevance | Travel-domain behavior, evaluated separately from factual accuracy |
| Completion diagnostic | Retained in trace details; excluded from new alerts and checkpoint decisions |

Code checks diagnose tool failures. Phoenix judges evaluate the final answer, so a handled tool error need not fail the response. Unknown and not-applicable labels remain visible and are excluded from pass-rate denominators.

Alerts use the live-window policy above. Benchmark and validation traffic cannot open live quality incidents. Agent and evaluator versions stay separate. Redaction failure withholds content and queues an immediate incident. Threshold breaches are operational signals, not statistical proof of population drift.

Reference scenarios and judges await human calibration. Repeated executions are correlated; confidence intervals do not account for judge error.

## Privacy and execution boundaries

Local redaction precedes feedback persistence and telemetry export. Covered entities include names, emails, phones, street addresses, passport-like IDs, payment identifiers, and common secret formats. Provider exceptions retain their type instead of raw messages. Tests check detection and preservation of travel constraints.

Raw chat context remains in process memory; conversations have idle expiry and capacity limits. The original agent's provider request still contains what the user submits. Redaction protects the feedback loop and judge/repair context.

Local recognizers can miss unusual personal data or over-redact. Production needs broader adversarial coverage, reviewed policy, authentication, encryption, and retention enforcement. Candidate syntax restrictions and subprocess timeouts are a local demo boundary, not a production VM sandbox. Keep the unauthenticated demo on loopback.

## Checks and documentation

### Optional Kubernetes deployment

The local demo remains unchanged. A container, shared PostgreSQL journal, separate
evaluation workers/controller, and optional KEDA queue autoscaling are now provided.
See [deployment setup and enterprise capacity limits](docs/deployment.md). A cluster
is not required to use this demo; no million-request throughput is claimed.

```powershell
uv run pytest -q
uv run ruff check agent quality tests scripts
uv pip check
```

Unit tests use temporary databases and do not export test fixtures as telemetry. CI needs no LLM credentials; paid benchmarks run on a qualifying live incident or explicit operator request.

- [Architecture and workflow](docs/architecture.md)
- [Historical measured results](docs/demo-results.md)
- [Metric definitions](docs/metrics.md)
- [Assessment requirements and skills usage](docs/requirements.md)

Core infrastructure lives in `quality/`. Travel policy and independent references live in `quality/profiles/travel.py`. Another agent needs its capture adapter, evidence source, rubrics, and scenarios; storage, workers, monitoring, and experiment gates can be reused.

## Phoenix tooling

The official [tracing skill](.agents/skills/phoenix-tracing/SKILL.md), [evaluation skill](.agents/skills/phoenix-evals/SKILL.md), and [CLI skill](.agents/skills/phoenix-cli/SKILL.md) informed the implementation. Relevant references are retained locally. OpenInference supplies AI span semantics and instrumentation using OpenTelemetry SDK/OTLP underneath.

The Phoenix CLI was used to inspect actual projects and traces:

```powershell
$env:PHOENIX_ENDPOINT='http://127.0.0.1:6006'
$env:PHOENIX_PROJECT='travel-agent'
npx --yes @arizeai/phoenix-cli@1.18.2 trace list --limit 2 --format raw --no-progress
```

Phoenix stores native datasets, experiments, runs, and annotations. The demo dashboard adds incident, repair, and delivered-email views.

### Validate the evaluator before enabling automated proposals

With Phoenix and the worker running, execute:

```powershell
.venv\Scripts\python.exe -m quality.audit_evaluator
.venv\Scripts\python.exe -m scripts.demo enable-repair
```

The audit runs the actual judge against 17 preserved, redacted real responses in
`datasets/evaluator-recorded-responses.json`, with 60 developer-authored expected
labels. It creates real evaluation traces and a Phoenix experiment. Archived
responses are evidence inputs; they are never inserted as new live conversations
or replayed as new agent telemetry. A passed audit for the active evaluator is
required before automatic proposals. Customer human calibration remains pending.

### Resume after a provider credit interruption

Restore Anthropic credits first, then run:

```powershell
.venv\Scripts\python.exe -m scripts.demo resume-provider
```

This resumes the recorded blocked campaign, retries its unfinished evaluation jobs
and incident workflow, and retains successful cases, judgments and original failures.
It does not rerun rejected candidates to seek better scores. Unfinished multi-turn
cases can require a new real conversation; every attempt remains distinguishable.

## Verified current demo

See [verified end-to-end results](docs/demo-verified.md) for the audited evaluator,
measured baseline and revision, rejected candidate, real SMTP receipt and draft PR #2.

### Manual full evaluation and saved baselines

Open **Dashboard → Benchmarks → Run full evaluation** to queue a full run for the
selected target. The target version is displayed beside the button;
unmerged PR code must be selected explicitly. Completed runs have immutable
version records available through **Saved version**, including historical baselines.
New manual checkpoints compare with the original compatible baseline and preserve it.

Select the running agent or a registered candidate in the **Evaluate** selector.
The saved-experiment selector is for viewing history, independently of the run target.

See [evaluation cadence](docs/evaluation-cadence.md) for the active policy. Candidate
revisions get targeted checks; full checkpoints run after 24 hours AND an unmeasured
version change. To enable this schedule on a fresh installation:

```powershell
.venv\Scripts\python.exe -m scripts.demo enable-checkpoints
```

Keep `scripts.demo start` running for the local worker to execute scheduled work.
Full checkpoints require human approval before release. The dashboard records this
approval; it never merges or deploys agent code. PR #2 retains its original measured
results and historical acceptance policy.
