"""Restartable workers for local processes or pods; bounded retry and leases."""
import argparse
import base64
import concurrent.futures
import importlib
import json
import os
import signal
import threading
import time

import httpx
from phoenix.client import Client

from quality import store
from quality.config import PHOENIX

EXPORT_HTTP = httpx.Client(timeout=15)
PHOENIX_CLIENT = Client(base_url=PHOENIX)
STOP = threading.Event()


def run_leased(job, lease_seconds):
    """Keep long model calls leased while allowing other replicas to claim work.

    Delivery remains at least once: a crash between an external side effect and
    acknowledgment may replay it. Job keys and annotation identities deduplicate.
    """
    finished = threading.Event()
    lost = threading.Event()
    def heartbeat():
        while not finished.wait(min(30, lease_seconds / 3)):
            try:
                if not store.renew(job, lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                STOP.set()  # Drain this process; do not claim more jobs offline.
                return
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        dispatch(job)
        if lost.is_set():
            raise RuntimeError("Job lease lost; result acknowledgment withheld")
    finally:
        finished.set()
        thread.join()


def dispatch(job):
    payload = job["payload"]
    if job["kind"] == "trace":
        headers = {"Content-Type": "application/x-protobuf"}
        if os.getenv("PHOENIX_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["PHOENIX_API_KEY"]
        response = EXPORT_HTTP.post(PHOENIX + "/v1/traces", content=base64.b64decode(payload["otlp"]),
                                    headers=headers, timeout=15)
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
        event = payload.get('evaluation_event')
        if event and event.get('source') in ('live','scenario'):
            from quality.monitoring import current_window
            # Capture the actual acknowledged Phoenix window at delivery time.
            # A busy monitor must not miss a brief breach by reading a later,
            # already recovered window when it finally consumes this event.
            window = current_window(event['source'], event['version'], evaluation_version=event['evaluation_version'])
            store.enqueue('monitor', 'evaluation-ready:'+job['key'], {**event,'window_snapshot':window})
    elif job["kind"] in ("evaluate", "evaluate_live"):
        from quality.evaluation import evaluate_run
        evaluate_run(payload["run_id"])
    elif job["kind"] == "scenario":
        from quality.scenario_stream import execute
        execute(payload)
    elif job["kind"] == "monitor":
        from quality.monitoring import evaluation_ready
        evaluation_ready(payload)
    elif job["kind"] == "email":
        from quality.mailbox import send_alert
        send_alert(payload, job["key"])
    elif job["kind"] == "repair":
        if payload.get('request_id'):
            from quality.repair_dispatch import execute
            return execute(payload['request_id'])
        from quality.repair_dispatch import execute_legacy
        execute_legacy(job)
    elif job["kind"] == "full_evaluation":
        from quality.full_evaluation import execute
        execute(payload)
    elif job['kind'] == 'repair_baseline':
        from quality.repair_dispatch import baseline
        baseline(payload)
    elif job['kind'] == 'pr_lifecycle':
        from quality.repair_dispatch import sync_pr
        sync_pr(payload['pr_url'])


class TraceExportPending(Exception):
    pass


def consume(kinds):
    from quality.queue_notifications import Wakeup
    wakeup = Wakeup()
    try:
        _consume(kinds, wakeup)
    finally:
        wakeup.close()


def claim_job(kinds, lease_seconds):
    if 'evaluate_live' in kinds:
        live = store.claim(('evaluate_live',), lease_seconds=lease_seconds)
        if live:
            return live
    return store.claim(kinds, lease_seconds=lease_seconds)


def _consume(kinds, wakeup):
    last_checkpoint = 0
    while not STOP.is_set():
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
        available_kinds = tuple(k for k in kinds if not provider_paused or k not in ("evaluate", "evaluate_live", "scenario", "repair", "repair_baseline", "full_evaluation"))
        if not available_kinds:
            time.sleep(1)
            continue
        lease_seconds = 7200 if any(k in available_kinds for k in ('repair','repair_baseline','full_evaluation')) else 600
        job = claim_job(available_kinds, lease_seconds)
        if job is None:
            wakeup.wait(STOP)
            continue
        try:
            run_leased(job, lease_seconds)
            store.finish(job)
        except TraceExportPending:
            store.execute("UPDATE jobs SET state='pending',available=?,attempts=attempts-1,lease_until=NULL WHERE id=? AND owner=?",
                          (time.time()+5, job["id"], job["owner"]))
        except Exception as error:
            if job['kind']=='repair' and job['payload'].get('request_id'):
                from quality.repair_dispatch import update
                update(job['payload']['request_id'],'failed',error_type=type(error).__name__)
            if isinstance(error, ImportError):
                from quality.privacy import safe_payload
                store.set_setting("worker_import_error", {"at": time.time(), "kind": job["kind"],
                    "detail": safe_payload(str(error))})
            store.fail(job, error, max_attempts=1 if job["kind"] in ("repair", "repair_baseline", "scenario", "full_evaluation") else 5)
            print(json.dumps({"job": job["id"], "kind": job["kind"], "error_type": type(error).__name__, "attempt": job["attempts"]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluators", type=int, default=3)
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument('--repair-only', action='store_true')
    parser.add_argument('--repairers', type=int, default=2)
    parser.add_argument('--no-repairs', action='store_true')
    args = parser.parse_args()
    if args.evaluators < 0 or (args.evaluate_only and args.evaluators < 1):
        parser.error("Evaluation workers need a positive concurrency; controllers may use zero")
    if args.repairers < 1 or sum((args.evaluate_only,args.export_only,args.repair_only)) > 1:
        parser.error('Choose one worker role and a positive repair concurrency')
    STOP.clear()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    store.init()
    # Load shared SDK integrations before worker threads request each other’s
    # partially initialized modules during a cold start.
    for module in ("quality.evaluation", "quality.remediation", "quality.scenario_stream", "quality.checkpoints"):
        importlib.import_module(module)
    from quality.privacy import analyzer
    analyzer()  # Fail before consuming jobs if the redaction model is unavailable.
    lanes = ([] if args.evaluate_only else [("trace",), ("arize_trace",), ("annotations",), ("monitor", "email", "pr_lifecycle"), ("repair_baseline", "full_evaluation"), ("scenario",)]) + [("evaluate_live","evaluate")] * args.evaluators
    if not args.evaluate_only and not args.no_repairs:
        lanes += [('repair',)] * args.repairers
    if args.repair_only:
        lanes = [('repair',)] * args.repairers
    if args.export_only:
        lanes = [("trace",)]
    print(f"Quality worker started with {args.evaluators} evaluation lanes", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = [pool.submit(consume, lane) for lane in lanes]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        finally:
            STOP.set()


if __name__ == "__main__":
    main()
