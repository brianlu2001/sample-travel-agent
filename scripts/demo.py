"""One local launcher. All generated files live under gitignored .quality/."""
import argparse
import json
import os
import subprocess
import sys
import time

import httpx

from quality import store
from quality.config import ROOT, STATE


def spawn(name, command, extra_env=None):
    STATE.mkdir(exist_ok=True)
    log = (STATE / (name + ".log")).open("a", encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", **(extra_env or {})}
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    log.close()
    store.set_setting("process:"+name, {"pid": process.pid, "command": command})
    return process


def healthy(url):
    try:
        return httpx.get(url, timeout=2).is_success
    except httpx.HTTPError:
        return False


def start():
    store.init()
    if not healthy("http://127.0.0.1:6006/healthz"):
        phoenix_python = ROOT / ".phoenix-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not phoenix_python.exists():
            raise RuntimeError("Install the Phoenix server environment first; see README setup")
        spawn("phoenix", [str(phoenix_python), "-X", "utf8", "-m", "phoenix.server.main", "serve"],
              {"PHOENIX_WORKING_DIR": str(STATE / "phoenix"), "PHOENIX_HOST": "127.0.0.1", "PHOENIX_PORT": "6006", "PHOENIX_TELEMETRY_ENABLED": "false"})
        for _ in range(60):
            if healthy("http://127.0.0.1:6006/healthz"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Phoenix did not become ready; inspect .quality/phoenix.log")
    import socket
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 1025)):
            spawn("mailbox", [sys.executable, "-X", "utf8", "-m", "quality.mailbox"])
    import psutil
    worker = store.setting("process:worker")
    if not worker or not psutil.pid_exists(worker["pid"]):
        spawn("worker", [sys.executable, "-X", "utf8", "-m", "quality.worker", "--evaluators", os.getenv("QUALITY_EVALUATORS", "8")])
    if not healthy("http://127.0.0.1:8000/health"):
        spawn("api", [sys.executable, "-X", "utf8", "-m", "uvicorn", "agent.api:app", "--host", "127.0.0.1", "--port", "8000"])
    print("Chat: http://127.0.0.1:8000/\nDashboard: http://127.0.0.1:8000/dashboard\nPhoenix: http://127.0.0.1:6006/")


def stop(names=("api", "worker-export", "worker-extra", "worker", "mailbox", "phoenix")):
    import psutil
    for name in names:
        saved = store.setting("process:"+name)
        if not saved:
            continue
        try:
            process = psutil.Process(saved["pid"])
            # Never stop a recycled PID or a process outside this workspace.
            if process.cmdline() != saved["command"] or not str(process.exe()).lower().startswith(str(ROOT).lower()):
                continue
            for child in process.children(recursive=True):
                child.terminate()
            process.terminate()
            process.wait(timeout=5)
            store.set_setting("process:"+name, None)
        except psutil.NoSuchProcess:
            store.set_setting("process:"+name, None)


def enable_repair():
    store.set_setting("auto_repair", True)
    from quality.monitoring import schedule_repair
    schedule_repair()
    print("Live incident workflow enabled. A current baseline is measured before any proposal; candidates always await human PR review.")


def enable_checkpoints():
    from quality.checkpoints import register_candidate, sync_running
    from quality.evaluation import evaluator_version
    from quality.versions import archive_benchmarks
    store.init()
    archive_benchmarks()
    sync_running()
    for row in store.rows("SELECT id FROM repairs WHERE status='pr_open' AND json_extract(payload,'$.evaluator_version')=? ORDER BY created", (evaluator_version(),)):
        register_candidate(row["id"])
    store.set_setting("checkpoint_schedule_enabled", True)
    print("Daily checkpoints enabled: 24 hours AND a changed version. Existing full results are reused; deployment remains manual.")


def resume_provider():
    """Operator action after restoring credits; reuse actual responses and scores."""
    block = store.setting("provider_block")
    if not block:
        raise RuntimeError("No recorded provider block to resume")
    from quality.evaluation import evaluator_version
    baseline_id = block["baseline_id"]
    baseline = store.rows("SELECT * FROM benchmarks WHERE id=?", (baseline_id,))[0]
    if json.loads(baseline["manifest"])["evaluator_version"] != evaluator_version():
        raise RuntimeError("Evaluator changed; blocked baseline requires separate revalidation")
    with store.connection() as con:
        con.execute("UPDATE benchmarks SET status='running' WHERE id=? AND status='provider_blocked'", (baseline_id,))
        con.execute("UPDATE jobs SET state='pending',available=?,attempts=0,lease_until=NULL,owner=NULL "
                    "WHERE kind='evaluate' AND state IN ('dead','running','pending') "
                    "AND json_extract(payload,'$.run_id') IN (SELECT id FROM runs WHERE benchmark_id=?)", (time.time(), baseline_id))
        con.execute("UPDATE jobs SET state='pending',available=?,attempts=0,lease_until=NULL,owner=NULL "
                    "WHERE kind='repair' AND state='dead' AND json_extract(payload,'$.incident_id')=?", (time.time(), block["incident_id"]))
    campaign = store.setting("scenario_campaign")
    campaign.update(status="awaiting_repair", note="Resuming the existing baseline and its unfinished evaluations.")
    store.set_setting("scenario_campaign", campaign)
    store.set_setting("provider_block", None)
    store.set_setting("auto_repair", True)
    store.set_setting("monitor_enabled", True)
    stop(("worker-export",))
    start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "enable-repair", "enable-checkpoints", "status", "retry-failed", "resume-repair", "revise-repair", "resume-provider"])
    args = parser.parse_args()
    store.init()
    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "enable-repair":
        enable_repair()
    elif args.action == "enable-checkpoints":
        enable_checkpoints()
    elif args.action == "resume-provider":
        resume_provider()
    elif args.action == "retry-failed":
        # Operational retry, never changes recorded scores or creates fake traces.
        store.execute("UPDATE jobs SET state='pending',available=?,attempts=0 WHERE state='dead' AND kind!='repair'", (time.time(),))
        print("Failed infrastructure/evaluation jobs requeued")
    elif args.action == "resume-repair":
        store.execute("UPDATE jobs SET state='pending',available=?,attempts=0 WHERE state='dead' AND kind='repair'", (time.time(),))
        print("Failed repair jobs requeued; existing validated candidates and completed benchmark cases are reused")
    elif args.action == "revise-repair":
        baseline_id = store.setting("baseline_id")
        rejected = store.rows("SELECT * FROM repairs WHERE status='rejected' AND json_extract(payload,'$.baseline_id')=? AND json_extract(payload,'$.revision_of') IS NULL ORDER BY created DESC LIMIT 1", (baseline_id,))
        if not rejected:
            raise RuntimeError("No rejected initial proposal to revise")
        prior = rejected[0]
        store.enqueue("repair", "revision:"+prior["id"], {"incident_id": prior["incident_id"], "baseline_id": baseline_id, "revision_of": prior["id"]})
        print("One distinct revision queued using development failures; original rejection retained")
    else:
        print(json.dumps({"baseline_id": store.setting("baseline_id"), "auto_repair": store.setting("auto_repair", False),
                          "jobs": store.rows("SELECT kind,state,COUNT(*) n FROM jobs GROUP BY kind,state")}, indent=2))


if __name__ == "__main__":
    main()
