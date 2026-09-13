"""Silence is not a failure, and it must not cost the class its summary.

Measured on a live session: of six segments that reached ASR, two came back
with an empty transcript. Empty is deterministic -- the same audio returns
empty every time -- so the five retries each of them got were five paid calls
that could not succeed, and the segments then landed in ASR_FAILED.

ASR_FAILED is terminal *and* fatal to the class. The finalization drain refuses
to advance a session with any failed segment (D-006) and `verify_drained`
requires every segment to be DONE, so two seconds of silence, correctly
transcribed as nothing, would have destroyed the summary of the whole lecture.

D-006 is right about what it was written for: audio we could not transcribe is
lost content, and an incomplete class must not be summarised as if it were
whole. Silence is not lost content.
"""

from __future__ import annotations

import time

from classroom_interpreter.domain.models import SegmentState, utc_now
from classroom_interpreter.providers.errors import EmptyTranscript

from test_live_api import FakeTranslation, make_client, wait_for_status


class SilentOnSequence:
    """Real transcripts, except for one segment that holds no speech."""

    name = "half-silent"
    model = "half-silent-v1"

    def __init__(self, silent_sequence: int = 0):
        self.silent_sequence = silent_sequence
        self.calls: list[int] = []

    async def transcribe(self, segment, *, language: str = "en"):
        from classroom_interpreter.domain.models import TranscriptResult

        self.calls.append(segment.sequence)
        if segment.sequence == self.silent_sequence:
            raise EmptyTranscript("the recogniser returned no text")
        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=f"Lecture point {segment.sequence}.",
            provider=self.name,
            model=self.model,
            latency_ms=1,
            audio_seconds=segment.duration_ms / 1000,
            created_at=utc_now(),
        )


def run(tmp_path, silent_sequence=0):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    asr = SilentOnSequence(silent_sequence)
    runtime.asr_provider = asr
    with client:
        view = client.post("/api/sessions", json={"course_name": "Silence"}).json()
        session_id = view["session"]["id"]
        # The workers were built at start_class with the original provider.
        for worker in runtime._asr_workers:
            worker.provider = asr
        client.post(f"/api/sessions/{session_id}/end")
        view = wait_for_status(
            client, session_id, {"SUMMARY_SAVED", "RECOVERY_REQUIRED"}, timeout=20
        )
        status = view["session"]["status"]
    return client, database, runtime, session_id, status, asr


def states(database, session_id):
    with database.connect() as connection:
        return [
            row["state"]
            for row in connection.execute(
                "SELECT state FROM segments WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        ]


def test_a_silent_segment_is_terminal_but_not_failed(tmp_path):
    """Mutation that turns this red: let `EmptyTranscript` fall through to the
    `ProviderFailure` handler, as it did when the parser raised MALFORMED.
    """
    client, database, runtime, session_id, status, asr = run(tmp_path)

    found = states(database, session_id)
    assert SegmentState.NO_SPEECH.value in found, f"states were {found}"
    assert SegmentState.ASR_FAILED.value not in found
    assert runtime.progress(session_id)["failed_segments"] == 0
    assert runtime.progress(session_id)["pending_segments"] == 0


def test_a_silent_segment_is_never_retried(tmp_path):
    """The answer is deterministic. Every retry is a paid call that cannot
    succeed.

    Mutation that turns this red: schedule a retry instead of marking
    NO_SPEECH.
    """
    client, database, runtime, session_id, status, asr = run(tmp_path)

    assert asr.calls.count(0) == 1, (
        f"the silent segment was transcribed {asr.calls.count(0)} times"
    )
    with database.connect() as connection:
        attempts = connection.execute(
            "SELECT outcome FROM processing_attempts WHERE stage = 'ASR'"
        ).fetchall()
    assert [row["outcome"] for row in attempts].count("NO_SPEECH") == 1


def test_a_class_with_a_silent_segment_still_gets_its_summary(tmp_path):
    """The whole point. Two seconds of silence used to cost the entire lecture.

    Mutation that turns this red: drop NO_SPEECH from the terminal set in
    `SummaryAgent.verify_drained`.
    """
    client, database, runtime, session_id, status, asr = run(tmp_path)

    assert status == "SUMMARY_SAVED", (
        f"a silent segment stopped the class from being summarised ({status})"
    )


def test_a_silent_segment_contributes_no_text(tmp_path):
    """It is terminal and complete, not a gap filled with a placeholder.

    Mutation that turns this red: commit an empty or placeholder ASR result for
    it instead of marking the segment.
    """
    client, database, runtime, session_id, status, asr = run(tmp_path)

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT s.sequence FROM segments s
            JOIN asr_results a ON a.segment_id = s.id
            WHERE s.session_id = ? AND s.state = ?
            """,
            (session_id, SegmentState.NO_SPEECH.value),
        ).fetchall()
    assert rows == [], "a silent segment carried a transcript into the record"


def test_a_silent_segment_does_not_freeze_the_ones_behind_it(tmp_path):
    """Translations are released strictly in sequence, at a cursor. A silent
    segment never stages one, so the cursor stopped at the first one and every
    later sentence was stuck behind it forever.

    Reproduced before the fix: one NO_SPEECH segment, the next stuck in
    TRANSLATION_PROCESSING, the session frozen in FINALIZING, no summary.

    Mutation that turns this red: drop the `_skip_silent_segments` call from
    `release_next_translation_result`.
    """
    client, database, runtime, session_id, status, asr = run(tmp_path)

    found = states(database, session_id)
    assert SegmentState.NO_SPEECH.value in found
    stuck = [s for s in found if s.startswith("TRANSLATION_")]
    assert not stuck, f"segments left waiting behind a silent one: {found}"
    assert found.count(SegmentState.DONE.value) >= 1, (
        "nothing after the silent segment ever completed"
    )


def test_several_adjacent_silent_segments_are_all_skipped(tmp_path):
    """Silence comes in runs, not single segments.

    Mutation that turns this red: skip one segment instead of looping.
    """
    from classroom_interpreter.storage import SessionRepository

    client, database, runtime, session_id, status, asr = run(tmp_path)
    repository = SessionRepository(database)
    with database.connect() as connection:
        sequences = [
            row["sequence"]
            for row in connection.execute(
                "SELECT sequence FROM segments WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        ]
        connection.execute(
            "UPDATE sessions SET next_translation_sequence = 0 WHERE id = ?",
            (session_id,),
        )
        connection.execute(
            "UPDATE segments SET state = ? WHERE session_id = ?",
            (SegmentState.NO_SPEECH.value, session_id),
        )
        connection.commit()

    skipped = repository.advance_past_unreleasable(session_id)

    with database.connect() as connection:
        cursor = connection.execute(
            "SELECT next_translation_sequence FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()[0]
    assert cursor == len(sequences), (
        f"cursor stopped at {cursor} with {len(sequences)} silent segments"
    )
    assert [gap["sequence"] for gap in skipped] == sequences, (
        "a skipped sentence must be reported, not spliced out silently"
    )
