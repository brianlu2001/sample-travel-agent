"""Restartable local workers. Task failures retry with bounded backoff."""
import argparse
import base64
import concurrent.futures
import importlib
import json
import time

import httpx
from phoenix.client import Client

from quality import store
from quality.config import PHOENIX

EXPORT_HTTP = httpx.Client(timeout=15)
PHOENIX_CLIENT = Client(base_url=PHOENIX)


def dispatch(job):
    payload = job["payload"]
    if job["kind"] == "trace":
        response = EXPORT_HTTP.post(PHOENIX + "/v1/traces", content=base64.b64decode(payload["otlp"]),
                              headers={"Content-Type": "application/x-protobuf"}, timeout=15)
        response.raise_for_status()
    elif job["kind"] == "arize_trace":
        from quality.arize_export import export
        export(payload, EXPORT_HTTP)
    elif job["kind"] == "annotations":
        pending_exports = store.rows("SELECT COUNT(*) n FROM jobs WHERE kind='trace' AND state IN ('pending','running') AND key LIKE ?",
                                     ("%:"+payload["annotations"][0]["span_id"],))
        if pending_exports[0]["n"]:
            raise TraceExportPending()
        PHOENIX_CLIENT.spans.log_span_annotations(span_annotations=payload["annotations"], sync=True)
    elif job["kind"] == "evaluate":
        from quality.evaluation import evaluate_run
        evaluate_run(payload["run_id"])
    elif job["kind"] == "scenario":
        from quality.scenario_stream import execute
        execute(payload)
    elif job["kind"] == "monitor":
        from quality.monitoring import monitor
        monitor(**payload)
    elif job["kind"] == "email":
        from quality.mailbox import send_alert
        send_alert(payload, job["key"])
    elif job["kind"] == "repair":
        from quality.remediation import repair, repair_live_incident
        if payload.get("live"):
            repair_live_incident(payload["incident_id"])
        else:
            repair(**payload)
    elif job["kind"] == "full_evaluation":
        from quality.full_evaluation import execute
        execute(payload)


class TraceExportPending(Exception):
    pass


def consume(kinds):
    last_monitor = 0
    last_checkpoint = 0
    while True:
        provider_paused = bool(store.setting("provider_block"))
        if "full_evaluation" in kinds and not provider_paused and time.time()-last_checkpoint >= 30:
            last_checkpoint = time.time()
            try:
                from quality.checkpoints import tick
                tick()
            except Exception as error:
                store.set_setting("checkpoint_scheduler_error", {"at": time.time(), "error_type": type(error).__name__})
        if "scenario" in kinds and not provider_paused:
            try:
                from quality.scenario_stream import tick
                tick()
            except Exception as error:
                store.set_setting("scenario_runner_error", {"at": time.time(), "error_type": type(error).__name__})
                time.sleep(3)
        if "monitor" in kinds and store.setting("monitor_enabled", True) and time.time()-last_monitor >= 5:
            last_monitor = time.time()
            try:
                from quality.config import agent_version
                from quality.monitoring import monitor
                version = store.setting("serving_agent", {}).get("version", agent_version())
                monitor("live", version)
                if store.setting("scenario_campaign"):
                    monitor("scenario", version)
            except Exception as error:
                store.set_setting("live_monitor", {"status": "unavailable", "at": time.time(), "error_type": type(error).__name__})
        available_kinds = tuple(k for k in kinds if not provider_paused or k not in ("evaluate", "scenario", "repair", "full_evaluation"))
        if not available_kinds:
            time.sleep(1)
            continue
        job = store.claim(available_kinds, lease_seconds=7200 if "repair" in available_kinds else 600)
        if job is None:
            time.sleep(0.3)
            continue
        try:
            dispatch(job)
            store.finish(job)
        except TraceExportPending:
            store.execute("UPDATE jobs SET state='pending',available=?,attempts=attempts-1,lease_until=NULL WHERE id=? AND owner=?",
                          (time.time()+5, job["id"], job["owner"]))
        except Exception as error:
            if isinstance(error, ImportError):
                from quality.privacy import safe_payload
                store.set_setting("worker_import_error", {"at": time.time(), "kind": job["kind"],
                    "detail": safe_payload(str(error))})
            store.fail(job, error, max_attempts=1 if job["kind"] in ("repair", "scenario", "full_evaluation") else 5)
            print(json.dumps({"job": job["id"], "kind": job["kind"], "error_type": type(error).__name__, "attempt": job["attempts"]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluators", type=int, default=3)
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    store.init()
    # Load shared SDK integrations before worker threads request each other’s
    # partially initialized modules during a cold start.
    for module in ("quality.evaluation", "quality.remediation", "quality.scenario_stream", "quality.checkpoints"):
        importlib.import_module(module)
    from quality.privacy import analyzer
    analyzer()  # Fail before consuming jobs if the redaction model is unavailable.
    lanes = ([] if args.evaluate_only else [("trace",), ("arize_trace",), ("annotations",), ("monitor", "email"), ("repair", "full_evaluation"), ("scenario",)]) + [("evaluate",)] * args.evaluators
    if args.export_only:
        lanes = [("trace",)]
    print(f"Quality worker started with {args.evaluators} evaluation lanes", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        list(pool.map(consume, lanes))


if __name__ == "__main__":
    main()
