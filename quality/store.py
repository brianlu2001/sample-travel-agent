"""Durable SQLite/PostgreSQL job queue and read models; sanitized payloads only.

Short atomic transactions, leases and unique idempotency keys allow restarts and
multiple workers. Model/export calls never happen inside database transactions.
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from quality.config import DATABASE_URL, DB, STATE

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
 version TEXT NOT NULL, benchmark_id TEXT, scenario_id TEXT,
 event TEXT NOT NULL, evaluation TEXT, evaluated REAL
);
CREATE INDEX IF NOT EXISTS runs_scope ON runs(source, version, created);
CREATE TABLE IF NOT EXISTS evaluation_history (
 run_id TEXT NOT NULL, version TEXT NOT NULL, started REAL NOT NULL,
 result TEXT NOT NULL, archived REAL NOT NULL,
 PRIMARY KEY(run_id, version, started)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT UNIQUE NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available REAL NOT NULL, lease_until REAL, owner TEXT, error TEXT, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available, kind);
CREATE TABLE IF NOT EXISTS benchmarks (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent_id TEXT, status TEXT NOT NULL,
 created REAL NOT NULL, completed REAL, manifest TEXT NOT NULL, report TEXT,
 phoenix_dataset TEXT, phoenix_experiment TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_versions (
 benchmark_id TEXT PRIMARY KEY, record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_targets (
 id TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_decisions (
 benchmark_id TEXT NOT NULL, policy_version TEXT NOT NULL, created REAL NOT NULL,
 record TEXT NOT NULL, PRIMARY KEY(benchmark_id,policy_version)
);
CREATE TABLE IF NOT EXISTS checkpoint_approvals (
 benchmark_id TEXT NOT NULL, policy_version TEXT NOT NULL, created REAL NOT NULL,
 note TEXT NOT NULL, PRIMARY KEY(benchmark_id,policy_version)
);
CREATE TRIGGER IF NOT EXISTS benchmark_versions_no_update
 BEFORE UPDATE ON benchmark_versions BEGIN
 SELECT RAISE(ABORT, 'Saved benchmark versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS benchmark_versions_no_delete
 BEFORE DELETE ON benchmark_versions BEGIN
 SELECT RAISE(ABORT, 'Saved benchmark versions are immutable'); END;
CREATE TABLE IF NOT EXISTS incidents (
 id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL,
 repair_id TEXT
);
CREATE TABLE IF NOT EXISTS monitors (
 scope TEXT PRIMARY KEY, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
 id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, status TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repair_requests (
 id TEXT PRIMARY KEY, metric_key TEXT NOT NULL, metric TEXT NOT NULL,
 incident_id TEXT NOT NULL, episode TEXT NOT NULL, state TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL, baseline_id TEXT, repair_id TEXT,
 detail TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_metric_repair ON repair_requests(metric_key)
 WHERE state IN ('queued','waiting_baseline','running','awaiting_review','needs_attention');
CREATE INDEX IF NOT EXISTS repairs_waiting_baseline ON repair_requests(baseline_id,state);
CREATE TABLE IF NOT EXISTS emails (
 id TEXT PRIMARY KEY, received REAL NOT NULL, sender TEXT NOT NULL,
 recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_receipts (
 id TEXT PRIMARY KEY, accepted REAL NOT NULL, transport TEXT NOT NULL,
 smtp_code INTEGER NOT NULL, smtp_response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
 run_id TEXT PRIMARY KEY, created REAL NOT NULL, labels TEXT NOT NULL, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@contextmanager
def connection():
    if DATABASE_URL:
        from quality.postgres import connection as postgres_connection
        with postgres_connection(DATABASE_URL) as con:
            yield con
        return
    STATE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    if os.getenv("QUALITY_REQUIRE_POSTGRES", "false").lower() == "true" and not DATABASE_URL:
        raise RuntimeError("QUALITY_DATABASE_URL is required for this deployment")
    if DATABASE_URL:
        from quality.postgres import initialize
        initialize(DATABASE_URL, SCHEMA)
        return
    with connection() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)


def rows(sql, args=()):
    with connection() as con:
        return [dict(r) for r in con.execute(sql, args)]


def execute(sql, args=()):
    with connection() as con:
        con.execute(sql, args)


def enqueue(kind, key, payload, con=None):
    values = (kind, key, json.dumps(payload, ensure_ascii=False), time.time(), time.time())
    sql = "INSERT OR IGNORE INTO jobs(kind,key,payload,available,created) VALUES(?,?,?,?,?)"
    if con is not None:
        con.execute(sql, values)
    else:
        execute(sql, values)


def claim(kinds, lease_seconds=300):
    now = time.time()
    owner = uuid.uuid4().hex
    if DATABASE_URL:
        from quality.postgres import claim as postgres_claim
        return postgres_claim(DATABASE_URL, kinds, lease_seconds, now, owner)
    with connection() as con:
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in kinds)
        row = con.execute(
            f"SELECT * FROM jobs WHERE kind IN ({placeholders}) AND "
            "((state='pending' AND available<=?) OR (state='running' AND lease_until<?)) "
            "ORDER BY id LIMIT 1", (*kinds, now, now),
        ).fetchone()
        if row is None:
            return None
        con.execute("UPDATE jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                    (owner, now + lease_seconds, row["id"]))
        return {**dict(row), "owner": owner, "payload": json.loads(row["payload"]), "attempts": row["attempts"] + 1}


def finish(job):
    execute("UPDATE jobs SET state='done',lease_until=NULL WHERE id=? AND owner=?",
            (job["id"], job["owner"]))


def renew(job, lease_seconds):
    """Extend only the currently owned lease; never revive a reassigned job."""
    with connection() as con:
        changed = con.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND state='running' AND lease_until>?",
                              (time.time()+lease_seconds, job["id"], job["owner"], time.time()))
        return changed.rowcount == 1


def fail(job, error, max_attempts=3):
    # Store only a code/type, never provider exception payloads.
    state = "dead" if job["attempts"] >= max_attempts else "pending"
    execute("UPDATE jobs SET state=?,available=?,error=?,lease_until=NULL WHERE id=? AND owner=?",
            (state, time.time() + min(60, 2 ** job["attempts"]), type(error).__name__, job["id"], job["owner"]))


def setting(key, default=None):
    found = rows("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(found[0]["value"]) if found else default


def set_setting(key, value):
    execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


def save_run(event):
    with connection() as con:
        con.execute("INSERT INTO runs(id,created,source,version,benchmark_id,scenario_id,event) VALUES(?,?,?,?,?,?,?)",
                    (event["id"], event["created"], event["source"], event["version"], event.get("benchmark_id"),
                     event.get("scenario_id"), json.dumps(event, ensure_ascii=False)))
        enqueue("evaluate", "evaluate:" + event["id"], {"run_id": event["id"]}, con)
        if not event.get("privacy_ok", True):
            # A real redaction failure is significant immediately; no sample warmup.
            incident_id = uuid.uuid4().hex
            payload = {"source": event["source"], "version": event["version"], "benchmark_id": event.get("benchmark_id"),
                       "metric": "privacy", "severity": "critical", "run_id": event["id"],
                       "description": "Redaction failed; content was withheld from the feedback loop."}
            con.execute("INSERT INTO incidents(id,fingerprint,status,created,updated,payload) VALUES(?,?,?,?,?,?)",
                        (incident_id, "privacy:"+event["id"], "open", time.time(), time.time(), json.dumps(payload)))
            enqueue("email", "privacy:"+event["id"], {"incident_id": incident_id, "event": "withheld"}, con)


def get_run(run_id):
    found = rows("SELECT * FROM runs WHERE id=?", (run_id,))
    if not found:
        return None
    row = found[0]
    row["event"] = json.loads(row["event"])
    row["evaluation"] = json.loads(row["evaluation"]) if row["evaluation"] else None
    row["evaluation_history"] = [json.loads(r["result"]) for r in rows(
        "SELECT result FROM evaluation_history WHERE run_id=? ORDER BY started", (run_id,))]
    return row
