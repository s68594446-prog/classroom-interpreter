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

from .caption_order import CaptionOrderGate
from .events import EventPublisher


class SingleActiveSessionError(RuntimeError):
    pass


class SessionNotFoundError(RuntimeError):
    pass


class CaptureStartError(RuntimeError):
    pass


# Below this an interim transcript is a fragment, not a phrase. Measured on
# real classroom audio: under ~2.5s of speech the transcript is a single token
# like "8." or "Age.", which reads as fact and is not one.
_MIN_PARTIAL_WORDS = 2


def _has_moved_enough(previous: str, current: str, min_new_words: int) -> bool:
    """Has the interim English changed enough to be worth re-translating?

    Every interim translation is a paid call, and the loop sees the same
    sentence growing word by word. Re-translating for one extra word spends
    money to change nothing the reader would notice. A shrink or a rewrite --
    the transcriber revising what it heard -- always counts as movement.
    """
    if not previous:
        return True
    if not current.startswith(previous):
        # Not an extension of what we translated -- the transcriber changed its
        # mind about earlier words ("the agents coming" -> "the agent
        # commission"). That is exactly when a stale Chinese line is worst.
        return True
    return len(current.split()) - len(previous.split()) >= min_new_words


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
        # Set when the segmenter's silence run triggered an automatic pause, so
        # the UI can distinguish it from a pause the user pressed.
        self.auto_paused_at_silence_seconds: float | None = None
        self.worker_faults: dict[str, dict[str, int | str]] = {}
        self._caption_gate = CaptionOrderGate(self._publish_caption)
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

                self.listener = AudioListener(
                    self.database,
                    capture_root,
                    chunk_seconds=self.settings.capture_chunk_seconds,
                )
            await self.listener.start(session_id)
        except Exception:
            self.listener = None
            raise
        self.active_session = session_id
        self.last_finalization_error = None
        self.auto_paused_at_silence_seconds = None
        # Segment sequences restart at 0 for every session, so the gate has to
        # as well or the first caption of a new class would be held forever
        # behind the sequence numbers of the previous one.
        self._caption_gate = CaptionOrderGate(self._publish_caption)
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
            # This was configured, validated, and never passed here.
            preferred_seconds=self.settings.segment_preferred_seconds,
            preferred_silence_milliseconds=(
                self.settings.segment_preferred_silence_milliseconds
            ),
            noise_floor_multiplier=self.settings.vad_noise_floor_multiplier,
            voiced_onset_milliseconds=self.settings.vad_voiced_onset_milliseconds,
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
        if self.settings.partial_transcription_interval_seconds:
            self._tasks.append(
                asyncio.create_task(self._partial_loop(), name="partial-transcription-loop")
            )

    def _event_sink(self, kind: str, payload: dict) -> None:
        if self.active_session is None:
            return
        # English captions are published straight from whichever ASR worker
        # finishes first, so with concurrent workers a later sentence can
        # overtake an earlier one on screen. Translations never had this
        # problem: they pass through the durable ordered cursor. The gate gives
        # the English stream the same guarantee without giving up its speed.
        if kind == WebSocketEventKind.SUBTITLE_CURRENT and "sequence" in payload:
            self._caption_gate.offer(int(payload["sequence"]), payload)
            return
        self.publisher.publish(self.active_session, kind, payload)

    def _publish_caption(self, payload: dict) -> None:
        if self.active_session is None:
            return
        self.publisher.publish(
            self.active_session, WebSocketEventKind.SUBTITLE_CURRENT, payload
        )

    async def _segmenter_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._segmenter.process_available_chunks()
                # A caption held behind a gap that never closes must still
                # reach the screen; nothing else would release it if the
                # missing segment is the last one to fail.
                self._caption_gate.tick()
                await self._maybe_auto_pause()
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
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.segmenter_poll_seconds
                )
            except asyncio.TimeoutError:
                continue

    async def _partial_loop(self) -> None:
        """Transcribe the sentence still being spoken, for display only.

        Without this the first caption waits for the segment to seal: ~6s of
        speech plus ~1s of ASR. Transcribing the open utterance every couple of
        seconds puts English on screen roughly 2s behind the speaker, and the
        sealed segment still replaces it when it arrives.

        Everything here is deliberately outside the durable pipeline. Partial
        text is never written to the database, never sequenced, never
        translated, and never reaches the summary -- so `PERSIST FIRST,
        PROCESS SECOND` and English-as-source-of-truth are untouched. A partial
        that is wrong, late, or lost costs a moment of display, nothing more,
        which is why every failure below is swallowed rather than recorded as a
        pipeline fault.
        """
        interval = self.settings.partial_transcription_interval_seconds
        min_ms = self.settings.partial_transcription_min_audio_ms
        translate_every = self.settings.partial_translation_interval_seconds
        min_new_words = self.settings.partial_translation_min_new_words
        last_text = ""
        last_translated_text = ""
        last_translated_at = 0.0
        last_transcribed_at = 0.0
        # Poll far more often than we transcribe. Sleeping a whole interval
        # between checks meant an utterance that became long enough just after
        # a check waited most of another interval before anyone looked -- up to
        # two seconds of pure phase delay in front of the FIRST caption of every
        # sentence, which is the one the reader is waiting on. The number of
        # paid calls is unchanged: it is still governed by `interval` below.
        poll = min(0.25, interval) if interval else 0.25
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll)
                return
            except asyncio.TimeoutError:
                pass
            session_id = self.active_session
            if session_id is None or self.asr_provider is None:
                last_text = ""
                continue
            try:
                if self.repository.get_session(session_id).status is not SessionStatus.RECORDING:
                    last_text = ""
                    continue
                wav = self._segmenter.pending_utterance_wav(min_ms=min_ms)
                if not wav:
                    last_text = ""
                    last_transcribed_at = 0.0
                    continue
                now = time.monotonic()
                # The first partial of an utterance goes as soon as there is
                # enough audio; after that, one per interval.
                if last_text and now - last_transcribed_at < interval:
                    continue
                last_transcribed_at = now
                text = await self.asr_provider.transcribe_bytes(wav)
                if not text or text == last_text:
                    continue
                # Real classroom audio produces one-word fragments ("8.",
                # "Age.") before there is enough speech to transcribe. Showing
                # those is worse than showing nothing: the reader believes them.
                if len(text.split()) < _MIN_PARTIAL_WORDS:
                    continue
                last_text = text
                # Ephemeral on purpose: not persisted, not sequenced, so a
                # reconnect never replays stale interim text.
                self.publisher.publish_ephemeral(
                    session_id,
                    WebSocketEventKind.SUBTITLE_PARTIAL,
                    {"english_text": text},
                )
                # English goes out first and alone. Waiting for the Chinese
                # would make the English caption as slow as the translation and
                # give up the whole point of transcribing early.
                if not translate_every:
                    continue
                now = time.monotonic()
                if now - last_translated_at < translate_every:
                    continue
                if not _has_moved_enough(last_translated_text, text, min_new_words):
                    continue
                chinese = await self.translation_provider.translate_text(text)
                if not chinese:
                    continue
                last_translated_text = text
                last_translated_at = time.monotonic()
                self.publisher.publish_ephemeral(
                    session_id,
                    WebSocketEventKind.SUBTITLE_PARTIAL,
                    {"english_text": text, "chinese_text": chinese},
                )
            except Exception as exc:
                # Interim display only; a failed partial must never disturb the
                # durable pipeline or surface as a session error. But it must
                # not be invisible either: interim transcription returned empty
                # text on every call for its entire existence because of an
                # AttributeError swallowed exactly here, and nothing anywhere
                # said so. Counting it is content-free and makes a dead feature
                # show up in /health instead of looking like silence.
                self._record_worker_fault("partial-transcription", exc)
                continue

    async def _maybe_auto_pause(self) -> None:
        """Pause recording after a long quiet stretch.

        A class that ended, a break, or a microphone left running should not
        keep burning disk and provider quota. This reads the segmenter's own
        durable silence run rather than a wall clock, so it measures silence in
        *captured audio* -- a stalled or restarted pipeline cannot fake it, and
        a restart resumes the count instead of resetting it.

        Only fires while RECORDING, and pause is idempotent, so a loop
        iteration that runs again before the state change lands cannot pause
        twice. Audio already captured is untouched: pause seals the open
        utterance, it never discards.
        """
        threshold = self.settings.auto_pause_silence_seconds
        if not threshold or self.active_session is None:
            return
        if self._segmenter.idle_silence_ms() < threshold * 1000:
            return
        session_id = self.active_session
        record = self.repository.get_session(session_id)
        if record.status is not SessionStatus.RECORDING:
            return
        # pause() records the state transition and publishes SESSION_STATE
        # itself; this only adds why it happened, so the UI can say "paused
        # automatically" rather than looking like the user did it.
        await self.pause(session_id)
        self.auto_paused_at_silence_seconds = threshold

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
        # A pause the user pressed is not an auto-pause; clear any stale reason
        # so the UI never mislabels it.
        self.auto_paused_at_silence_seconds = None
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
        # The segmenter tick is gone now, so anything the gate is still holding
        # would never be released. These are real captions for real segments.
        self._caption_gate.flush()
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
            # Lets the UI say "paused automatically after N s of silence"
            # instead of leaving the user wondering why recording stopped.
            # None whenever the pause was the user's own.
            "auto_paused_after_seconds": (
                self.auto_paused_at_silence_seconds
                if record.status is SessionStatus.PAUSED
                else None
            ),
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
