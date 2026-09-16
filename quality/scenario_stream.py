"""Paced real conversations, followed by the existing Phoenix incident workflow.

Development reference inputs or operator-supplied conversations are used. Each
conversation is evaluated before the next starts. Outputs are actual executions.
"""
import json
import time
import uuid
from collections import defaultdict

from quality import store
from quality.config import agent_version
from quality.evaluation import evaluator_version, scenarios

ACTIVE = ("running", "awaiting_repair")


def plan():
    groups = defaultdict(list)
    for scenario in scenarios():
        if scenario["split"] == "development":
            groups[scenario["category"]].append(scenario["id"])
    # Round-robin coverage: flights, hotels, itineraries, weather, scope, etc.
    return [items[i] for i in range(max(map(len, groups.values())))
            for items in groups.values() if i < len(items)]


def start(cases=None, *, stop_on_incident=True):
    current = store.setting("scenario_campaign", {})
    if current.get("status") in ACTIVE:
        return current
    if cases is not None:
        if not cases or len({c['id'] for c in cases}) != len(cases) or any(
            not c.get("messages") or any(not isinstance(m, str) or not m.strip() for m in c["messages"])
            for c in cases
        ):
            raise ValueError("Provide distinct conversations with nonempty user messages")
        from quality.privacy import safe_payload
        cases = safe_payload(cases)
        if not isinstance(cases, list):
            raise ValueError("Conversation plan could not be redacted")
    if current.get("id"):
        store.set_setting("scenario_campaign:" + current["id"], current)
    current = {"id": uuid.uuid4().hex, "status": "running", "created": time.time(),
               "version": agent_version(), "evaluator_version": evaluator_version(),
               "scenario_ids": [c["id"] for c in cases] if cases is not None else plan(),
               "custom_cases": cases, "stop_on_incident": stop_on_incident,
               "completed": 0, "interval_seconds": 15,
               "next_at": time.time(), "last_run_ids": [],
               "note": "Real executions on synthetic development inputs; separate Phoenix rolling window."}
    store.set_setting("scenario_campaign", current)
    return current


def stop():
    current = store.setting("scenario_campaign", {})
    if current.get("status") in ACTIVE:
        current.update(status="paused", updated=time.time())
        store.set_setting("scenario_campaign", current)
    return current


def save(current, **updates):
    current.update(**updates, updated=time.time())
    store.set_setting("scenario_campaign", current)


def tick():
    current = store.setting("scenario_campaign", {})
    if current.get("status") not in ACTIVE:
        return
    if current["version"] != agent_version() or current["evaluator_version"] != evaluator_version():
        save(current, status="needs_attention", note="Agent or evaluator changed during this campaign.")
        return
    incidents = store.rows("SELECT * FROM incidents WHERE created>=? AND json_extract(payload,'$.source')='scenario' ORDER BY created",
                           (current["created"],))
    if incidents and current.get("stop_on_incident", True):
        incident_ids = [i["id"] for i in incidents]
        repairs = store.rows("SELECT * FROM repairs ORDER BY created DESC")
        repairs = [r for r in repairs if r["incident_id"] in incident_ids]
        save(current, status="awaiting_repair", incident_ids=incident_ids,
             note="Threshold breached. Scenario requests paused while the baseline and candidate are measured.")
        if not repairs:
            dead = store.rows("SELECT payload FROM jobs WHERE kind='repair' AND state='dead'")
            if any(json.loads(j["payload"]).get("incident_id") in incident_ids for j in dead):
                save(current, status="needs_attention", note="Repair job failed; inspect worker health.")
            return
        repair = repairs[0]
        payload = json.loads(repair["payload"])
        save(current, repair_id=repair["id"])
        if repair["status"] == "pr_open":
            save(current, status="complete", pr_url=payload["pr_url"], note="New draft PR awaits human review; scenario traffic stopped.")
        elif repair["status"] == "rejected":
            if payload.get("revision_of"):
                save(current, status="rejected", note="Both proposals retained. Quality gates rejected the revision; no new PR published.")
            else:
                store.enqueue("repair", "revision:" + repair["id"],
                              {"incident_id": repair["incident_id"], "baseline_id": payload["baseline_id"], "revision_of": repair["id"]})
                save(current, note="Initial candidate rejected. One distinct revision queued; validation gates stay fixed.")
        elif repair["status"] in ("failed", "awaiting_github_access", "awaiting_human_evidence"):
            save(current, status="needs_attention", note="Repair needs operator attention: " + repair["status"])
        return
    pending = store.rows("SELECT state FROM jobs WHERE kind='scenario' AND key LIKE ? AND state IN ('pending','running','dead')",
                         ("scenario:" + current["id"] + ":%",))
    if any(j["state"] == "dead" for j in pending):
        save(current, status="needs_attention", note="A scenario execution failed; its real error trace is retained.")
        return
    if pending or time.time() < current["next_at"]:
        return
    for run_id in current["last_run_ids"]:
        evaluation = store.get_run(run_id)["evaluation"]
        if not evaluation:
            dead = store.rows("SELECT id FROM jobs WHERE key=? AND state='dead'", ("evaluate:" + run_id,))
            if dead:
                save(current, status="needs_attention", note="Evaluation failed; no replacement scores were inserted.")
            return
    # Wait for the actual annotation export before asking Phoenix about this turn.
    if store.rows("SELECT id FROM jobs WHERE kind IN ('trace','annotations') AND state IN ('pending','running') LIMIT 1"):
        return
    observed = store.setting("scenario_monitor", {}).get("evaluated_run_ids", [])
    if not set(current["last_run_ids"][-1:]).issubset(observed):
        return
    # A just-created incident must pause requests before we schedule another case.
    if current.get("stop_on_incident", True) and store.rows("SELECT id FROM incidents WHERE created>=? AND json_extract(payload,'$.source')='scenario'", (current["created"],)):
        return
    if current["completed"] >= len(current["scenario_ids"]):
        if current.get("custom_cases") is not None:
            save(current, status="complete", note="All requested conversations executed and evaluated; any repair workflow continues independently.")
        else:
            save(current, status="exhausted", note="All development scenarios evaluated without a qualifying breach. No PR forced.")
        return
    store.enqueue("scenario", f"scenario:{current['id']}:{current['completed']}",
                  {"campaign_id": current["id"], "index": current["completed"]})


def execute(payload):
    current = store.setting("scenario_campaign", {})
    if current.get("id") != payload["campaign_id"] or current.get("status") != "running":
        return
    if payload["index"] != current["completed"]:
        return
    # An expired lease could mean the provider already answered. Require review,
    # rather than silently sending the same conversation a second time.
    if current.get("in_flight"):
        save(current, status="needs_attention", note="Interrupted scenario needs review before retry.")
        return
    from quality.runtime import prepare_agent, run_observed
    if prepare_agent()["version"] != current["version"]:
        save(current, status="needs_attention", note="Scenario worker must restart to load the active agent.")
        return
    scenario_id = current["scenario_ids"][current["completed"]]
    candidates = current.get("custom_cases")
    if candidates is None:
        candidates = [s for s in scenarios() if s["split"] == "development"]
    scenario = next(s for s in candidates if s["id"] == scenario_id)
    save(current, in_flight=scenario_id)
    messages, run_ids, session = [], [], uuid.uuid4().hex
    for message in scenario["messages"]:
        messages.append({"role": "user", "content": message})
        _, messages, event = run_observed(messages, source="scenario", conversation_id=session, scenario_id=scenario_id)
        run_ids.append(event["id"])
    # Preserve a pause requested while the provider was generating its response.
    latest = store.setting("scenario_campaign", {})
    if latest.get("id") == current["id"]:
        save(latest, completed=current["completed"] + 1, in_flight=None,
             last_run_ids=run_ids, next_at=time.time() + current["interval_seconds"])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    store.init()
    result = start() if args.action == "start" else stop() if args.action == "stop" else store.setting("scenario_campaign", {})
    print(json.dumps(result, indent=2))
