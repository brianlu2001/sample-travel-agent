"""Delivery activity from actual queue jobs, inbox records and SMTP receipts."""
import json
import uuid

from quality import store


def delivery_history():
    messages = {r["id"]: r for r in store.rows("SELECT * FROM emails")}
    receipts = {r["id"]: r for r in store.rows("SELECT * FROM email_receipts")}
    events = {}
    for job in store.rows("SELECT * FROM jobs WHERE kind='email' ORDER BY created DESC,id DESC"):
        payload = json.loads(job["payload"])
        identifier = f"<{uuid.uuid5(uuid.NAMESPACE_URL, job['key']).hex}@quality.local>"
        events[identifier] = {"id": identifier, "incident_id": payload.get("incident_id"),
                              "event": payload.get("event"), "queued_at": job["created"],
                              "job_state": job["state"], "attempts": job["attempts"], "error_type": job["error"]}
    # Also retain real receipts if an old queue job has been archived separately.
    for identifier in messages.keys() | receipts.keys():
        events.setdefault(identifier, {"id": identifier, "incident_id": None, "event": None,
                                      "queued_at": None, "job_state": None, "attempts": None, "error_type": None})
    for identifier, event in events.items():
        message, receipt = messages.get(identifier, {}), receipts.get(identifier, {})
        event.update({key: message.get(key) for key in ("recipient", "subject", "body", "received")})
        event.update({key: receipt.get(key) for key in ("accepted", "transport", "smtp_code", "smtp_response")})
        if receipt and 200 <= receipt["smtp_code"] < 300:
            status = "sent"
        elif message:
            status = "received"
        else:
            status = {"pending": "retrying" if event["error_type"] else "queued", "running": "sending", "dead": "failed"}.get(event["job_state"], "unconfirmed")
        event.update(status=status, created=event["accepted"] or event["received"] or event["queued_at"] or 0)
    return sorted(events.values(), key=lambda e: (e["created"], e["id"]), reverse=True)
