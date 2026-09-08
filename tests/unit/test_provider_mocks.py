import asyncio
from pathlib import Path

import pytest

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    ContextWindow,
    TranslationRequest,
)
from classroom_interpreter.providers import MockASRProvider, MockTranslationProvider


def segment_ref() -> AudioSegmentRef:
    return AudioSegmentRef(
        segment_id="segment-7",
        session_id="session-1",
        sequence=7,
        audio_path=Path("/synthetic/segment-7.wav"),
        audio_sha256="abc",
        duration_ms=2500,
    )


def test_mock_asr_is_deterministic_and_english_only() -> None:
    provider = MockASRProvider(transcripts={7: "Expected loss is frequency times severity."})
    first = asyncio.run(provider.transcribe(segment_ref(), language="en"))
    second = asyncio.run(provider.transcribe(segment_ref(), language="en"))
    assert first.english_text == second.english_text
    assert first.provider_request_id == second.provider_request_id
    with pytest.raises(ValueError, match="English only"):
        asyncio.run(provider.transcribe(segment_ref(), language="auto"))


def test_mock_translation_is_deterministic() -> None:
    context = ContextWindow(
        session_id="session-1",
        current_segment_id="segment-7",
        previous_segments=(),
        window_start_ms=0,
        window_end_ms=2500,
        estimated_tokens=8,
        current_english_text="Expected loss is frequency times severity.",
    )
    request = TranslationRequest(
        segment_id="segment-7",
        session_id="session-1",
        sequence=7,
        current_english_text=context.current_english_text,
        context=context,
    )
    provider = MockTranslationProvider(translations={7: "预期损失等于频率乘以损失程度。"})
    first = asyncio.run(provider.translate(request))
    second = asyncio.run(provider.translate(request))
    assert first == second

