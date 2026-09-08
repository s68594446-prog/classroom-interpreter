from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    ProviderError,
    ProviderErrorCategory,
    TranscriptResult,
    TranslationRequest,
    TranslationResult,
    utc_now,
)
from .errors import ProviderFailure


@dataclass(slots=True)
class MockASRProvider:
    transcripts: Mapping[int, str] = field(default_factory=dict)
    latency_ms: int = 0
    name: str = "mock"
    model: str = "mock-asr-v1"
    created_at: datetime = field(default_factory=utc_now)

    async def transcribe(
        self, segment: AudioSegmentRef, *, language: str = "en"
    ) -> TranscriptResult:
        if language != "en":
            raise ValueError("Phase 1 mock ASR accepts explicit English only")
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        text = self.transcripts.get(
            segment.sequence, f"Mock transcript for segment {segment.sequence}."
        )
        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=text,
            provider=self.name,
            model=self.model,
            latency_ms=self.latency_ms,
            audio_seconds=segment.duration_ms / 1000,
            provider_request_id=f"mock-asr-{segment.session_id}-{segment.sequence}",
            created_at=self.created_at,
        )


@dataclass(slots=True)
class MockTranslationProvider:
    translations: Mapping[int, str] = field(default_factory=dict)
    latency_ms: int = 0
    name: str = "mock"
    model: str = "mock-translation-v1"
    created_at: datetime = field(default_factory=utc_now)

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        text = self.translations.get(
            request.sequence, f"模拟翻译片段 {request.sequence}。"
        )
        source_hash = hashlib.sha256(
            request.current_english_text.encode("utf-8")
        ).hexdigest()
        return TranslationResult(
            segment_id=request.segment_id,
            chinese_text=text,
            english_text_sha256=source_hash,
            provider=self.name,
            model=self.model,
            latency_ms=self.latency_ms,
            provider_request_id=(
                f"mock-translation-{request.session_id}-{request.sequence}"
            ),
            created_at=self.created_at,
        )


@dataclass(slots=True)
class ScriptedASRProvider:
    """Deterministic error-matrix mock.

    `script` maps segment sequence -> TranscriptResult | ProviderFailure |
    Exception | Callable(AudioSegmentRef) -> either. Unscripted sequences use
    `default`. Records every call so tests can assert exactly-once/at-least-once
    behavior. Never selected silently: tests inject it explicitly.
    """

    script: Mapping[int, Any] = field(default_factory=dict)
    default: Callable[[AudioSegmentRef], TranscriptResult] | TranscriptResult | None = None
    latency_ms: int = 0
    name: str = "scripted-mock"
    model: str = "mock-asr-v1"
    calls: list[AudioSegmentRef] = field(default_factory=list)

    async def transcribe(
        self, segment: AudioSegmentRef, *, language: str = "en"
    ) -> TranscriptResult:
        self.calls.append(segment)
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        outcome: Any = (
            self.script[segment.sequence]
            if segment.sequence in self.script
            else self.default
        )
        if callable(outcome) and not isinstance(outcome, TranscriptResult):
            outcome = outcome(segment)
        if outcome is None:
            outcome = self.default
        if callable(outcome) and not isinstance(outcome, TranscriptResult):
            outcome = outcome(segment)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, ProviderFailure):
            raise outcome
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, TranscriptResult):
            return outcome
        if outcome is None:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.UNKNOWN,
                    retryable=False,
                    safe_message="scripted provider has no outcome configured",
                )
            )
        raise TypeError(f"unsupported scripted outcome {type(outcome).__name__}")


@dataclass(slots=True)
class MockSummaryProvider:
    """Deterministic summary mock.

    Produces a structurally valid ten-section document from the chunk notes it
    receives. `chunk_outputs` / `synthesis_output` override specific calls for
    invalid-output matrix tests; `calls` records every invocation.
    """

    chunk_outputs: Mapping[int, str] = field(default_factory=dict)
    synthesis_output: str | None = None
    default_chunk_notes: str = "片段要点：老师讲解了核心概念并给出了例题。"
    name: str = "mock"
    model: str = "mock-summary-v1"
    chunk_calls: list[str] = field(default_factory=list)
    synthesize_calls: list[list[str]] = field(default_factory=list)

    async def summarize_chunk(
        self, *, chunk_index: int, transcript_material: str
    ) -> str:
        self.chunk_calls.append(transcript_material)
        outcome = self.chunk_outputs.get(chunk_index, self.default_chunk_notes)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(chunk_index, transcript_material)
        return str(outcome)

    async def synthesize(
        self,
        *,
        course_name: str,
        date: str,
        duration_text: str,
        chunk_summaries: Sequence[str],
    ) -> str:
        self.synthesize_calls.append(list(chunk_summaries))
        if self.synthesis_output is not None:
            outcome: Any = self.synthesis_output
            if isinstance(outcome, Exception):
                raise outcome
            if callable(outcome):
                return str(outcome(course_name, date, duration_text, chunk_summaries))
            return str(outcome)
        sections = "\n\n".join(
            f"## {index}. {title}\n{notes if index <= len(chunk_summaries) else '不确定'}"
            if False
            else f"## {index + 1}. {title}\n- 要点{index + 1}：{chunk_summaries[min(index, len(chunk_summaries) - 1)] if chunk_summaries else '不确定'}"
            for index, title in enumerate(
                __import__(
                    "classroom_interpreter.providers.summary_contract",
                    fromlist=["REQUIRED_SECTIONS"],
                ).REQUIRED_SECTIONS
            )
        )
        return (
            f"# 课程名称：{course_name}\n\n日期：{date}\n\n"
            f"课程时长：{duration_text}\n\n{sections}"
        )
