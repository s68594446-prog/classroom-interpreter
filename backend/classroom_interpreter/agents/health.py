from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class HealthMetricsStore:
    """Bounded content-free buffer flushed to an isolated SQLite file."""

    path: Path
    max_buffered: int = 4096
    _buffer: deque[tuple] = field(init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    dropped_samples: int = 0
    last_flush_error: str | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.max_buffered < 1:
            raise ValueError("health metrics buffer must be positive")
        self._buffer = deque(maxlen=self.max_buffered)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    class _ClosingConnection(sqlite3.Connection):
        """Commit/rollback AND release the handle on context exit.

        `with sqlite3.connect(...) as connection:` commits or rolls back but
        does NOT close -- a long-standing trap in the stdlib API. Every flush
        therefore leaked one open descriptor to the health database until the
        garbage collector happened to run. A live hour of recording held 102
        handles to a single file. `storage.database.ClosingConnection` exists
        for exactly this reason; it simply was never applied here.
        """

        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                self.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=0.1, factory=self._ClosingConnection
        )
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 100")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS health_heartbeats (
                    agent_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS health_metric_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    metric_name TEXT NOT NULL,
                    numeric_value REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _append(self, item: tuple) -> None:
        with self._lock:
            if len(self._buffer) == self.max_buffered:
                self.dropped_samples += 1
            self._buffer.append(item)

    def record_heartbeat(
        self, agent_name: str, status: str, *, error_code: str | None = None
    ) -> None:
        self._append(("heartbeat", agent_name, status, error_code, _now()))

    def record_metric(
        self,
        metric_name: str,
        numeric_value: float,
        *,
        session_id: str | None = None,
    ) -> None:
        self._append(
            ("metric", session_id, metric_name, float(numeric_value), _now())
        )

    @property
    def buffered_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def flush(self, batch_size: int = 256) -> int:
        with self._lock:
            batch = [self._buffer.popleft() for _ in range(min(batch_size, len(self._buffer)))]
        if not batch:
            return 0
        try:
            with self._connect() as connection:
                for item in batch:
                    if item[0] == "heartbeat":
                        _, agent_name, status, error_code, observed_at = item
                        connection.execute(
                            """
                            INSERT INTO health_heartbeats(agent_name, status, error_code, observed_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(agent_name) DO UPDATE SET
                              status=excluded.status, error_code=excluded.error_code,
                              observed_at=excluded.observed_at
                            """,
                            (agent_name, status, error_code, observed_at),
                        )
                    else:
                        _, session_id, metric_name, value, recorded_at = item
                        connection.execute(
                            """
                            INSERT INTO health_metric_samples(
                                session_id, metric_name, numeric_value, recorded_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (session_id, metric_name, value, recorded_at),
                        )
        except sqlite3.Error as exc:
            with self._lock:
                for item in reversed(batch):
                    if len(self._buffer) == self.max_buffered:
                        self._buffer.pop()
                        self.dropped_samples += 1
                    self._buffer.appendleft(item)
            self.last_flush_error = type(exc).__name__
            return 0
        self.last_flush_error = None
        return len(batch)


@dataclass(slots=True)
class HealthMonitorAgent:
    store: HealthMetricsStore
    runtime: Any
    interval_seconds: float = 1.0

    def sample_once(self) -> None:
        session_id = self.runtime.active_session
        if session_id is not None:
            progress = self.runtime.progress(session_id)
            self.store.record_metric(
                "pipeline.pending_segments",
                progress["pending_segments"],
                session_id=session_id,
            )
            self.store.record_metric(
                "pipeline.live_delay_ms",
                progress["live_delay_ms"],
                session_id=session_id,
            )
        for name, fault in self.runtime.worker_faults.items():
            self.store.record_heartbeat(
                name, "degraded", error_code=str(fault["error_type"])
            )

    async def run(self, stop) -> None:
        import asyncio

        while not stop.is_set():
            try:
                self.sample_once()
                await asyncio.to_thread(self.store.flush)
            except Exception as exc:
                self.store.last_flush_error = type(exc).__name__
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue
        await asyncio.to_thread(self.store.flush)
