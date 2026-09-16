"""Local SMTP sink with a real SMTP delivery path and a dashboard inbox."""
import email
import json
import os
import smtplib
import time
import uuid
from email.message import EmailMessage

from aiosmtpd.controller import Controller

from quality import store
from quality.privacy import safe_payload

RECIPIENT = os.getenv("QUALITY_ALERT_EMAIL", "travel-agent-dev@example.com")


class ReceiptSMTP(smtplib.SMTP):
    def data(self, msg):
        code, response = super().data(msg)
        self.delivery_response = (code, response)
        return code, response


class Inbox:
    async def handle_DATA(self, server, session, envelope):
        message = email.message_from_bytes(envelope.content)
        body = message.get_payload(decode=True).decode("utf-8", errors="replace")
        message_id = str(message.get("Message-ID", uuid.uuid4().hex))
        # Service routing addresses are configuration; free-form mail content is redacted.
        safe = safe_payload({"subject": str(message.get("Subject", "")), "body": body})
        def address(value):
            return value if value in ("quality-monitor@example.com", RECIPIENT) else str(safe_payload(value))
        store.execute("INSERT OR IGNORE INTO emails VALUES(?,?,?,?,?,?)",
                      (message_id, time.time(), address(envelope.mail_from), ",".join(address(v) for v in envelope.rcpt_tos),
                       safe.get("subject", "[WITHHELD]"), safe.get("body", "[WITHHELD]")))
        return "250 Message accepted for local delivery"


def serve():
    store.init()
    controller = Controller(Inbox(), hostname="127.0.0.1", port=1025)
    controller.start()
    print("Local SMTP inbox listening on 127.0.0.1:1025", flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        controller.stop()


def send_alert(payload, key):
    incident = store.rows("SELECT * FROM incidents WHERE id=?", (payload["incident_id"],))[0]
    data = json.loads(incident["payload"])
    message = EmailMessage()
    message["From"] = "quality-monitor@example.com"
    message["To"] = RECIPIENT
    message["Message-ID"] = f"<{uuid.uuid5(uuid.NAMESPACE_URL,key).hex}@quality.local>"
    message["Subject"] = f"[Travel quality] {data['metric']} {payload['event']}"
    measurement = data.get("measurement")
    lines = [f"Travel agent quality alert: {data['metric']} {payload['event']}",
             f"Traffic source: {data['source']}"]
    if measurement:
        lines += [f"Pass rate: {measurement['pass_rate']:.1%} ({measurement['pass']} of {measurement['n']} applicable conversations)",
                  f"Warning threshold: below {data['threshold']:.0%}",
                  f"Window: latest {data['window']} conversations; minimum {data['min_samples']} applicable evaluations."]
    lines += [data.get("description", ""), f"Incident ID: {incident['id']}",
              "Dashboard: http://127.0.0.1:8000/dashboard#incidents",
              "Real executions and Phoenix evaluations. A proposed fix requires a measured baseline, candidate validation and human PR review."]
    message.set_content(str(safe_payload("\n\n".join(lines))))
    host = os.getenv("QUALITY_SMTP_HOST", "127.0.0.1")
    port = int(os.getenv("QUALITY_SMTP_PORT", "1025"))
    with ReceiptSMTP(host, port, timeout=10) as smtp:
        if os.getenv("QUALITY_SMTP_STARTTLS", "false").lower() == "true":
            smtp.starttls()
        if os.getenv("QUALITY_SMTP_USERNAME"):
            smtp.login(os.environ["QUALITY_SMTP_USERNAME"], os.environ["QUALITY_SMTP_PASSWORD"])
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        code, response = smtp.delivery_response
        transport = "local SMTP inbox" if host in ("127.0.0.1", "localhost") and port == 1025 else "configured SMTP relay"
        store.execute("INSERT OR IGNORE INTO email_receipts VALUES(?,?,?,?,?)",
                      (str(message["Message-ID"]), time.time(), transport, code,
                       str(safe_payload(response.decode("utf-8", errors="replace")))))


if __name__ == "__main__":
    serve()
