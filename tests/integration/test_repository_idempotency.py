import hashlib
from pathlib import Path

import pytest

from classroom_interpreter.domain.models import (
    SegmentState,
    TranscriptResult,
    TranslationResult,
)
from classroom_interpreter.storage import (
    CommitOutcome,
    IdempotencyConflict,
    OrderingConflict,
    SessionRepository,
    SourceHashMismatch,
)


def create_processing_segment(
    repository: SessionRepository, tmp_path: Path, session_id: str, sequence: int
):
    segment = repository.create_segment(
        session_id=session_id,
        sequence=sequence,
        start_ms=sequence * 1000,
        end_ms=(sequence + 1) * 1000,
        overlap_ms=0,
        audio_path=tmp_path / f"{sequence}.wav",
        audio_sha256=f"hash-{sequence}",
    )
    repository.transition_segment(
        session_id,
        sequence,
        SegmentState.ASR_PENDING,
        reason_code="TEST",
        actor="test",
    )
    return repository.transition_segment(
        session_id,
        sequence,
        SegmentState.ASR_PROCESSING,
        reason_code="TEST",
        actor="test",
    )


def commit_asr(
    repository: SessionRepository, session_id: str, sequence: int, segment_id: str
) -> str:
    english = f"English segment {sequence}."
    result = TranscriptResult(
        segment_id=segment_id,
        english_text=english,
        provider="mock",
        model="mock-asr-v1",
        latency_ms=1,
        audio_seconds=1,
    )
    assert (
        repository.commit_asr_result(session_id, sequence, result)
        is CommitOutcome.CREATED
    )
    repository.transition_segment(
        session_id,
        sequence,
        SegmentState.TRANSLATION_PENDING,
        reason_code="TEST",
        actor="test",
    )
    repository.transition_segment(
        session_id,
        sequence,
        SegmentState.TRANSLATION_PROCESSING,
        reason_code="TEST",
        actor="test",
    )
    return english


def translation_result(segment_id: str, english: str, sequence: int) -> TranslationResult:
    return TranslationResult(
        segment_id=segment_id,
        chinese_text=f"中文片段 {sequence}。",
        english_text_sha256=hashlib.sha256(english.encode("utf-8")).hexdigest(),
        provider="mock",
        model="mock-translation-v1",
        latency_ms=1,
    )


def test_asr_commit_is_idempotent(repository: SessionRepository, tmp_path: Path) -> None:
    session = repository.create_session("Course", app_version="test")
    segment = create_processing_segment(repository, tmp_path, session.id, 0)
    result = TranscriptResult(
        segment_id=segment.id,
        english_text="Expected loss.",
        provider="mock",
        model="mock-asr-v1",
        latency_ms=1,
        audio_seconds=1,
    )
    assert repository.commit_asr_result(session.id, 0, result) is CommitOutcome.CREATED
    assert (
        repository.commit_asr_result(session.id, 0, result)
        is CommitOutcome.ALREADY_COMMITTED
    )

    different = result.model_copy(update={"english_text": "Different result."})
    with pytest.raises(IdempotencyConflict):
        repository.commit_asr_result(session.id, 0, different)

    with repository.database.connect() as connection:
        count = connection.execute("SELECT count(*) FROM asr_results").fetchone()[0]
    assert count == 1


def test_translation_commit_is_ordered_per_session_and_idempotent(
    repository: SessionRepository, tmp_path: Path
) -> None:
    session_a = repository.create_session("A", app_version="test", session_id="A")
    session_b = repository.create_session("B", app_version="test", session_id="B")
    a0 = create_processing_segment(repository, tmp_path, session_a.id, 0)
    a1 = create_processing_segment(repository, tmp_path, session_a.id, 1)
    b0 = create_processing_segment(repository, tmp_path, session_b.id, 0)
    english_a0 = commit_asr(repository, "A", 0, a0.id)
    english_a1 = commit_asr(repository, "A", 1, a1.id)
    english_b0 = commit_asr(repository, "B", 0, b0.id)

    with pytest.raises(OrderingConflict):
        repository.commit_translation_result_direct(
            "A", 1, translation_result(a1.id, english_a1, 1)
        )

    assert repository.commit_translation_result_direct(
        "B", 0, translation_result(b0.id, english_b0, 0)
    ) is CommitOutcome.CREATED
    assert repository.commit_translation_result_direct(
        "A", 0, translation_result(a0.id, english_a0, 0)
    ) is CommitOutcome.CREATED
    a1_result = translation_result(a1.id, english_a1, 1)
    assert repository.commit_translation_result_direct("A", 1, a1_result) is CommitOutcome.CREATED
    assert (
        repository.commit_translation_result_direct("A", 1, a1_result)
        is CommitOutcome.ALREADY_COMMITTED
    )
    assert repository.get_session("A").next_translation_sequence == 2
    assert repository.get_session("B").next_translation_sequence == 1


def test_translation_rejects_wrong_english_source_hash(
    repository: SessionRepository, tmp_path: Path
) -> None:
    session = repository.create_session("Course", app_version="test")
    segment = create_processing_segment(repository, tmp_path, session.id, 0)
    english = commit_asr(repository, session.id, 0, segment.id)
    result = translation_result(segment.id, english, 0).model_copy(
        update={"english_text_sha256": "wrong"}
    )
    with pytest.raises(SourceHashMismatch):
        repository.commit_translation_result_direct(session.id, 0, result)


def test_translation_cursor_rejects_a_missing_earlier_sequence(
    repository: SessionRepository, tmp_path: Path
) -> None:
    session = repository.create_session("Course", app_version="test")
    segment = create_processing_segment(repository, tmp_path, session.id, 1)
    english = commit_asr(repository, session.id, 1, segment.id)
    with pytest.raises(OrderingConflict, match="expects translation sequence 0"):
        repository.commit_translation_result_direct(
            session.id, 1, translation_result(segment.id, english, 1)
        )
