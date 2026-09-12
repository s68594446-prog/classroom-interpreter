from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionStatus(StrEnum):
    NEW = "NEW"
    RECORDING = "RECORDING"
    PAUSED = "PAUSED"
    FINALIZING = "FINALIZING"
    SUMMARIZING = "SUMMARIZING"
    SUMMARY_SAVED = "SUMMARY_SAVED"
    PURGING = "PURGING"
    PURGED = "PURGED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class SegmentState(StrEnum):
    CAPTURED = "CAPTURED"
    ASR_PENDING = "ASR_PENDING"
    ASR_PROCESSING = "ASR_PROCESSING"
    ASR_DONE = "ASR_DONE"
    ASR_RETRY = "ASR_RETRY"
    ASR_FAILED = "ASR_FAILED"
    TRANSLATION_PENDING = "TRANSLATION_PENDING"
    TRANSLATION_PROCESSING = "TRANSLATION_PROCESSING"
    TRANSLATION_RETRY = "TRANSLATION_RETRY"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    DONE = "DONE"


class HealthStatus(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    UNKNOWN = "unknown"


class WebSocketEventKind(StrEnum):
    SYSTEM_READY = "system.ready"
    SYSTEM_PONG = "system.pong"
    SESSION_STATE = "session.state"
    SUBTITLE_CURRENT = "subtitle.current"
    # Interim transcript of the sentence still being spoken. Display-only:
    # never persisted, never sequenced, replaced by SUBTITLE_CURRENT when the
    # segment seals (D-046).
    SUBTITLE_PARTIAL = "subtitle.partial"
    SUBTITLE_COMMITTED = "subtitle.committed"
    HEALTH = "health.snapshot"


class SummarySourcePolicy(StrEnum):
    ENGLISH_SOURCE_OF_TRUTH = "ENGLISH_SOURCE_OF_TRUTH"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionRecord(FrozenModel):
    id: str
    course_name: str
    status: SessionStatus
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    summary_path: Path | None = None
    summary_hash: str | None = None
    summary_model: str | None = None
    app_version: str
    purged_at: datetime | None = None
    next_translation_sequence: int = Field(default=0, ge=0)
    last_error_category: str | None = None
    last_error_safe_message: str | None = None
    # The FIRST error of the session, kept because the last one is usually the
    # consequence: a capture fault stops the recording, and the summary failure
    # it causes then overwrites it.
    first_error_category: str | None = None
    first_error_safe_message: str | None = None


class PurgedSessionMetadata(FrozenModel):
    session_id: str
    course_name: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int = Field(ge=0)
    summary_path: Path
    summary_hash: str
    summary_model: str
    app_version: str
    purged_at: datetime
    status: SessionStatus = SessionStatus.PURGED


class SegmentRecord(FrozenModel):
    id: str
    session_id: str
    sequence: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    overlap_ms: int = Field(default=0, ge=0)
    audio_path: Path
    audio_sha256: str
    state: SegmentState
    created_at: datetime
    updated_at: datetime


class AudioFrame(FrozenModel):
    session_id: str
    capture_epoch: int = Field(ge=0)
    frame_sequence: int = Field(ge=0)
    monotonic_started_ns: int = Field(ge=0)
    wall_clock_started_at: datetime
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_format: str
    pcm_bytes: bytes


class CaptureChunk(FrozenModel):
    chunk_id: str
    session_id: str
    capture_epoch: int = Field(ge=0)
    sequence: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    path: Path
    byte_size: int = Field(gt=0)
    sha256: str
    sealed_at: datetime


class AudioSegmentRef(FrozenModel):
    segment_id: str
    session_id: str
    sequence: int = Field(ge=0)
    audio_path: Path
    audio_sha256: str
    duration_ms: int = Field(gt=0)


class TranscriptResult(FrozenModel):
    segment_id: str
    english_text: str = Field(min_length=1)
    provider: str
    model: str
    latency_ms: int = Field(ge=0)
    audio_seconds: float = Field(ge=0)
    provider_request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ContextSegment(FrozenModel):
    sequence: int = Field(ge=0)
    english_text: str = Field(min_length=1)
    chinese_text: str = Field(min_length=1)


class ContextWindow(FrozenModel):
    session_id: str
    current_segment_id: str
    previous_segments: tuple[ContextSegment, ...] = ()
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    current_english_text: str = Field(min_length=1)


class TranslationRequest(FrozenModel):
    segment_id: str
    session_id: str
    sequence: int = Field(ge=0)
    current_english_text: str = Field(min_length=1)
    context: ContextWindow


class TranslationResult(FrozenModel):
    segment_id: str
    chinese_text: str = Field(min_length=1)
    english_text_sha256: str
    provider: str
    model: str
    latency_ms: int = Field(ge=0)
    provider_request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ProviderErrorCategory(StrEnum):
    AUTH = "AUTH"
    BALANCE = "BALANCE"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    SERVER = "SERVER"
    MALFORMED = "MALFORMED"
    CONTENT_FILTER = "CONTENT_FILTER"
    NETWORK = "NETWORK"
    UNKNOWN = "UNKNOWN"


class ProviderError(FrozenModel):
    category: ProviderErrorCategory
    retryable: bool
    http_status: int | None = None
    provider_code: str | None = None
    safe_message: str = ""
    retry_after_ms: int | None = Field(default=None, ge=0)


class HealthComponent(FrozenModel):
    name: str
    status: HealthStatus
    reason_code: str
    checked_at: datetime = Field(default_factory=utc_now)


class HealthSnapshot(FrozenModel):
    status: HealthStatus
    phase: str
    app_version: str
    components: tuple[HealthComponent, ...]
    checked_at: datetime = Field(default_factory=utc_now)


# Sentinel sequence for ephemeral events (interim transcripts, D-046). Clients
# advance their replay cursor only on sequences greater than the one they hold,
# so a negative value can never move it -- which is the point: a partial must
# not make a reconnect skip past a real event it has not seen.
EPHEMERAL_EVENT_SEQUENCE = -1


class WebSocketEvent(FrozenModel):
    schema_version: str = "1"
    # >= -1 rather than >= 0 solely to admit EPHEMERAL_EVENT_SEQUENCE. Every
    # persisted event still carries a real, non-negative sequence; only
    # non-persisted fan-out uses the sentinel.
    event_sequence: int = Field(ge=EPHEMERAL_EVENT_SEQUENCE)
    kind: WebSocketEventKind
    session_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
