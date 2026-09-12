"""Database handles must be released, not left to the garbage collector.

Found by watching `lsof` during a live 60-minute run: the server held 102 open
descriptors to `health.sqlite3`. `with sqlite3.connect(...) as connection:`
commits or rolls back and does NOT close -- a trap the storage layer already
guards against with `ClosingConnection`, which the health store simply never
used.

Nothing failed as a result, because the limit on this machine is a million
descriptors and the collector eventually reclaimed them. It is still a leak,
and it is the kind that surfaces on someone else's machine, three hours in.
"""

from __future__ import annotations

import gc

from classroom_interpreter.agents.health import HealthMetricsStore


def open_handles_to(path) -> int:
    """Count live sqlite3 connections without shelling out to lsof."""
    import sqlite3

    return sum(
        1
        for obj in gc.get_objects()
        if isinstance(obj, sqlite3.Connection)
    )


def test_flush_does_not_accumulate_connections(tmp_path):
    """Mutation that turns this red: drop `factory=self._ClosingConnection`.

    Counting live `sqlite3.Connection` objects rather than file descriptors
    keeps the test portable, and it fails for the same reason: an unclosed
    connection is what holds the descriptor.
    """
    store = HealthMetricsStore(path=tmp_path / "health.sqlite3")

    gc.collect()
    before = open_handles_to(store.path)

    for index in range(40):
        store.record_metric("probe", float(index))
        store.flush()

    # No collection here on purpose: the point is that the connections are
    # already closed, not that the collector can be relied on to close them.
    after = open_handles_to(store.path)

    assert after - before <= 1, (
        f"{after - before} sqlite3 connections still live after 40 flushes; "
        "`with sqlite3.connect(...)` commits but does not close"
    )


def test_the_store_still_actually_writes(tmp_path):
    """Closing eagerly must not cost the writes themselves.

    A fix that closed the connection before the commit landed would make the
    leak test pass and silently discard every health sample.
    """
    store = HealthMetricsStore(path=tmp_path / "health.sqlite3")
    for index in range(5):
        store.record_metric("latency_ms", float(index), session_id="s1")
    written = store.flush()
    assert written == 5

    import sqlite3

    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute(
            "SELECT metric_name, numeric_value FROM health_metric_samples"
            " ORDER BY numeric_value"
        ).fetchall()
    finally:
        connection.close()
    assert [r[1] for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0]
