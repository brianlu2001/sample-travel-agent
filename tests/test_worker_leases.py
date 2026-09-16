"""Lifecycle checks use an isolated journal, never model calls or exports."""
import threading
import time

import pytest

from quality import store, worker


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "leases.sqlite3")
    monkeypatch.setattr(worker, "STOP", threading.Event())
    store.init()


def test_heartbeat_retains_long_running_job(monkeypatch):
    store.enqueue("unit", "unit", {})
    job = store.claim(("unit",), lease_seconds=.3)
    monkeypatch.setattr(worker, "dispatch", lambda job: time.sleep(.45))
    worker.run_leased(job, .3)
    assert store.claim(("unit",)) is None
    store.finish(job)
    assert store.rows("SELECT state FROM jobs")[0]["state"] == "done"


def test_shutdown_finishes_current_job_without_claiming_next(monkeypatch):
    store.enqueue("unit", "first", {})
    store.enqueue("unit", "next", {})
    monkeypatch.setattr(worker, "dispatch", lambda job: worker.STOP.set())
    worker.consume(("unit",))
    assert [r["state"] for r in store.rows("SELECT state FROM jobs ORDER BY id")] == ["done", "pending"]


def test_cluster_configuration_never_silently_falls_back_to_sqlite(monkeypatch):
    monkeypatch.setenv("QUALITY_REQUIRE_POSTGRES", "true")
    with pytest.raises(RuntimeError, match="required"):
        store.init()
