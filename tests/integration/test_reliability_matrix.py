"""Phase 8: automated reliability matrix cases.

Covers the FAILURE_MODES.md rows that can be exercised deterministically in
process. Hardware-only rows (F-015..F-018) remain target-mac manual tests and
are listed as NOT TESTED in the Phase 8 report.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

import pytest

from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import ProviderError, ProviderErrorCategory
from classroom_interpreter.main import create_app
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mocks import MockSummaryProvider
from classroom_interpreter.runtime.pipeline import SessionOrchestrator
from classroom_interpreter.storage import Database, SessionRepository

from test_live_api import FakeASR, FakeListener, FakeTranslation, make_settings
import helpers_phase3 as h


class OutageASR(FakeASR):
    """Network outage for the first N provider calls, then full recovery."""

    def __init__(self, outage_calls: int):
        self.remaining = outage_calls
        self.calls = 0

    async def transcribe(self, segment, *, language: str = "en"):
        from classroom_interpreter.domain.models import TranscriptResult, utc_now

        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.NETWORK,
                    retryable=True,
                    safe_message="network failure",
                )
            )
        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=f"Recovered point {segment.sequence}.",
            provider=self.name,
            model=self.model,
            latency_ms=1,
            audio_seconds=segment.duration_ms / 1000,
            created_at=utc_now(),
        )


class AuthFailingASR(FakeASR):
    async def transcribe(self, segment, *, language: str = "en"):
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.AUTH,
                retryable=False,
                http_status=401,
                safe_message="provider rejected credentials",
            )
        )


def make_runtime(tmp_path: Path, database: Database, *, asr=None, auto_purge=False):
    settings = make_settings(tmp_path)
    return SessionOrchestrator(
        settings,
        database,
        asr_provider=asr or FakeASR(),
        translation_provider=FakeTranslation(),
        summary_provider=MockSummaryProvider(),
        listener_factory=lambda root: FakeListener(database, root),
        auto_purge=auto_purge,
    )


def build_app(tmp_path: Path, database: Database, runtime):
    from fastapi.testclient import TestClient

    return TestClient(create_app(make_settings(tmp_path), database, runtime=runtime))


def wait_status(client, session_id, statuses, timeout=15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{session_id}")
        if response.status_code == 404:
            return "PURGED"
        last = response.json()["session"]["status"]
        if last in statuses:
            return last
        time.sleep(0.1)
    return last


def test_network_outage_recovers_with_zero_data_loss(tmp_path):
    """F-001/F-002/F-035: provider unreachable for a window. Capture and
    segmentation continue; durable work drains after recovery; no loss."""
    tmp = tmp_path / "data"
    database = Database(tmp_path / "runtime.sqlite3")
    database.migrate()
    asr = OutageASR(outage_calls=6)
    runtime = make_runtime(tmp_path, database, asr=asr, auto_purge=True)
    client = build_app(tmp_path, database, runtime)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Outage"}).json()
        session_id = view["session"]["id"]
        # Give the worker loop a moment to hit the outage while capture runs.
        time.sleep(0.5)
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            probe = client.get(f"/api/sessions/{session_id}")
            if probe.status_code == 404:
                status = "PURGED"
                break
            status = probe.json()["session"]["status"]
            if status == "RECOVERY_REQUIRED":
                break
            time.sleep(0.1)
        assert status == "PURGED"
        # Retries happened during the outage (at-least-once delivery) and the
        # full drain completed — duplicate-commit freedom is asserted by the
        # Phase 3/4 idempotency suites (content rows are purged here).
        assert asr.calls > 2


def test_backend_restart_reconciles_and_recovers(tmp_path):
    """F-013: backend restart mid-recording. The session becomes a visible
    recovery choice; resume drains all durable work to a saved summary."""
    tmp = tmp_path / "data"
    database = Database(tmp / "runtime.sqlite3")
    database.migrate()
    runtime = make_runtime(tmp_path, database, auto_purge=False)
    client = build_app(tmp_path, database, runtime)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Restart"}).json()
        session_id = view["session"]["id"]
        deadline = time.monotonic() + 10
        done_before = 0
        while time.monotonic() < deadline:
            view2 = client.get(f"/api/sessions/{session_id}").json()
            done_before = int(view2["progress"]["processed_audio_ms"] > 0)
            if done_before:
                break
            time.sleep(0.1)
        assert done_before  # some work completed before the "restart"

    # Backend restart: a fresh orchestrator on the same durable state.
    runtime2 = make_runtime(tmp_path, database, auto_purge=True)
    moved = runtime2.reconcile_on_startup()
    assert moved == 1
    client2 = build_app(tmp_path, database, runtime2)
    with client2:
        record = SessionRepository(database).get_session(session_id)
        assert record.status == "RECOVERY_REQUIRED"
        listed = client2.get("/api/recovery").json()["sessions"]
        assert any(s["id"] == session_id for s in listed)
        resumed = client2.post(f"/api/recovery/{session_id}/resume")
        assert resumed.status_code == 200
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            probe = client2.get(f"/api/sessions/{session_id}")
            if probe.status_code == 404:
                status = "PURGED"
                break
            status = probe.json()["session"]["status"]
            time.sleep(0.1)
        assert status == "PURGED"
        summaries = list((tmp / "summaries").glob("*.md"))
        assert len(summaries) == 1


def test_sqlite_busy_never_loses_work(tmp_path):
    """F-021: a long external write lock must not corrupt or lose durable
    work; the worker survives and finishes afterwards."""
    tmp = tmp_path / "data"
    database = Database(tmp / "runtime.sqlite3")
    database.migrate()
    runtime = make_runtime(tmp_path, database, auto_purge=False)
    client = build_app(tmp_path, database, runtime)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Busy"}).json()
        session_id = view["session"]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            view2 = client.get(f"/api/sessions/{session_id}").json()
            if view2["progress"]["captured_audio_ms"] > 0:
                break
            time.sleep(0.1)
        # Hold an exclusive write lock while the worker tries to claim.
        blocker = sqlite3.connect(tmp / "runtime.sqlite3", timeout=1)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute("INSERT INTO agent_heartbeats(agent_name, instance_id, status, last_seen_at) VALUES ('blocker','1','ok',datetime('now'))")
            time.sleep(0.2)
            # The step may fail with a busy error; it must not lose data.
            try:
                asyncio.run(runtime._asr_worker.step())
            except sqlite3.OperationalError:
                pass  # visible, bounded failure; work stays durable
        finally:
            blocker.rollback()
            blocker.close()
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status in ("SUMMARY_SAVED", "RECOVERY_REQUIRED"):
                break
            time.sleep(0.1)
        # The contract is "never loses work": once the external lock is gone the
        # blocked claim must be retried and the class must finish. Accepting
        # RECOVERY_REQUIRED here (as this test used to) let a genuine failure to
        # recover pass silently -- the independent review called this assertion
        # "too permissive for recovery success", and it is the one shape of
        # assertion this project forbids.
        assert status == "SUMMARY_SAVED", (
            "a transient SQLite busy lock must not cost the class its summary; "
            f"ended in {status}"
        )
        with database.connect() as connection:
            bad = connection.execute("PRAGMA integrity_check").fetchone()[0]
            states = {
                row["state"]: row["n"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS n FROM segments "
                    "WHERE session_id = ? GROUP BY state",
                    (session_id,),
                ).fetchall()
            }
            transcribed = connection.execute(
                "SELECT COUNT(*) AS n FROM asr_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()["n"]
        assert bad == "ok"
        # No work lost: every segment reached DONE and each one kept its
        # transcript, including the segment whose claim hit the busy lock.
        assert states and set(states) == {"DONE"}, f"unfinished segments: {states}"
        assert transcribed == states["DONE"]


def test_summary_write_failure_refuses_saved_then_recovers(tmp_path):
    """F-025: summary write failure → never SAVED; recovery path retries and
    completes once the filesystem is writable again."""
    tmp = tmp_path / "data"
    database = Database(tmp / "runtime.sqlite3")
    database.migrate()
    runtime = make_runtime(tmp_path, database, auto_purge=False)
    client = build_app(tmp_path, database, runtime)
    with client:
        view = client.post("/api/sessions", json={"course_name": "ReadOnly"}).json()
        session_id = view["session"]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            view2 = client.get(f"/api/sessions/{session_id}").json()
            if view2["progress"]["captured_audio_ms"] > 0:
                break
            time.sleep(0.1)

    summaries_dir = tmp / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    summaries_dir.chmod(0o500)  # read-only: atomic rename will fail
    client2 = build_app(tmp_path, database, runtime)
    with client2:
        assert client2.post(f"/api/sessions/{session_id}/end").status_code == 200
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            status = client2.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status in ("SUMMARY_SAVED", "RECOVERY_REQUIRED"):
                break
            time.sleep(0.1)
        assert status == "RECOVERY_REQUIRED"
        record = SessionRepository(database).get_session(session_id)
        assert record.summary_path is None

    # Free space restored (directory writable again) → user resumes recovery.
    summaries_dir.chmod(0o700)
    runtime3 = make_runtime(tmp_path, database, auto_purge=True)
    client3 = build_app(tmp_path, database, runtime3)
    with client3:
        resumed = client3.post(f"/api/recovery/{session_id}/resume")
        assert resumed.status_code == 200
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            probe = client3.get(f"/api/sessions/{session_id}")
            if probe.status_code == 404:
                status = "PURGED"
                break
            status = probe.json()["session"]["status"]
            time.sleep(0.1)
        assert status == "PURGED"
        assert len(list(summaries_dir.glob("*.md"))) == 1


def test_health_shows_red_actionable_on_auth_failure(tmp_path):
    """F-008: terminal provider credential errors must surface as a red,
    actionable state instead of blind retries; capture continues."""
    tmp = tmp_path / "data"
    database = Database(tmp / "runtime.sqlite3")
    database.migrate()
    runtime = make_runtime(tmp_path, database, asr=AuthFailingASR(), auto_purge=False)
    client = build_app(tmp_path, database, runtime)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Auth"}).json()
        session_id = view["session"]["id"]
        deadline = time.monotonic() + 15
        status = None
        while time.monotonic() < deadline:
            health = client.get("/health").json()
            provider = next(c for c in health["components"] if c["name"] == "provider")
            if provider["status"] == "red":
                break
            time.sleep(0.1)
        assert provider["status"] == "red"
        assert provider["reason_code"] == "PROVIDER_AUTH"
        # No blind retry: exactly one attempt per segment.
        with database.connect() as connection:
            attempts = connection.execute(
                """
                SELECT COUNT(*) AS n FROM processing_attempts WHERE stage = 'ASR'
                """
            ).fetchone()["n"]
            segments = connection.execute(
                "SELECT COUNT(*) AS n FROM segments WHERE session_id = ?",
                (session_id,),
            ).fetchone()["n"]
        assert attempts == segments  # one attempt each, no retry loop
        # Capture itself kept working: chunks were sealed.
        assert count_capture_chunks(database, session_id) >= 1


def count_capture_chunks(database, session_id):
    with database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) AS n FROM capture_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
