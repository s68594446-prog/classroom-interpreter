from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    SegmentState,
    TranscriptResult,
    utc_now,
)
from classroom_interpreter.providers.base import ASRProvider
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.storage.repository import IdempotencyConflict, SessionRepository

# Categories that must never enter an automatic retry loop (FAILURE_MODES.md).
NON_RETRYABLE_CATEGORIES = frozenset({"AUTH", "BALANCE", "CONTENT_FILTER"})


@dataclass(slots=True)
class TranscriptionAgent:
    """Claims CAPTURED/eligible segments through durable SQLite leases, calls
    the configured ASR provider strictly outside any database transaction, and
    commits results idempotently. At-least-once execution: a crash may repeat a
    provider call, but `commit_asr_result` guarantees one result per segment.
    """

    database: Database
    provider: ASRProvider
    worker_id: str = "transcription-1"
    lease_seconds: float = 120.0
    max_attempts: int = 5
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 60.0
    request_timeout_seconds: float = 30.0
    jitter_fraction: float = 0.1
    # The class this worker belongs to. None keeps the historical behaviour of
    # claiming across every session, which the offline drain tools use.
    session_id: str | None = None
    event_sink: object | None = None
    last_step_error: str | None = None
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    _repository: SessionRepository = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._repository = SessionRepository(self.database)

    async def step(self) -> bool:
        """Run one claim → transcribe → commit cycle. Returns False when no
        work was claimable (callers use this to decide whether to sleep)."""
        self._repository.promote_captured_segments(self.session_id)
        claim = self._repository.claim_next_asr_segment(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            session_id=self.session_id,
        )
        if claim is None:
            return False
        await self._process_claim(claim)
        return True

    async def run(self, *, stop: asyncio.Event, idle_sleep_seconds: float = 0.5) -> None:
        while not stop.is_set():
            try:
                worked = await self.step()
                self.last_step_error = None
            except Exception as exc:  # keep the worker alive; state stays durable
                self.last_step_error = type(exc).__name__
                worked = False
            if not worked:
                if not await self._sleep(stop, idle_sleep_seconds):
                    return

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> bool:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return True
        return False

    async def _process_claim(self, segment) -> None:
        sequence = segment.sequence
        session_id = segment.session_id
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_number FROM processing_attempts
                WHERE segment_id = ? AND stage = 'ASR'
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (segment.id,),
            ).fetchone()
        attempt_number = int(row["attempt_number"]) if row else 1

        if not segment.audio_path.is_file():
            self._finish_and_fail(
                segment,
                attempt_number,
                category="MALFORMED",
                message="segment audio file is missing",
                outcome="LOCAL_FILE_MISSING",
                http_status=None,
                provider_request_id=None,
                latency_ms=0,
            )
            return
        actual_sha256 = _sha256_file(segment.audio_path)
        if actual_sha256 != segment.audio_sha256:
            self._finish_and_fail(
                segment,
                attempt_number,
                category="MALFORMED",
                message="segment audio checksum mismatch",
                outcome="LOCAL_FILE_CORRUPT",
                http_status=None,
                provider_request_id=None,
                latency_ms=0,
            )
            return

        ref = AudioSegmentRef(
            segment_id=segment.id,
            session_id=session_id,
            sequence=sequence,
            audio_path=segment.audio_path,
            audio_sha256=segment.audio_sha256,
            duration_ms=segment.duration_ms,
        )
        started = utc_now()
        try:
            result = await asyncio.wait_for(
                self.provider.transcribe(ref, language="en"),
                timeout=self.request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._classify_and_store(
                segment,
                attempt_number,
                category="TIMEOUT",
                message="ASR request timed out",
                retryable=True,
                http_status=None,
                provider_request_id=None,
                retry_after_ms=None,
                started=started,
            )
            return
        except ProviderFailure as failure:
            error = failure.error
            self._classify_and_store(
                segment,
                attempt_number,
                category=error.category.value,
                message=error.safe_message or error.category.value,
                retryable=error.retryable,
                http_status=error.http_status,
                provider_request_id=None,
                retry_after_ms=error.retry_after_ms,
                started=started,
            )
            return
        except Exception as exc:  # unexpected provider/adapter crash
            self._classify_and_store(
                segment,
                attempt_number,
                category="UNKNOWN",
                message=f"unexpected provider failure: {type(exc).__name__}",
                retryable=True,
                http_status=None,
                provider_request_id=None,
                retry_after_ms=None,
                started=started,
            )
            return
        latency_ms = int((utc_now() - started).total_seconds() * 1000)
        try:
            outcome = self._repository.commit_asr_result(session_id, sequence, result)
        except IdempotencyConflict:
            # A different result is already durably committed for this segment.
            # This is an integrity violation, not a transient failure: fail the
            # segment visibly instead of looping retries forever.
            self._finish_and_fail(
                segment,
                attempt_number,
                category="MALFORMED",
                message="durable result conflict: segment already committed with different content",
                outcome="COMMIT_CONFLICT",
                http_status=None,
                provider_request_id=result.provider_request_id,
                latency_ms=latency_ms,
            )
            return
        except Exception:
            # Transient commit failure: leave the lease to expire and re-run
            # (at-least-once). The idempotent commit protects correctness.
            self._repository.finish_asr_attempt(
                segment.id,
                attempt_number=attempt_number,
                outcome="COMMIT_FAILED",
                latency_ms=latency_ms,
                provider_request_id=result.provider_request_id,
            )
            raise
        self._repository.finish_asr_attempt(
            segment.id,
            attempt_number=attempt_number,
            outcome="OK",
            latency_ms=latency_ms,
            provider_request_id=result.provider_request_id,
        )
        if outcome.name == "CREATED" and self.event_sink is not None:
            # The segment's own session, not "whatever is live". A claim can
            # legitimately come from another class (recovery, offline drain),
            # and publishing that caption under the live session writes one
            # class's sentences into another class's durable record.
            self.event_sink(
                "subtitle.current",
                {
                    "sequence": sequence,
                    "segment_id": segment.id,
                    "english_text": result.english_text,
                },
                segment.session_id,
            )

    def _classify_and_store(
        self,
        segment,
        attempt_number: int,
        *,
        category: str,
        message: str,
        retryable: bool,
        http_status: int | None,
        provider_request_id: str | None,
        retry_after_ms: int | None,
        started: datetime,
    ) -> None:
        latency_ms = int((utc_now() - started).total_seconds() * 1000)
        if not retryable or category in NON_RETRYABLE_CATEGORIES:
            self._finish_and_fail(
                segment,
                attempt_number,
                category=category,
                message=message,
                outcome=f"FAILED_{category}",
                http_status=http_status,
                provider_request_id=provider_request_id,
                latency_ms=latency_ms,
            )
            return
        if attempt_number >= self.max_attempts:
            self._finish_and_fail(
                segment,
                attempt_number,
                category=category,
                message=message,
                outcome=f"EXHAUSTED_{category}",
                http_status=http_status,
                provider_request_id=provider_request_id,
                latency_ms=latency_ms,
            )
            return
        delay = self._backoff_delay(attempt_number, retry_after_ms)
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        self._repository.finish_asr_attempt(
            segment.id,
            attempt_number=attempt_number,
            outcome=f"RETRY_{category}",
            latency_ms=latency_ms,
            error_category=category,
            http_status=http_status,
            provider_request_id=provider_request_id,
        )
        self._repository.mark_asr_retry(
            segment.session_id,
            segment.sequence,
            category=category,
            safe_message=message,
            next_attempt_at=next_attempt_at,
            http_status=http_status,
        )

    def _finish_and_fail(
        self,
        segment,
        attempt_number: int,
        *,
        category: str,
        message: str,
        outcome: str,
        http_status: int | None,
        provider_request_id: str | None,
        latency_ms: int,
    ) -> None:
        self._repository.finish_asr_attempt(
            segment.id,
            attempt_number=attempt_number,
            outcome=outcome,
            latency_ms=latency_ms,
            error_category=category,
            http_status=http_status,
            provider_request_id=provider_request_id,
        )
        self._repository.mark_asr_failed(
            segment.session_id,
            segment.sequence,
            category=category,
            safe_message=message,
            http_status=http_status,
        )

    def _backoff_delay(self, attempt_number: int, retry_after_ms: int | None) -> float:
        if retry_after_ms is not None:
            return max(0.0, retry_after_ms / 1000)
        delay = self.backoff_base_seconds * (2 ** (attempt_number - 1))
        delay = min(delay, self.backoff_cap_seconds)
        if self.jitter_fraction:
            jitter = abs(self._rng.gauss(0, delay * self.jitter_fraction))
            delay = min(delay * (1 + self.jitter_fraction), delay + jitter)
        return delay


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(131072), b""):
            digest.update(block)
    return digest.hexdigest()
