import uuid
import asyncio
import json
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from starlette.middleware.trustedhost import TrustedHostMiddleware

from quality.runtime import run_observed
from quality import store
from quality.privacy import safe_payload

store.init()

def expire_conversations():
    with LOCK_GUARD:
        for key in [key for key, at in LAST_ACTIVE.items() if time.time()-at > 3600 and not LOCKS[key].locked()]:
            CONVERSATIONS.pop(key, None)
            LOCKS.pop(key, None)
            LAST_ACTIVE.pop(key, None)


@asynccontextmanager
async def lifespan(app):
    from quality.runtime import prepare_agent
    from quality.versions import register_running_agent, archive_benchmarks
    archive_benchmarks()
    register_running_agent(prepare_agent())
    async def cleanup():
        while True:
            await asyncio.sleep(60)
            expire_conversations()
    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="Travel Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

CONVERSATIONS: dict[str, list] = {}
LOCKS: dict[str, threading.Lock] = {}
LOCK_GUARD = threading.Lock()
LAST_ACTIVE: dict[str, float] = {}


@app.middleware("http")
async def local_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in ("http://127.0.0.1:8000", "http://localhost:8000"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Untrusted origin"}, status_code=403)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str
    run_id: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(422, "Message cannot be blank")
    conversation_id = req.conversation_id or str(uuid.uuid4())
    try:
        uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(422, "Invalid conversation ID") from None
    expire_conversations()
    with LOCK_GUARD:
        if conversation_id not in LOCKS and len(LOCKS) >= 500:
            raise HTTPException(429, "Demo conversation capacity reached; try again later.")
        lock = LOCKS.setdefault(conversation_id, threading.Lock())
        LAST_ACTIVE[conversation_id] = time.time()
    with lock:
        if len(CONVERSATIONS.get(conversation_id, [])) >= 150:
            raise HTTPException(422, "Please start a new chat; this conversation reached the demo limit.")
        messages = [*CONVERSATIONS.get(conversation_id, []), {"role": "user", "content": req.message}]
        try:
            reply, messages, event = run_observed(messages, conversation_id=conversation_id)
        except RuntimeError:
            raise HTTPException(502, "Agent request failed. Check the quality dashboard for the recorded error.") from None
        CONVERSATIONS[conversation_id] = messages
    return ChatResponse(reply=reply, conversation_id=conversation_id, run_id=event["id"])


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


@app.get("/quality/state", include_in_schema=False)
def quality_state():
    from quality.dashboard import state
    return state()


class FullEvaluationRequest(BaseModel):
    request_id: uuid.UUID
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_id: str = Field(default="running", max_length=100)


@app.post("/quality/benchmarks", status_code=202, include_in_schema=False)
def start_full_evaluation(req: FullEvaluationRequest):
    from quality.full_evaluation import EvaluationUnavailable, request
    try:
        return request(req.request_id, req.expected_version, target_id=req.target_id)
    except EvaluationUnavailable as error:
        raise HTTPException(409, str(error)) from None


@app.get("/quality/benchmarks/{benchmark_id}/version", include_in_schema=False)
def benchmark_version(benchmark_id: str):
    found = store.rows("SELECT record FROM benchmark_versions WHERE benchmark_id=?", (benchmark_id,))
    if not found:
        raise HTTPException(404, "No sealed evaluation version exists for this experiment")
    return json.loads(found[0]["record"])


class CheckpointApproval(BaseModel):
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(default="", max_length=2000)
    accept_uncertainty: bool = False


@app.post("/quality/benchmarks/{benchmark_id}/approve", include_in_schema=False)
def approve_checkpoint(benchmark_id: str, req: CheckpointApproval):
    from quality.checkpoints import approve
    try:
        return approve(benchmark_id, req.expected_version, req.note, req.accept_uncertainty)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.post("/quality/scenarios/stop", include_in_schema=False)
def stop_scenarios():
    from quality.scenario_stream import stop
    return stop()


@app.get("/quality/runs/{run_id}", include_in_schema=False)
def run_detail(run_id: str):
    row = store.get_run(run_id)
    if row is None:
        raise HTTPException(404, "Run not found")
    from quality.trace_view import trace_details
    row["trace"] = trace_details(row["event"]["trace_id"])
    return row


@app.get("/quality/runs", include_in_schema=False)
def history(source: Literal["online", "live", "scenario", "benchmark", "validation", "all"] = "online",
            benchmark_id: str | None = None, cursor: str | None = None,
            metric: Literal["correctness", "groundedness", "topic_relevance", "task_completion"] | None = None,
            label: Literal["pass", "fail", "not_applicable", "unknown", "pending"] | None = None,
            evaluator: Literal["all", "current"] = "all", conversation_id: str | None = None,
            limit: int = Query(default=20, ge=1, le=100)):
    from quality.dashboard import run_history
    try:
        return run_history(source, benchmark_id, cursor, limit, metric, label, evaluator, conversation_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


class Feedback(BaseModel):
    correctness: Literal["pass", "fail", "not_applicable", "unknown"]
    groundedness: Literal["pass", "fail", "not_applicable", "unknown"]
    topic_relevance: Literal["pass", "fail", "not_applicable", "unknown"]
    task_completion: Literal["pass", "fail", "not_applicable", "unknown"] = "unknown"
    note: str = Field(default="", max_length=2000)


@app.post("/quality/runs/{run_id}/feedback", include_in_schema=False)
def feedback(run_id: str, feedback: Feedback):
    row = store.get_run(run_id)
    if not row:
        raise HTTPException(404, "Run not found")
    labels = feedback.model_dump(exclude={"note"})
    note = safe_payload(feedback.note)
    store.execute("INSERT INTO feedback VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET created=excluded.created,labels=excluded.labels,note=excluded.note",
                  (run_id, time.time(), json.dumps(labels), str(note)))
    store.enqueue("annotations", f"human:{run_id}:{time.time()}", {"annotations": [
        {"span_id": row["event"]["span_id"], "name": "human_"+name, "annotator_kind": "HUMAN",
         "result": {"label": label, "score": 1 if label == "pass" else 0 if label == "fail" else None, "explanation": str(note)}}
        for name, label in labels.items()]})
    return {"saved": True}
