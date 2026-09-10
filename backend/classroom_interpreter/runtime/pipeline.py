from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from classroom_interpreter.agents.segmentation import (
    CorruptCaptureChunk,
    SegmentAgent,
    SegmenterConfig,
)
from classroom_interpreter.agents.health import HealthMetricsStore, HealthMonitorAgent
from classroom_interpreter.agents.transcription import TranscriptionAgent
from classroom_interpreter.agents.translation import TranslationAgent, TranslationSequencer
from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import (
    SessionRecord,
    SessionStatus,
    WebSocketEventKind,
)
from classroom_interpreter.storage import Database, SessionRepository
from classroom_interpreter.logging import MetadataLogEvent, log_metadata

from .events import EventPublisher


class SingleActiveSessionError(RuntimeError):
    pass


class SessionNotFoundError(RuntimeError):
    pass


class CaptureStartError(RuntimeError):
    pass


class SessionOrchestrator:
    """Coordinates the single active session's lifecycle and its worker loops.

    The orchestrator owns state transitions and worker supervision only; it
    never performs capture, ASR, translation, summary, or cleanup itself
    (AGENTS.md §5). Durable rows are the only queue; asyncio tasks are
    wake-up hints (D-025). The microphone lives in the backend listener, so a
    browser refresh cannot stop capture (D-002).
    """

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        asr_provider,
        translation_provider,
        summary_provider=None,
        listener_factory=None,
        asr_worker_count: int = 2,
        translation_worker_count: int = 2,
        auto_purge: bool = True,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = SessionRepository(database)
        self.publisher = EventPublisher(database)
        self.asr_provider = asr_provider
        self.translation_provider = translation_provider
        self.summary_provider = summary_provider
        self.listener_factory = listener_factory
        self.asr_worker_count = asr_worker_count
        self.translation_worker_count = translation_worker_count
        self.auto_purge = auto_purge
        self.active_session: str | None = None
        self.listener = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._finalize_task: asyncio.Task | None = None
        self.last_finalization_error: str | None = None
        self.worker_faults: dict[str, dict[str, int | str]] = {}
        self.health_store = HealthMetricsStore(
            settings.health_database_path,
            max_buffered=settings.health_buffer_max_samples,
        )
        self.health_monitor = HealthMonitorAgent(
            self.health_store,
            self,
            interval_seconds=settings.health_flush_interval_seconds,
        )

    # ------------------------------------------------------------- lifecycle

    def _default_listener_factory(capture_root: Path):
        from classroom_interpreter.audio.listener import AudioListener

        return AudioListener(_factory_database, capture_root)

    def session_store(self, session_id: str) -> Path:
        return (
            self.settings.classroom_interpreter_data_dir
            / "sessions"
            / session_id
        )

    async def start_class(self, course_name: str) -> SessionRecord:
        if self.active_session is not None:
            raise SingleActiveSessionError("a session is already active")
        if self.asr_provider is None or self.translation_provider is None:
            raise CaptureStartError(
                "MIMO_API_KEY is not configured; provider readiness is required"
            )
        record = self.repository.create_session(
            course_name, app_version=self.settings.app_version
        )
        session_id = record.id
        capture_root = self.session_store(session_id) / "capture"
        try:
            if self.listener_factory is not None:
                self.listener = self.listener_factory(capture_root)
            else:
                from classroom_interpreter.audio.listener import AudioListener

                self.listener = AudioListener(self.database, capture_root)
            await self.listener.start(session_id)
        except Exception:
            self.listener = None
            raise
        self.active_session = session_id
        self.last_finalization_error = None
        self._stop.clear()
        self._spawn_workers()
        self.publisher.publish(
            session_id,
            WebSocketEventKind.SESSION_STATE,
            self._state_payload(session_id),
        )
        return self.repository.get_session(session_id)

    def _build_workers(self, session_id: str) -> None:
        session_store = self.session_store(session_id)
        segment_config = SegmenterConfig(
            min_seconds=self.settings.segment_min_seconds,
            max_seconds=self.settings.segment_max_seconds,
            silence_milliseconds=self.settings.segment_silence_milliseconds,
            overlap_milliseconds=self.settings.segment_overlap_milliseconds,
            threshold_rms=self.settings.vad_threshold_rms,
        )
        self._segmenter = SegmentAgent(
            self.database, session_store, session_id, segment_config
        )
        self._asr_workers = [
            TranscriptionAgent(
                self.database,
                self.asr_provider,
                worker_id=f"asr-{index + 1}",
                lease_seconds=self.settings.asr_lease_seconds,
                max_attempts=self.settings.asr_max_attempts,
                backoff_base_seconds=self.settings.asr_backoff_base_seconds,
                backoff_cap_seconds=self.settings.asr_backoff_cap_seconds,
                request_timeout_seconds=self.settings.asr_request_timeout_seconds,
                jitter_fraction=0.0,
                event_sink=self._event_sink,
            )
            for index in range(self.asr_worker_count)
        ]
        self._translation_workers = [
            TranslationAgent(
                self.database,
                self.translation_provider,
                worker_id=f"translation-{index + 1}",
                lease_seconds=self.settings.translation_lease_seconds,
                max_attempts=self.settings.translation_max_attempts,
                backoff_base_seconds=self.settings.translation_backoff_base_seconds,
                backoff_cap_seconds=self.settings.translation_backoff_cap_seconds,
                request_timeout_seconds=self.settings.translation_request_timeout_seconds,
                context_max_segments=self.settings.context_max_segments,
                context_max_seconds=self.settings.context_max_seconds,
                context_max_tokens=self.settings.context_max_tokens,
                jitter_fraction=0.0,
            )
            for index in range(self.translation_worker_count)
        ]
        self._translation_sequencer = TranslationSequencer(
            self.database, event_sink=self._event_sink
        )
        # Compatibility handles for focused tests and finalization diagnostics.
        self._asr_worker = self._asr_workers[0]
        self._translation_worker = self._translation_workers[0]

    def _spawn_workers(self) -> None:
        self._build_workers(self.active_session)
        self._tasks = [
            asyncio.create_task(self._segmenter_loop(), name="segmenter-loop"),
            *[
                asyncio.create_task(self._asr_loop(worker), name=f"asr-loop-{index + 1}")
                for index, worker in enumerate(self._asr_workers)
            ],
            *[
                asyncio.create_task(
                    self._translation_loop(worker),
                    name=f"translation-loop-{index + 1}",
                )
                for index, worker in enumerate(self._translation_workers)
            ],
            asyncio.create_task(self._sequencer_loop(), name="translation-sequencer-loop"),
        ]

    def _event_sink(self, kind: str, payload: dict) -> None:
        if self.active_session is None:
            return
        self.publisher.publish(self.active_session, kind, payload)

    async def _segmenter_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._segmenter.process_available_chunks()
            except CorruptCaptureChunk as exc:
                self.last_finalization_error = "CORRUPT_CAPTURE_CHUNK"
                if self.active_session:
                    self.repository.update_session_error(
                        self.active_session,
                        category="MALFORMED",
                        safe_message="a sealed capture chunk failed verification",
                    )
                self._record_worker_fault("segmenter", exc)
            except Exception as exc:
                self._record_worker_fault("segmenter", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    async def _asr_loop(self, worker=None) -> None:
        worker = worker or self._asr_worker
        while not self._stop.is_set():
            try:
                worked = await worker.step()
            except Exception as exc:
                self._record_worker_fault(worker.worker_id, exc)
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.3)
            except asyncio.TimeoutError:
                continue

    async def _translation_loop(self, worker=None) -> None:
        worker = worker or self._translation_worker
        while not self._stop.is_set():
            try:
                worked = await worker.step()
            except Exception as exc:
                self._record_worker_fault(worker.worker_id, exc)
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.3)
            except asyncio.TimeoutError:
                continue

    async def _sequencer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = await self._translation_sequencer.step()
            except Exception as exc:
                self._record_worker_fault("translation-sequencer", exc)
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

    def _record_worker_fault(self, agent_name: str, exc: Exception) -> None:
        prior = self.worker_faults.get(agent_name, {})
        self.worker_faults[agent_name] = {
            "count": int(prior.get("count", 0)) + 1,
            "error_type": type(exc).__name__,
        }
        self.health_store.record_heartbeat(
            agent_name, "degraded", error_code=type(exc).__name__
        )
        self.health_store.record_metric("worker.exception_count", 1.0)
        log_metadata(
            logging.getLogger("classroom_interpreter.runtime"),
            MetadataLogEvent(
                agent=agent_name,
                event="worker_step_exception",
                status="degraded",
                session_id=self.active_session,
                error_code=type(exc).__name__,
            ),
        )

    async def pause(self, session_id: str) -> SessionRecord:
        self._assert_active(session_id)
        record = self.repository.get_session(session_id)
        if record.status is SessionStatus.PAUSED:
            return record  # idempotent command
        if record.status is not SessionStatus.RECORDING:
            raise RuntimeError(f"PAUSE_INVALID_STATE:{record.status.value}")
        await asyncio.to_thread(self.listener.pause, session_id)
        self.publisher.publish(
            session_id, WebSocketEventKind.SESSION_STATE, self._state_payload(session_id)
        )
        return self.repository.get_session(session_id)

    async def resume(self, session_id: str) -> SessionRecord:
        self._assert_active(session_id)
        record = self.repository.get_session(session_id)
        if record.status is SessionStatus.RECORDING:
            return record  # idempotent command
        if record.status is not SessionStatus.PAUSED:
            raise RuntimeError(f"RESUME_INVALID_STATE:{record.status.value}")
        await asyncio.to_thread(self.listener.resume, session_id)
        self.publisher.publish(
            session_id, WebSocketEventKind.SESSION_STATE, self._state_payload(session_id)
        )
        return self.repository.get_session(session_id)

    async def end_class(self, session_id: str) -> SessionRecord:
        """Stop new capture and start the drain in the background.

        The endpoint returns immediately with the session FINALIZING; progress
        is visible via GET /api/sessions/{id} and session.state events.
        Idempotent: ending an already-finalizing/finalized session is a no-op.
        """
        try:
            current = self.repository.get_session(session_id).status
        except KeyError:
            raise SessionNotFoundError(session_id) from None
        if current is not SessionStatus.RECORDING and current is not SessionStatus.PAUSED:
            return self.repository.get_session(session_id)
        self._assert_active(session_id)
        if self._finalize_task is not None and not self._finalize_task.done():
            return self.repository.get_session(session_id)
        await asyncio.to_thread(self.listener.close)
        await self.stop_workers_for_finalization()
        self._finalize_task = asyncio.create_task(
            self._finalize(session_id), name="finalize-drain"
        )
        return self.repository.get_session(session_id)

    async def stop_workers_for_finalization(self) -> int:
        """Stop every worker task, then hand back the leases they died holding.

        A worker cancelled inside a provider call leaves its segment leased
        for `*_lease_seconds` (120 s). The finalization drain budget is also
        120 s, so without this the drain can spend its whole budget waiting on
        leases whose owners are already gone and then summarize an incomplete
        transcript. Cancellation is awaited first, so every lease released here
        is provably stale (D-034).
        """
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        worker_ids = [worker.worker_id for worker in getattr(self, "_asr_workers", [])]
        worker_ids += [
            worker.worker_id for worker in getattr(self, "_translation_workers", [])
        ]
        return self.repository.expire_leases_for_workers(worker_ids)

    async def _finalize(self, session_id: str) -> None:
        """Drain every persisted segment to DONE, then hand over to summary.

        Any terminal segment failure moves the session to RECOVERY_REQUIRED —
        a knowingly incomplete class never advances (D-006). The runtime slot
        is released when finalization ends so recovery or a new class can use
        it.
        """
        try:
            segmenter = getattr(self, "_segmenter", None)
            asr_workers = getattr(self, "_asr_workers", [])
            translation_workers = getattr(self, "_translation_workers", [])
            sequencer = getattr(self, "_translation_sequencer", None)
            drain_deadline = time.monotonic() + 120.0
            while time.monotonic() < drain_deadline:
                progressed = False
                if segmenter is not None:
                    try:
                        segmenter.process_available_chunks()
                    except CorruptCaptureChunk:
                        self.last_finalization_error = "CORRUPT_CAPTURE_CHUNK"
                        break
                for asr in asr_workers:
                    if await asr.step():
                        progressed = True
                for translation in translation_workers:
                    if await translation.step():
                        progressed = True
                if sequencer is not None:
                    while await sequencer.step():
                        progressed = True
                if segmenter is not None:
                    segmenter.seal_tail()
                counts = self._pending_counts(session_id)
                if counts["pending"] == 0 and not progressed:
                    break
                if not progressed:
                    # Work remains but is backing off (retry delays): wait for
                    # the durable retry schedule instead of spinning (F-003..).
                    await asyncio.sleep(0.2)
            if segmenter is not None:
                segmenter.seal_tail()
            # Final states check.
            pending = self._pending_counts(session_id)
            failed = pending["failed"]
            if failed:
                current = self.repository.get_session(session_id).status
                if current is SessionStatus.FINALIZING:
                    self.repository.transition_session(
                        session_id,
                        SessionStatus.RECOVERY_REQUIRED,
                        reason_code="TERMINAL_SEGMENT_FAILURE",
                        actor="orchestrator",
                    )
            else:
                current = self.repository.get_session(session_id).status
                if current is SessionStatus.FINALIZING:
                    self.repository.transition_session(
                        session_id,
                        SessionStatus.SUMMARIZING,
                        reason_code="DRAIN_COMPLETE",
                        actor="orchestrator",
                    )
            self.publisher.publish(
                session_id, WebSocketEventKind.SESSION_STATE, self._state_payload(session_id)
            )
            await self._run_summary(session_id)
            self.publisher.publish(
                session_id, WebSocketEventKind.SESSION_STATE, self._state_payload(session_id)
            )
        except Exception as exc:
            self.last_finalization_error = type(exc).__name__
            current = self.repository.get_session(session_id).status
            if current is SessionStatus.FINALIZING:
                self.repository.transition_session(
                    session_id,
                    SessionStatus.RECOVERY_REQUIRED,
                    reason_code="FINALIZATION_ERROR",
                    actor="orchestrator",
                )
        finally:
            if self.active_session == session_id:
                self.active_session = None

    async def _run_summary(self, session_id: str) -> None:
        """Run the final summary after a complete drain. Invalid/failed summary
        output never reaches SUMMARY_SAVED: the session goes RECOVERY_REQUIRED
        and the purge gate stays closed (D-006, D-030)."""
        current = self.repository.get_session(session_id).status
        if current is not SessionStatus.SUMMARIZING:
            return
        if self.summary_provider is None:
            self.repository.update_session_error(
                session_id,
                category="CONFIGURATION",
                safe_message="summary provider is unavailable",
            )
            self.repository.transition_session(
                session_id,
                SessionStatus.RECOVERY_REQUIRED,
                reason_code="SUMMARY_PROVIDER_UNAVAILABLE",
                actor="orchestrator",
            )
            return
        from classroom_interpreter.agents.summary import SummaryAgent

        summary = SummaryAgent(
            self.database,
            self.summary_provider,
            summaries_root=self.settings.classroom_interpreter_data_dir / "summaries",
            session_store_root=self.settings.classroom_interpreter_data_dir / "sessions",
            chunk_token_budget=self.settings.summary_chunk_token_budget,
            max_attempts=self.settings.summary_max_attempts,
        )
        try:
            await summary.summarize_async(session_id)
        except Exception as exc:
            self.last_finalization_error = type(exc).__name__
            self.repository.update_session_error(
                session_id,
                category="SUMMARY",
                safe_message=f"summary failed: {type(exc).__name__}",
            )
            current = self.repository.get_session(session_id).status
            if current is SessionStatus.SUMMARIZING:
                self.repository.transition_session(
                    session_id,
                    SessionStatus.RECOVERY_REQUIRED,
                    reason_code="SUMMARY_FAILURE",
                    actor="orchestrator",
                )
            return
        if self.auto_purge:
            await self._auto_purge(session_id)

    async def _auto_purge(self, session_id: str) -> None:
        """Automatic purge after a verified summary save (PRODUCT journey).
        A refused purge leaves the session recoverable; the saved summary is
        never endangered."""
        from classroom_interpreter.agents.cleanup import CleanupAgent, PurgeRefused

        cleanup = self._make_cleanup()
        try:
            cleanup.purge(session_id)
        except PurgeRefused as exc:
            self.last_finalization_error = f"PURGE_REFUSED:{exc}"
            current = self.repository.get_session(session_id).status
            if current is SessionStatus.SUMMARY_SAVED:
                self.repository.transition_session(
                    session_id,
                    SessionStatus.RECOVERY_REQUIRED,
                    reason_code="PURGE_REFUSED",
                    actor="orchestrator",
                )
        except Exception as exc:
            self.last_finalization_error = type(exc).__name__
            current = self.repository.get_session(session_id).status
            if current in (SessionStatus.SUMMARY_SAVED, SessionStatus.PURGING):
                self.repository.transition_session(
                    session_id,
                    SessionStatus.RECOVERY_REQUIRED,
                    reason_code="PURGE_ERROR",
                    actor="orchestrator",
                )

    def _make_cleanup(self):
        from classroom_interpreter.agents.cleanup import CleanupAgent

        return CleanupAgent(
            self.database,
            summaries_root=self.settings.classroom_interpreter_data_dir / "summaries",
            session_store_root=self.settings.classroom_interpreter_data_dir / "sessions",
        )

    def delete_session(self, session_id: str) -> dict:
        """User-requested deletion of an unfinished session (explicit,
        destructive, never automatic)."""
        if self.active_session == session_id:
            from classroom_interpreter.agents.cleanup import PurgeRefused

            raise PurgeRefused(
                "ACTIVE_RUNTIME_OWNERSHIP: stop and enter recovery before deletion"
            )
        return self._make_cleanup().delete_unfinished(session_id)

    # ------------------------------------------------------------ recovery

    def reconcile_on_startup(self) -> int:
        """Backend restart reconciliation (F-013): a session that was
        RECORDING/PAUSED cannot own the microphone anymore — mark it
        RECOVERY_REQUIRED so the user sees the resume choice. Draining states
        (FINALIZING/SUMMARIZING/SUMMARY_SAVED) keep their durable progress and
        are resumable as-is."""
        moved = 0
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM sessions WHERE status IN ('RECORDING', 'PAUSED')"
            ).fetchall()
        for row in rows:
            try:
                self.repository.transition_session(
                    row["id"],
                    SessionStatus.RECOVERY_REQUIRED,
                    reason_code="BACKEND_RESTART",
                    actor="recovery_agent",
                )
                moved += 1
            except Exception:
                continue
        return moved

    def unfinished_sessions(self) -> list[SessionRecord]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM sessions
                WHERE status NOT IN ('PURGED')
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self.repository.get_session(row["id"]) for row in rows]

    async def recover(self, session_id: str) -> SessionRecord:
        """Resume an unfinished session's finalization (drain existing work)."""
        record = self.repository.get_session(session_id)
        if record.status is not SessionStatus.RECOVERY_REQUIRED:
            return record
        if self.active_session is not None:
            raise SingleActiveSessionError("a session is already active")
        self._build_workers(session_id)
        # A process crash invalidates open VAD buffers. Rebuild from durable
        # capture after the contiguous DONE prefix before changing lifecycle.
        self._segmenter.rebuild_unconfirmed_tail()
        self.repository.transition_session(
            session_id,
            SessionStatus.FINALIZING,
            reason_code="USER_RECOVERY_RESUME",
            actor="orchestrator",
        )
        self.active_session = session_id
        self._finalize_task = asyncio.create_task(
            self._finalize(session_id), name="recovery-drain"
        )
        return self.repository.get_session(session_id)

    # -------------------------------------------------------------- status

    def progress(self, session_id: str) -> dict:
        with self.database.connect() as connection:
            counts = connection.execute(
                """
                SELECT state, COUNT(*) AS n FROM segments WHERE session_id = ?
                GROUP BY state
                """,
                (session_id,),
            ).fetchall()
            captured_end = connection.execute(
                "SELECT COALESCE(MAX(end_ms), 0) AS v FROM segments WHERE session_id = ?",
                (session_id,),
            ).fetchone()["v"]
            done_end = connection.execute(
                """
                SELECT COALESCE(MAX(s.end_ms), 0) AS v FROM segments s
                WHERE s.session_id = ? AND s.state = 'DONE'
                """,
                (session_id,),
            ).fetchone()["v"]
            chunk_count = connection.execute(
                "SELECT COUNT(*) AS v FROM capture_chunks WHERE session_id = ?",
                (session_id,),
            ).fetchone()["v"]
        by_state = {row["state"]: row["n"] for row in counts}
        pending = by_state.get("ASR_PENDING", 0) + by_state.get("ASR_PROCESSING", 0) \
            + by_state.get("ASR_RETRY", 0) + by_state.get("TRANSLATION_PENDING", 0) \
            + by_state.get("TRANSLATION_PROCESSING", 0) + by_state.get("TRANSLATION_RETRY", 0)
        return {
            "segment_counts": by_state,
            "pending_segments": pending,
            "failed_segments": by_state.get("ASR_FAILED", 0)
            + by_state.get("TRANSLATION_FAILED", 0),
            "captured_audio_ms": int(captured_end),
            "processed_audio_ms": int(done_end),
            "live_delay_ms": max(0, int(captured_end) - int(done_end)),
            "capture_chunks": chunk_count,
        }

    def _state_payload(self, session_id: str) -> dict:
        record = self.repository.get_session(session_id)
        return {
            "status": record.status.value,
            "course_name": record.course_name,
            "progress": self.progress(session_id),
        }

    def _pending_counts(self, session_id: str) -> dict:
        counts = self.progress(session_id)
        return {
            "pending": counts["pending_segments"],
            "failed": counts["failed_segments"],
        }

    def _assert_active(self, session_id: str) -> None:
        if session_id != self.active_session:
            raise SessionNotFoundError("session is not owned by this runtime")

    def attach_existing_listener(self, listener) -> None:
        self.listener = listener


_factory_database = None


def bind_database(database: Database) -> None:
    global _factory_database
    _factory_database = database
