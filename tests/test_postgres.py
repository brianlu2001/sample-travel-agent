"""Real PostgreSQL integration tests, isolated schemas, no model calls/telemetry.

CI provides TEST_POSTGRES_URL. The local demo database is never used.
"""
import concurrent.futures
import json
import os
import time
import uuid

import pytest

from quality import store


@pytest.fixture
def database(monkeypatch, tmp_path):
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not configured; PostgreSQL tests run in CI")
    import psycopg
    from psycopg.conninfo import make_conninfo
    from quality.postgres import pool
    schema = "quality_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as con:
        con.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    scoped = make_conninfo(url, options=f"-c search_path={schema}")
    monkeypatch.setattr(store, "DATABASE_URL", scoped)
    monkeypatch.setattr(store, "STATE", tmp_path)
    store.init()
    try:
        yield
    finally:
        pool(scoped).close()
        assert schema.startswith("quality_test_")
        with psycopg.connect(url, autocommit=True) as con:
            con.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def test_versioned_history_queries_on_shared_database(database, monkeypatch):
    from quality import dashboard
    from quality.live_history import performance_history, assessment_for
    from quality.email_history import delivery_history
    monkeypatch.setattr(dashboard, "evaluator_version", lambda: "new-judge")
    store.save_run({"id": "live", "created": 1, "source": "live", "version": "old-agent", "input": [], "tools": []})
    old = {"version": "old-judge", "metrics": {"correctness": {"label": "fail"}}}
    new = {"version": "new-judge", "metrics": {"correctness": {"label": "pass"}}}
    store.execute("INSERT INTO evaluation_history VALUES(?,?,?,?,?)", ("live", "old-judge", 1, json.dumps(old), 2))
    store.execute("UPDATE runs SET evaluation=? WHERE id=?", (json.dumps(new), "live"))
    cohorts = performance_history("new-agent", "new-judge")["cohorts"]
    assert len(cohorts) == 2 and all(c["conversation_count"] == 1 for c in cohorts)
    assert dashboard.run_history(evaluation_version="old-judge", agent_version="old-agent", label="fail")["total"] == 1
    assert assessment_for(store.get_run("live"), "old-judge") == old
    store.enqueue("email", "mail", {"incident_id": "incident", "event": "opened"})
    assert delivery_history()[0]["status"] == "queued"


def test_concurrent_replicas_claim_each_job_once(database):
    for i in range(240):
        store.enqueue("evaluate", f"unit:{i}", {"run_id": str(i)})
        store.enqueue("evaluate", f"unit:{i}", {"run_id": str(i)})
    def consume():
        claimed = []
        while job := store.claim(("evaluate",)):
            claimed.append(job["id"])
            assert store.renew(job, 600)
            store.finish(job)
        return claimed
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as threads:
        groups = list(threads.map(lambda _: consume(), range(12)))
    identifiers = [identifier for group in groups for identifier in group]
    assert len(identifiers) == len(set(identifiers)) == 240
    assert store.rows("SELECT COUNT(*) n FROM jobs WHERE state='done'")[0]["n"] == 240


def test_expired_lease_is_reclaimed_and_old_owner_cannot_acknowledge(database):
    store.enqueue("evaluate", "lease", {})
    old = store.claim(("evaluate",))
    store.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time()-1, old["id"]))
    new = store.claim(("evaluate",))
    assert new["id"] == old["id"] and new["owner"] != old["owner"]
    assert not store.renew(old, 600)
    store.finish(old)
    store.fail(old, RuntimeError())
    assert store.rows("SELECT state FROM jobs")[0]["state"] == "running"
    store.finish(new)


def test_filters_nulls_immutable_versions_and_upserts(database, monkeypatch):
    from quality import dashboard, checkpoints
    monkeypatch.setattr(dashboard, "evaluator_version", lambda: "judge")
    store.save_run({"id": "run", "created": 1, "source": "live", "version": "agent",
                    "conversation_id": "conversation", "input": [], "tools": []})
    result = {"version": "judge", "metrics": {"correctness": {"label": "fail"}}}
    store.execute("UPDATE runs SET evaluation=? WHERE id=?", (json.dumps(result), "run"))
    assert dashboard.run_history(metric="correctness", label="fail", conversation_id="conversation")["total"] == 1
    assert store.rows("SELECT id FROM runs WHERE benchmark_id IS ?", (None,))[0]["id"] == "run"
    assert store.rows("SELECT COUNT(*) total,SUM(evaluated IS NOT NULL) evaluated FROM runs")[0]["evaluated"] == 0
    store.set_setting("key", "before")
    store.set_setting("key", "after")
    assert store.setting("key") == "after"
    checkpoints.register("retired", {"retired": True})
    checkpoints.register("active", {"retired": False})
    assert [t["id"] for t in checkpoints.targets()] == ["active"]
    store.execute("INSERT INTO benchmark_versions VALUES(?,?)", ("baseline", "{}"))
    with pytest.raises(Exception, match="immutable"):
        store.execute("UPDATE benchmark_versions SET record=?", ('{"changed":true}',))
    with pytest.raises(Exception, match="immutable"):
        store.execute("DELETE FROM benchmark_versions")
    store.execute("INSERT OR IGNORE INTO checkpoint_approvals VALUES(?,?,?,?)", ("b", "p", 1, "note"))
    store.execute("INSERT OR IGNORE INTO checkpoint_approvals VALUES(?,?,?,?)", ("b", "p", 1, "note"))
    assert len(store.rows("SELECT * FROM checkpoint_approvals")) == 1


def test_control_decisions_are_serialized(database):
    store.set_setting("counter", 0)
    def increment(_):
        with store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT value FROM settings WHERE key=?", ("counter",)).fetchone()
            time.sleep(.01)
            con.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(json.loads(row["value"])+1), "counter"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as threads:
        list(threads.map(increment, range(24)))
    assert store.setting("counter") == 24


def test_sql_adapter_keeps_bound_values_and_literal_question_marks():
    from quality.postgres import translate
    assert translate("SELECT '?' text WHERE key=? AND key LIKE 'a%'", True) == "SELECT '?' text WHERE key=%s AND key LIKE 'a%%'"


def test_committed_job_pushes_notification_and_rollback_does_not(database):
    import psycopg
    with psycopg.connect(store.DATABASE_URL,autocommit=True) as listener:
        listener.execute('LISTEN quality_jobs')
        with pytest.raises(ValueError):
            with store.connection() as con:
                store.enqueue('repair','rollback-test',{},con)
                raise ValueError('rollback')
        assert not list(listener.notifies(timeout=.1,stop_after=1))
        store.enqueue('repair','commit-test',{})
        events=list(listener.notifies(timeout=2,stop_after=1))
        assert len(events)==1 and events[0].payload=='repair'


def test_metric_reservations_are_unique_across_replicas(database):
    from quality import repair_dispatch as dispatch
    from quality.config import agent_version
    from quality.evaluation import evaluator_version
    store.set_setting('auto_repair',True)
    store.set_setting('evaluator_audit',{'status':'passed','evaluator_version':evaluator_version()})
    cases=[]
    for i in range(20):
        metric='correctness' if i%2 else 'groundedness'
        data={'source':'live','metric':metric,'version':agent_version(),'evaluator_version':evaluator_version(),'failing_run_ids':['isolated']}
        store.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,NULL)',(str(i),str(i),'open',time.time(),time.time(),json.dumps(data)))
        cases.append({'id':str(i),'payload':json.dumps(data)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as threads:
        list(threads.map(dispatch.schedule,cases))
    assert len(store.rows('SELECT * FROM repair_requests'))==2
    assert len(store.rows("SELECT * FROM jobs WHERE kind='repair'"))==2
