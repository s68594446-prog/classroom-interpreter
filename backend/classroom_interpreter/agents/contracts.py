from __future__ import annotations

from pathlib import Path
from typing import Protocol

from classroom_interpreter.domain.models import (
    AudioFrame,
    AudioSegmentRef,
    ContextWindow,
    HealthSnapshot,
    SegmentRecord,
    SessionRecord,
    SummarySourcePolicy,
    TranscriptResult,
    TranslationResult,
)


class AudioListenerAgent(Protocol):
    async def start(self, session_id: str) -> None: ...

    async def pause(self, session_id: str) -> None: ...

    async def resume(self, session_id: str) -> None: ...

    async def stop(self, session_id: str) -> None: ...


class SegmentAgent(Protocol):
    async def accept_frame(self, frame: AudioFrame) -> tuple[SegmentRecord, ...]: ...

    async def seal_tail(self, session_id: str) -> tuple[SegmentRecord, ...]: ...


class TranscriptionAgent(Protocol):
    async def process(self, segment: AudioSegmentRef) -> TranscriptResult: ...


class ContextAgent(Protocol):
    async def build(self, session_id: str, segment_sequence: int) -> ContextWindow: ...


class TranslationAgent(Protocol):
    async def process(self, session_id: str, segment_sequence: int) -> TranslationResult: ...


class SessionOrchestratorAgent(Protocol):
    async def start_class(self, course_name: str) -> SessionRecord: ...

    async def pause(self, session_id: str) -> SessionRecord: ...

    async def resume(self, session_id: str) -> SessionRecord: ...

    async def end_class(self, session_id: str) -> SessionRecord: ...


class SummaryAgent(Protocol):
    async def summarize(
        self,
        session_id: str,
        *,
        source_policy: SummarySourcePolicy = SummarySourcePolicy.ENGLISH_SOURCE_OF_TRUTH,
    ) -> Path: ...


class CleanupAgent(Protocol):
    async def purge(self, session_id: str) -> None: ...


class RecoveryAgent(Protocol):
    async def scan(self) -> tuple[SessionRecord, ...]: ...

    async def resume(self, session_id: str) -> SessionRecord: ...


class HealthMonitorAgent(Protocol):
    async def snapshot(self) -> HealthSnapshot: ...
