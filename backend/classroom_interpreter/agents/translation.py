from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from classroom_interpreter.agents.context import ContextAgent
from classroom_interpreter.domain.models import TranslationRequest, utc_now
from classroom_interpreter.providers.base import TranslationProvider
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.storage.repository import (
    SessionRepository,
    SourceHashMismatch,
)

NON_RETRYABLE_CATEGORIES = frozenset({"AUTH", "BALANCE", "CONTENT_FILTER"})


@dataclass(slots=True)
class TranslationAgent:
    """Orders translations per session against the durable cursor.

    Only the segment at each session's `next_translation_sequence` is claimed
    (head-of-line by design); the context reads committed predecessors only.
    Provider calls run strictly outside database transactions; commits are
    idempotent, hash-bound, and strictly ordered by `commit_translation_result`.
    """

    database: Database
    provider: TranslationProvider
    worker_id: str = "translation-1"
    lease_seconds: float = 120.0
    max_attempts: int = 5
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 60.0
    request_timeout_seconds: float = 30.0
    jitter_fraction: float = 0.1
    context_max_segments: int = 8
    context_max_seconds: int = 90
    context_max_tokens: int = 1200
    event_sink: object | None = None
    last_step_error: str | None = None
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    _repository: SessionRepository = field(init=False, repr=False)
    _context: ContextAgent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._repository = SessionRepository(self.database)
        self._context = ContextAgent(
            self.database,
            max_segments=self.context_max_segments,
            max_seconds=self.context_max_seconds,
            max_tokens=self.context_max_tokens,
        )

    async def step(self) -> bool:
        claim = self._repository.claim_next_translation_segment(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
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
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_number FROM processing_attempts
                WHERE segment_id = ? AND stage = 'TRANSLATION'
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (segment.id,),
            ).fetchone()
        attempt_number = int(row["attempt_number"]) if row else 1

        window = self._context.build(segment.session_id, segment.sequence)
        request = TranslationRequest(
            segment_id=segment.id,
            session_id=segment.session_id,
            sequence=segment.sequence,
            current_english_text=window.current_english_text,
            context=window,
        )
        started = utc_now()
        try:
            result = await asyncio.wait_for(
                self.provider.translate(request),
                timeout=self.request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._classify_and_store(
                segment,
                attempt_number,
                category="TIMEOUT",
                message="translation request timed out",
                retryable=True,
                http_status=None,
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
                retry_after_ms=error.retry_after_ms,
                started=started,
            )
            return
        except Exception as exc:
            self._classify_and_store(
                segment,
                attempt_number,
                category="UNKNOWN",
                message=f"unexpected provider failure: {type(exc).__name__}",
                retryable=True,
                http_status=None,
                retry_after_ms=None,
                started=started,
            )
            return
        latency_ms = int((utc_now() - started).total_seconds() * 1000)
        try:
            outcome = self._repository.commit_translation_result(
                segment.session_id, segment.sequence, result
            )
        except SourceHashMismatch:
            # The provider translated a different source than the committed
            # English. This is an integrity violation, not transient: fail
            # visibly instead of committing mismatched content.
            self._fail(
                segment,
                attempt_number,
                category="MALFORMED",
                message="translation source hash does not match committed English",
                outcome="SOURCE_HASH_MISMATCH",
                http_status=None,
                latency_ms=latency_ms,
            )
            return
        except Exception:
            self._repository.finish_translation_attempt(
                segment.id,
                attempt_number=attempt_number,
                outcome="COMMIT_FAILED",
                latency_ms=latency_ms,
                provider_request_id=result.provider_request_id,
            )
            raise
        self._repository.finish_translation_attempt(
            segment.id,
            attempt_number=attempt_number,
            outcome="OK",
            latency_ms=latency_ms,
            provider_request_id=result.provider_request_id,
        )
        if outcome.name == "CREATED" and self.event_sink is not None:
            self.event_sink(
                "subtitle.committed",
                {
                    "sequence": segment.sequence,
                    "segment_id": segment.id,
                    "english_text": window.current_english_text,
                    "chinese_text": result.chinese_text,
                },
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
        retry_after_ms: int | None,
        started: datetime,
    ) -> None:
        latency_ms = int((utc_now() - started).total_seconds() * 1000)
        if not retryable or category in NON_RETRYABLE_CATEGORIES:
            self._fail(
                segment,
                attempt_number,
                category=category,
                message=message,
                outcome=f"FAILED_{category}",
                http_status=http_status,
                latency_ms=latency_ms,
            )
            return
        if attempt_number >= self.max_attempts:
            self._fail(
                segment,
                attempt_number,
                category=category,
                message=message,
                outcome=f"EXHAUSTED_{category}",
                http_status=http_status,
                latency_ms=latency_ms,
            )
            return
        delay = self._backoff_delay(attempt_number, retry_after_ms)
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        self._repository.finish_translation_attempt(
            segment.id,
            attempt_number=attempt_number,
            outcome=f"RETRY_{category}",
            latency_ms=latency_ms,
            error_category=category,
            http_status=http_status,
        )
        self._repository.mark_translation_retry(
            segment.session_id,
            segment.sequence,
            category=category,
            safe_message=message,
            next_attempt_at=next_attempt_at,
            http_status=http_status,
        )

    def _fail(
        self,
        segment,
        attempt_number: int,
        *,
        category: str,
        message: str,
        outcome: str,
        http_status: int | None,
        latency_ms: int,
    ) -> None:
        self._repository.finish_translation_attempt(
            segment.id,
            attempt_number=attempt_number,
            outcome=outcome,
            latency_ms=latency_ms,
            error_category=category,
            http_status=http_status,
        )
        self._repository.mark_translation_failed(
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
