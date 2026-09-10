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


def test_health_flush_completes_while_runtime_database_is_write_locked(
    database, tmp_path
):
    """The isolation direction the earlier suite never exercised.

    The prior tests locked the *health* database. The reviewer's concern was
    the opposite: health writes must not need the runtime DB's write lock. A
    held `BEGIN IMMEDIATE` on the runtime database would block any writer
    that shares it, so a health flush completing here proves the stores are
    genuinely separate rather than merely differently named.

    Mutation that turns this red: point `HealthMetricsStore` at
    `database.path` (a shared store) -- the flush then contends on the held
    runtime write lock, exceeds its 100ms busy timeout, and returns 0.
    """
    store = HealthMetricsStore(tmp_path / "health.sqlite3", max_buffered=256)
    for index in range(50):
        store.record_metric("pipeline.pending_segments", index)
    assert store.buffered_count == 50

    holder = sqlite3.connect(database.path)
    holder.execute("BEGIN IMMEDIATE")  # runtime DB write lock held for real
    try:
        started = time.monotonic()
        flushed = store.flush(256)
        elapsed = time.monotonic() - started
        assert flushed == 50, f"health flush blocked by runtime lock: {flushed}"
        assert store.last_flush_error is None
        assert elapsed < 1.0
    finally:
        holder.rollback()
        holder.close()

    assert store.buffered_count == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM health_metric_samples"
        ).fetchone()[0] == 50


def test_health_buffer_overflow_drops_oldest_and_counts_without_content(tmp_path):
    """Overflow must stay bounded and record only a number.

    Mutation that turns this red: give `_buffer` an unbounded deque (drop the
    `maxlen`) -- memory then grows without limit and `buffered_count` exceeds
    `max_buffered`; or stop incrementing `dropped_samples`, and the loss
    becomes silent.
    """
    store = HealthMetricsStore(tmp_path / "health.sqlite3", max_buffered=10)
    for index in range(35):
        store.record_metric("pipeline.pending_segments", index)

    assert store.buffered_count == 10, "buffer must stay bounded"
    assert store.dropped_samples == 25

    assert store.flush(64) == 10
    with sqlite3.connect(store.path) as connection:
        values = [
            row[0]
            for row in connection.execute(
                "SELECT numeric_value FROM health_metric_samples ORDER BY id"
            ).fetchall()
        ]
    # Oldest dropped, newest retained: the surviving window is the last 10.
    assert values == [float(index) for index in range(25, 35)]
    # The drop record is a count only -- no metric payload, no session content.
    assert isinstance(store.dropped_samples, int)


def test_health_monitor_flushes_buffer_on_shutdown(tmp_path):
    """Stopping the monitor must drain, not silently discard.

    Mutation that turns this red: delete the final `await
    asyncio.to_thread(self.store.flush)` after the run loop -- buffered
    samples then never reach the isolated database.
    """
    import asyncio

    from classroom_interpreter.agents.health import HealthMonitorAgent

    class _IdleRuntime:
        active_session = None
        worker_faults: dict = {}

    store = HealthMetricsStore(tmp_path / "health.sqlite3", max_buffered=256)
    # A long interval is essential. With a short one the periodic in-loop
    # flush drains the buffer on its own and this test passes even when the
    # shutdown flush is deleted -- it would assert nothing. Here the loop
    # flushes once, then parks on stop.wait(), and the samples recorded below
    # can only reach the database via the flush after the loop.
    monitor = HealthMonitorAgent(store, _IdleRuntime(), interval_seconds=60.0)

    async def exercise():
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        # Let the loop complete its single scheduled flush and park on
        # stop.wait(); the next periodic flush is 60s away.
        await asyncio.sleep(0.2)

        # Buffered strictly after the periodic flush had its chance.
        store.record_metric("pipeline.pending_segments", 1)
        store.record_heartbeat("segmenter", "ok")
        assert store.buffered_count == 2

        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(exercise())
    assert store.buffered_count == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM health_metric_samples"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT COUNT(*) FROM health_heartbeats"
        ).fetchone()[0] >= 1
