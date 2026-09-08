from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    TranscriptResult,
    TranslationRequest,
    TranslationResult,
)


class ASRProvider(Protocol):
    name: str
    model: str

    async def transcribe(
        self, segment: AudioSegmentRef, *, language: str = "en"
    ) -> TranscriptResult: ...


class TranslationProvider(Protocol):
    name: str
    model: str

    async def translate(self, request: TranslationRequest) -> TranslationResult: ...


class SummaryProvider(Protocol):
    name: str
    model: str

    async def summarize_chunk(
        self, *, chunk_index: int, transcript_material: str
    ) -> str: ...

    async def synthesize(
        self,
        *,
        course_name: str,
        date: str,
        duration_text: str,
        chunk_summaries: Sequence[str],
    ) -> str: ...

