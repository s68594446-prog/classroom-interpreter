from __future__ import annotations

import sqlite3
import threading
import time

from classroom_interpreter.agents.health import HealthMetricsStore
from classroom_interpreter.storage import SessionRepository


def test_metrics_use_isolated_database_and_do_not_block_pipeline_writes(
    database, tmp_path
):
    health_path = tmp_path / "health.sqlite3"
    store = HealthMetricsStore(health_path, max_buffered=2048)
    assert health_path.resolve() != database.path.resolve()
    session_id = SessionRepository(database).create_session(
        "health pressure", app_version="test"
    ).id

    errors = []

    def metrics_writer():
        try:
            for index in range(800):
                store.record_metric(
                    "pipeline.pending_segments", index % 31, session_id=session_id
                )
                if index % 32 == 0:
                    store.flush(64)
            while store.buffered_count:
                store.flush(256)
        except Exception as exc:  # surfaced to the test thread
            errors.append(exc)

    thread = threading.Thread(target=metrics_writer)
    thread.start()
    repository = SessionRepository(database)
    latencies = []
    for index in range(200):
        started = time.monotonic()
        repository.update_session_error(
            session_id,
            category="PRESSURE",
            safe_message=f"content-free-{index % 2}",
        )
        latencies.append(time.monotonic() - started)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert max(latencies) < 1.0

    with sqlite3.connect(health_path) as connection:
        samples = connection.execute(
            "SELECT COUNT(*) FROM health_metric_samples"
        ).fetchone()[0]
    assert samples == 800
    with database.connect() as connection:
        # Production health writes never use the legacy main-DB metrics table.
        assert connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0] == 0


def test_locked_health_database_requeues_without_blocking_runtime_database(
    database, tmp_path
):
    store = HealthMetricsStore(tmp_path / "health.sqlite3", max_buffered=128)
    store.record_metric("pipeline.pending_segments", 7)
    lock = sqlite3.connect(store.path)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        assert store.flush() == 0
        session_id = SessionRepository(database).create_session(
            "pipeline remains writable", app_version="test"
        ).id
        SessionRepository(database).update_session_error(
            session_id, category="PROBE", safe_message="content-free"
        )
        assert time.monotonic() - started < 1.0
        assert store.buffered_count == 1
        assert store.last_flush_error == "OperationalError"
    finally:
        lock.rollback()
        lock.close()
    assert store.flush() == 1
    assert store.buffered_count == 0
