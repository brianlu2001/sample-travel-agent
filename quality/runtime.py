"""Thin adapter around the unchanged agent; captures actual executions only."""
import contextvars
import copy
import json
import threading
import time
import uuid

from opentelemetry.trace import Status, StatusCode

from quality import store
from quality.config import ROOT, agent_version, file_hash, fixture_version
from quality.privacy import safe_payload
from quality.tracing import error_status, ids, set_io, setup

_capture = contextvars.ContextVar("quality_capture", default=None)
_patch_lock = threading.Lock()
_patched = False
_loaded_agent = None


def prepare_agent():
    """Freeze the identity of imported serving code until the process restarts."""
    global _loaded_agent
    with _patch_lock:
        if _loaded_agent is None:
            from agent import loop
            _loaded_agent = {"version": agent_version(), "model": loop.MODEL,
                             "files": {name: file_hash([ROOT / "agent" / name])
                                       for name in ("loop.py", "prompt.py", "tools.py", "config.py")}}
    return _loaded_agent


def _instrument_tools():
    global _patched
    with _patch_lock:
        if _patched:
            return
        from agent import loop
        original = loop.execute_tool

        def observed_tool(name, tool_input):
            capture = _capture.get()
            with setup().start_as_current_span(name, openinference_span_kind="tool",
                                               record_exception=False, set_status_on_exception=False) as span:
                span.set_attribute("tool.name", name)
                set_io(span, tool_input)
                started, result, failure = time.time(), None, None
                try:
                    result = original(name, tool_input)
                    set_io(span, tool_input, result)
                    span.set_status(Status(StatusCode.ERROR if isinstance(result, dict) and "error" in result else StatusCode.OK))
                    return result
                except Exception as error:
                    failure = type(error).__name__
                    error_status(span, error)
                    raise
                finally:
                    if capture is not None:
                        capture.append({"name": name, "arguments": tool_input, "result": result,
                                        "error_type": failure, "started": started, "finished": time.time(), **ids(span)})

        loop.execute_tool = observed_tool
        _patched = True


def run_observed(messages, *, source="live", conversation_id=None, benchmark_id=None, scenario_id=None, version_override=None):
    tracer = setup()
    loaded = prepare_agent()
    _instrument_tools()
    from agent.loop import run_agent
    from agent.config import MODEL

    run_id = uuid.uuid4().hex
    calls = []
    token = _capture.set(calls)
    start = time.time()
    safe_input = safe_payload(messages)
    event = {
        "id": run_id, "created": start, "source": source,
        "conversation_id": conversation_id or uuid.uuid4().hex,
        "benchmark_id": benchmark_id, "scenario_id": scenario_id,
        "version": version_override or loaded["version"], "fixture_version": fixture_version(), "model": MODEL,
        "input": safe_input, "status": "ok", "provenance": "actual_agent_execution",
    }
    reply = ""
    updated = None
    raised = None
    try:
        with tracer.start_as_current_span("travel.turn", openinference_span_kind="agent",
                                           record_exception=False, set_status_on_exception=False) as span:
            event.update(ids(span))
            span.set_attribute("session.id", event["conversation_id"])
            span.set_attribute("metadata", json.dumps({k: event[k] for k in ("source", "version", "fixture_version", "benchmark_id", "scenario_id")}))
            try:
                reply, updated = run_agent(copy.deepcopy(messages))
                span.set_status(Status(StatusCode.OK))
            except Exception as error:
                raised = error
                event["status"] = "error"
                event["error_type"] = type(error).__name__
                error_status(span, error)
            set_io(span, safe_input, reply)
    finally:
        _capture.reset(token)
    event.update({"output": safe_payload(reply), "tools": safe_payload(calls),
                  "finished": time.time(), "latency_ms": round((time.time() - start) * 1000, 2)})
    # Runtime content remains in memory for the user; only the sanitized copy persists.
    event["privacy_ok"] = all(not isinstance(value, dict) or "privacy_error" not in value
                              for value in (safe_input, event["output"], event["tools"]))
    store.save_run(event)
    if raised:
        raise RuntimeError(f"Agent request failed ({type(raised).__name__}); run {run_id}") from None
    return reply, updated, event
