from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from classroom_interpreter.agents.segmentation import SegmenterConfig
from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import SegmentState, SessionStatus, utc_now
from classroom_interpreter.main import create_app
from classroom_interpreter.providers.mocks import MockSummaryProvider
from classroom_interpreter.runtime.pipeline import SessionOrchestrator
from classroom_interpreter.storage import Database, SessionRepository

import helpers_phase3 as h


class FakeASR:
    name = "fake-asr"
    model = "fake-asr-v1"

    async def transcribe(self, segment, *, language: str = "en"):
        from classroom_interpreter.domain.models import TranscriptResult

        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=f"Lecture point {segment.sequence}.",
            provider=self.name,
            model=self.model,
            latency_ms=1,
            audio_seconds=segment.duration_ms / 1000,
            created_at=utc_now(),
        )


class FakeTranslation:
    name = "fake-tr"
    model = "fake-tr-v1"

    async def translate(self, request):
        from classroom_interpreter.domain.models import TranslationResult

        return TranslationResult(
            segment_id=request.segment_id,
            chinese_text=f"要点 {request.sequence}。",
            english_text_sha256=__import__("hashlib").sha256(
                request.current_english_text.encode("utf-8")
            ).hexdigest(),
            provider=self.name,
            model=self.model,
            latency_ms=1,
            created_at=utc_now(),
        )


class FakeListener:
    """Backend-owned capture stand-in: same lifecycle contract as the real
    AudioListener, but writes deterministic chunks immediately on start."""

    def __init__(self, database: Database, root: Path):
        self.database = database
        self.root = root
        self.repository = SessionRepository(database)
        self.session_id: str | None = None
        self.state = "NEW"

    async def start(self, session_id: str) -> None:
        self.session_id = session_id
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self.repository.transition_session(
            session_id, SessionStatus.RECORDING,
            reason_code="FAKE_CAPTURE_START", actor="fake_listener",
        )
        for index in range(2):
            writer = h.build_chunk_writer(self.database, session_dir, session_id)
            blocks = h.envelope_blocks([("silence", 0.5), ("speech", 8.0), ("silence", 1.5)])
            ns = 1_000_000_000 * index * 12
            for block in blocks:
                writer.append(block, ns, "2026-09-08T00:00:00+00:00", 0)
                ns += round(len(block) // 2 / h.RATE * 1_000_000_000)
            writer.seal()
        self.state = "RECORDING"

    def pause(self, session_id: str) -> None:
        self.repository.transition_session(
            session_id, SessionStatus.PAUSED,
            reason_code="FAKE_PAUSE", actor="fake_listener",
        )
        self.state = "PAUSED"

    def resume(self, session_id: str) -> None:
        self.repository.transition_session(
            session_id, SessionStatus.RECORDING,
            reason_code="FAKE_RESUME", actor="fake_listener",
        )
        self.state = "RECORDING"

    def close(self) -> None:
        if self.session_id is None:
            return
        record = self.repository.get_session(self.session_id)
        if record.status in (SessionStatus.RECORDING, SessionStatus.PAUSED):
            self.repository.transition_session(
                self.session_id, SessionStatus.FINALIZING,
                reason_code="FAKE_STOP", actor="fake_listener",
            )
        self.state = "STOPPED"

    def snapshot(self) -> dict:
        return {
            "capture_active": self.state == "RECORDING",
            "state": self.state,
            "reason_code": None,
            "audio_seconds_captured": 0,
        }


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path / "data",
        app_phase="PHASE_5_TEST",
    )


def make_client(
    tmp_path: Path,
    *,
    with_providers=True,
    summary_provider="default",
    auto_purge=False,
):
    settings = make_settings(tmp_path)
    database = Database(tmp_path / "runtime.sqlite3")
    database.migrate()
    asr = FakeASR() if with_providers else None
    translation = FakeTranslation() if with_providers else None
    if summary_provider == "default":
        summary = MockSummaryProvider() if with_providers else None
    else:
        summary = summary_provider
    runtime = SessionOrchestrator(
        settings,
        database,
        asr_provider=asr,
        translation_provider=translation,
        summary_provider=summary,
        listener_factory=lambda root: FakeListener(database, root),
        auto_purge=auto_purge,
    )
    app = create_app(settings, database, runtime=runtime)
    client = TestClient(app)
    return client, database, runtime


def receive_json_or_fail(ws, timeout=10.0, what="websocket frame"):
    """`ws.receive_json()` with a deadline.

    Starlette's test websocket blocks forever on an empty receive queue. A
    lost-event regression (the replay->subscribe race) would therefore hang
    the suite instead of failing it, which hides the bug in CI. Failing on a
    deadline keeps the race test red rather than stuck.
    """
    # A daemon thread, deliberately not a ThreadPoolExecutor: the pool joins
    # its workers at shutdown (and at interpreter exit), which would re-hang
    # on the very frame that never arrives. A daemon thread parked on the
    # queue cannot block teardown.
    inbox: "queue.Queue" = queue.Queue(maxsize=1)

    def pump() -> None:
        try:
            inbox.put(("ok", ws.receive_json()))
        except BaseException as exc:  # surfaced below, never swallowed
            inbox.put(("error", exc))

    threading.Thread(target=pump, daemon=True, name="ws-receive").start()
    try:
        kind, value = inbox.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"timed out after {timeout}s waiting for {what}")
    if kind == "error":
        raise value
    return value


def wait_for_status(client, session_id, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{session_id}")
        last = response.json()
        if last["session"]["status"] in statuses:
            return last
        time.sleep(0.1)
    return last


def test_full_live_flow_start_to_drain(tmp_path):
    client, database, runtime = make_client(tmp_path)
    with client:
        response = client.post(
            "/api/sessions", json={"course_name": "Risk Management"}
        )
        assert response.status_code == 201
        view = response.json()
        session_id = view["session"]["id"]
        assert view["session"]["status"] == "RECORDING"

        # Browser refresh cannot stop capture: the listener lives in the app.
        assert view["progress"]["capture_chunks"] >= 1

        pause = client.post(f"/api/sessions/{session_id}/pause")
        assert pause.status_code == 200
        assert pause.json()["status"] == "PAUSED"
        # Idempotent pause.
        assert client.post(f"/api/sessions/{session_id}/pause").json()["status"] == "PAUSED"

        resume = client.post(f"/api/sessions/{session_id}/resume")
        assert resume.json()["status"] == "RECORDING"

        end = client.post(f"/api/sessions/{session_id}/end")
        assert end.status_code == 200
        assert end.json()["status"] in ("FINALIZING", "SUMMARIZING")

        final = wait_for_status(
            client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED")
        )
        assert final["session"]["status"] == "SUMMARY_SAVED"
        assert final["progress"]["pending_segments"] == 0
        assert final["progress"]["failed_segments"] == 0
        history = final["committed_history"]
        assert len(history) >= 2
        assert [item["sequence"] for item in history] == sorted(
            item["sequence"] for item in history
        )
        assert history[0]["chinese_text"].startswith("要点")

        # End Class is idempotent.
        again = client.post(f"/api/sessions/{session_id}/end")
        assert again.status_code == 200


def test_second_start_is_rejected_while_active(tmp_path):
    client, database, runtime = make_client(tmp_path)
    with client:
        first = client.post("/api/sessions", json={"course_name": "A"})
        assert first.status_code == 201
        second = client.post("/api/sessions", json={"course_name": "B"})
        assert second.status_code == 409
        assert second.json()["detail"] == "SINGLE_ACTIVE_SESSION"


def test_start_without_providers_is_refused(tmp_path):
    client, database, runtime = make_client(tmp_path, with_providers=False)
    with client:
        response = client.post("/api/sessions", json={"course_name": "A"})
        assert response.status_code == 503
        assert response.json()["detail"] == "PROVIDER_NOT_CONFIGURED"


def test_websocket_replay_is_deterministic_and_ordered(tmp_path):
    client, database, runtime = make_client(tmp_path)
    with client:
        view = client.post("/api/sessions", json={"course_name": "WS"}).json()
        session_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        final = wait_for_status(
            client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED")
        )
        assert final["session"]["status"] == "SUMMARY_SAVED"

        with client.websocket_connect(f"/ws/sessions/{session_id}?cursor=-1") as ws:
            ready = ws.receive_json()
            assert ready["kind"] == "system.ready"
            events = [ready]
            # Drain until no more data within a short window.
            ws.send_text("ping")
            got_pong = False
            seen_sequences = []
            while True:
                message = ws.receive_json()
                if message["kind"] == "system.pong":
                    got_pong = True
                    break
                seen_sequences.append(message["event_sequence"])
            assert got_pong
            assert seen_sequences == sorted(seen_sequences)
            committed = [
                s for s in seen_sequences
            ]
            max_seq = max(seen_sequences)

        # Reconnect from the last-seen cursor: no duplicate delivery.
        with client.websocket_connect(
            f"/ws/sessions/{session_id}?cursor={max_seq}"
        ) as ws:
            ws.receive_json()  # ready
            ws.send_text("ping")
            while True:
                message = ws.receive_json()
                if message["kind"] == "system.pong":
                    break
                assert False, f"unexpected replayed event: {message}"


def test_websocket_buffers_event_published_after_replay_query_before_stream(
    tmp_path, monkeypatch
):
    """Deterministically inject the exact reviewer replay→subscribe race."""
    import classroom_interpreter.api.websocket as websocket_module

    client, database, runtime = make_client(tmp_path)
    repository = SessionRepository(database)
    with client:
        session = repository.create_session("WS race", app_version="test")
        cursor = runtime.publisher.publish(
            session.id, "session.state", {"marker": "baseline"}
        )
        original_replay = websocket_module._replay

        def replay_then_publish(database_arg, session_id, replay_cursor):
            replayed = original_replay(database_arg, session_id, replay_cursor)
            runtime.publisher.publish(
                session_id, "session.state", {"marker": "race-window"}
            )
            return replayed

        monkeypatch.setattr(websocket_module, "_replay", replay_then_publish)
        with client.websocket_connect(
            f"/ws/sessions/{session.id}?cursor={cursor}"
        ) as ws:
            ready = receive_json_or_fail(ws, what="system.ready")
            assert ready["kind"] == "system.ready"
            # Mutation that turns this red: subscribe after `_replay` instead
            # of before it. The injected event then falls in the gap and is
            # never delivered -- caught here as a timeout, not a hang.
            injected = receive_json_or_fail(ws, what="event published mid-replay")
            assert injected["event_sequence"] == cursor + 1
            assert injected["payload"] == {"marker": "race-window"}
            # A pong (not a second copy of the injected event) proves the
            # buffered replay did not also duplicate it.
            ws.send_text("ping")
            assert receive_json_or_fail(ws, what="system.pong")["kind"] == "system.pong"


def test_health_reflects_components(tmp_path):
    client, database, runtime = make_client(tmp_path)
    with client:
        snapshot = client.get("/health").json()
        names = {c["name"] for c in snapshot["components"]}
        assert {"microphone", "storage", "asr", "translation", "provider"} <= names
        capture = client.get("/health/capture").json()
        assert capture["capture_active"] is False


def test_recovery_lists_and_resumes(tmp_path):
    client, database, runtime = make_client(tmp_path)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Rec"}).json()
        session_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        wait_for_status(client, session_id, ("SUMMARY_SAVED",))

        repository = SessionRepository(database)
        # Simulate a crash-style recovery state.
        repository.transition_session(
            session_id, SessionStatus.RECOVERY_REQUIRED,
            reason_code="TEST_RECOVERY", actor="test",
        )
        listed = client.get("/api/recovery").json()
        assert any(s["id"] == session_id for s in listed["sessions"])

        resumed = client.post(f"/api/recovery/{session_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "FINALIZING"
        final = wait_for_status(client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED"))
        assert final["session"]["status"] == "SUMMARY_SAVED"

