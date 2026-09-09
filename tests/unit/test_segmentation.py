from __future__ import annotations

import wave
from pathlib import Path

import pytest

from classroom_interpreter.agents.segmentation import (
    CorruptCaptureChunk,
    SegmentAgent,
    SegmenterConfig,
)
from classroom_interpreter.storage import SessionRepository

import helpers_phase3 as h


def make_agent(database, tmp_path, session_id, **config) -> SegmentAgent:
    store = tmp_path / "sessions" / session_id
    defaults = dict(min_seconds=2.0, max_seconds=30.0, silence_milliseconds=700,
                    overlap_milliseconds=0, threshold_rms=300.0)
    defaults.update(config)
    return SegmentAgent(database, store, session_id, SegmenterConfig(**defaults))


def segment_rows(database, session_id):
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM segments WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_silence_close_seals_two_segments(database, tmp_path):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([
        ("silence", 0.5), ("speech", 3.0), ("silence", 1.0),
        ("speech", 3.0), ("silence", 1.0),
    ])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id)
    sealed = agent.process_available_chunks()
    assert len(sealed) == 2
    rows = segment_rows(database, session_id)
    assert [r["state"] for r in rows] == ["CAPTURED", "CAPTURED"]
    assert [r["sequence"] for r in rows] == [0, 1]
    # First utterance: starts ~500 ms, speech ends ~3500 ms, closed after 700 ms silence.
    assert rows[0]["start_ms"] == pytest.approx(500, abs=20)
    assert rows[0]["end_ms"] == pytest.approx(4200, abs=20)
    assert rows[0]["duration_ms"] == pytest.approx(3700, abs=40)
    assert rows[1]["start_ms"] == pytest.approx(4500, abs=20)
    # Cursor durably consumed everything.
    with database.connect() as connection:
        cursor = connection.execute(
            "SELECT next_chunk_sequence, in_utterance FROM pipeline_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    assert cursor["next_chunk_sequence"] == 1
    assert cursor["in_utterance"] == 0


def test_sub_minimum_utterance_is_discarded_not_sealed(database, tmp_path):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([
        ("silence", 1.0), ("speech", 0.5), ("silence", 1.0),
    ])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id)
    assert agent.process_available_chunks() == ()
    assert segment_rows(database, session_id) == []
    with database.connect() as connection:
        cursor = connection.execute(
            "SELECT discarded_utterances FROM pipeline_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    assert cursor["discarded_utterances"] == 1


def test_max_duration_forces_close_with_overlap(database, tmp_path):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 7.0), ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id, max_seconds=5.0,
                       overlap_milliseconds=250)
    sealed = agent.process_available_chunks()
    assert len(sealed) == 2
    rows = segment_rows(database, session_id)
    # Second segment opens with the configured overlap of the first segment's tail.
    assert rows[0]["duration_ms"] >= 5000
    assert rows[1]["overlap_ms"] == 250
    assert rows[1]["start_ms"] < rows[0]["end_ms"]


def test_utterance_spanning_chunk_boundary_stays_one_segment(database, tmp_path):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("silence", 18.0), ("speech", 5.0), ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks,
                   chunk_seconds=20)
    agent = make_agent(database, tmp_path, session_id)
    sealed = agent.process_available_chunks()
    assert len(sealed) == 1
    rows = segment_rows(database, session_id)
    assert rows[0]["start_ms"] == pytest.approx(18_000, abs=20)
    assert rows[0]["end_ms"] == pytest.approx(23_700, abs=40)
    with wave.open(rows[0]["audio_path"], "rb") as wav:
        assert wav.getnframes() == pytest.approx(5_700 * 16, abs=40)
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16_000, 1, 2)


def test_epoch_boundary_force_seals_open_utterance(database, tmp_path):
    session_id = h.make_session(database)
    chunk_dir = tmp_path / "capture" / session_id
    # Epoch 0: 3 s speech, utterance still open when the chunk ends.
    h.write_chunks(database, chunk_dir, session_id, h.envelope_blocks([("speech", 3.0)]),
                   epoch=0, chunk_seconds=10)
    # Epoch 1 (e.g. after pause/resume): fresh audio.
    h.write_chunks(database, chunk_dir, session_id, h.envelope_blocks([("speech", 3.0)]),
                   epoch=1, chunk_seconds=10)
    agent = make_agent(database, tmp_path, session_id)
    agent.process_available_chunks()
    agent.seal_tail()
    rows = segment_rows(database, session_id)
    assert len(rows) == 2
    # No segment may span the epoch boundary: the first is force-sealed at ~3 s.
    assert rows[0]["end_ms"] <= 3_100
    assert rows[1]["start_ms"] >= rows[0]["end_ms"]


def test_crash_resume_checkpoint_reproduces_same_segments(database, tmp_path):
    session_a = h.make_session(database, "one-shot")
    blocks = h.envelope_blocks([
        ("silence", 1.0), ("speech", 25.0), ("silence", 1.0),
        ("speech", 4.0), ("silence", 1.0),
    ])
    chunk_dir = tmp_path / "capture" / session_a
    h.write_chunks(database, chunk_dir, session_a, blocks, chunk_seconds=10)
    one_shot = make_agent(database, tmp_path, session_a, max_seconds=30.0)
    one_shot.process_available_chunks()
    one_shot.seal_tail()
    expected = segment_rows(database, session_a)
    assert len(expected) >= 2

    # Expose only the first half, persist an open VAD cursor, then make the rest
    # durable and simulate a process restart. Recovery must rebuild from capture
    # rather than cold-resume that open cursor.
    session_b = h.make_session(database, "restarted")
    chunk_dir_b = tmp_path / "capture" / session_b
    split = len(blocks) // 2
    h.write_chunks(
        database, chunk_dir_b, session_b, blocks[:split], chunk_seconds=10,
        start_ns=1_000_000_000,
    )
    crashed = make_agent(database, tmp_path, session_b, max_seconds=30.0)
    crashed.process_available_chunks()
    elapsed_ns = 1_000_000_000 + round(
        sum(len(block) // 2 for block in blocks[:split]) / h.RATE * 1_000_000_000
    )
    h.write_chunks(
        database, chunk_dir_b, session_b, blocks[split:], chunk_seconds=10,
        start_ns=elapsed_ns,
    )
    resumed = make_agent(database, tmp_path, session_b, max_seconds=30.0)
    resumed.rebuild_unconfirmed_tail()
    resumed.process_available_chunks()
    resumed.seal_tail()
    actual = segment_rows(database, session_b)

    assert len(actual) == len(expected)
    for want, got in zip(expected, actual):
        assert got["sequence"] == want["sequence"]
        # 48 kHz → 16 kHz is an exact integer ratio, so boundaries reproduce
        # within one 10 ms frame even with cold-started resamplers.
        assert got["start_ms"] == pytest.approx(want["start_ms"], abs=20)
        assert got["end_ms"] == pytest.approx(want["end_ms"], abs=20)
        assert got["duration_ms"] == pytest.approx(want["duration_ms"], abs=40)


def test_restart_rebuilds_open_vad_tail_from_capture_not_saved_cursor(database, tmp_path):
    session_id = h.make_session(database, "crash-mid-utterance")
    chunk_dir = tmp_path / "capture" / session_id
    h.write_chunks(
        database, chunk_dir, session_id,
        h.envelope_blocks([("speech", 3.0)]),
        chunk_seconds=10,
        start_ns=1_000_000_000,
    )
    before_crash = make_agent(database, tmp_path, session_id)
    assert before_crash.process_available_chunks() == ()
    with database.transaction() as connection:
        cursor = connection.execute(
            "SELECT in_utterance, pending_pcm FROM pipeline_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert cursor["in_utterance"] == 1 and len(cursor["pending_pcm"]) > 0
        # Model a crash-corrupted/inexact open boundary. Ordinary cursor resume
        # would trust these bytes and start time.
        connection.execute(
            """
            UPDATE pipeline_cursors
            SET pending_pcm = ?, utterance_start_ms = 9000,
                utterance_voiced_frames = 1
            WHERE session_id = ?
            """,
            (b"\x00\x00" * 160, session_id),
        )
    h.write_chunks(
        database, chunk_dir, session_id,
        h.envelope_blocks([("speech", 2.0), ("silence", 1.0)]),
        chunk_seconds=10,
        start_ns=4_000_000_000,
    )

    restarted = make_agent(database, tmp_path, session_id)
    evidence = restarted.rebuild_unconfirmed_tail()
    assert evidence == {
        "kept_done_segments": 0,
        "removed_unconfirmed_segments": 0,
        "restart_boundary_ms": 0,
    }
    restarted.process_available_chunks()
    restarted.seal_tail()
    rows = segment_rows(database, session_id)
    assert len(rows) == 1
    assert rows[0]["start_ms"] == pytest.approx(0, abs=20)
    assert rows[0]["end_ms"] == pytest.approx(5_700, abs=40)
    assert rows[0]["duration_ms"] == pytest.approx(5_700, abs=40)


def test_resume_never_duplicates_sealed_segments(database, tmp_path):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 25.0), ("silence", 1.0)])
    chunk_dir = tmp_path / "capture" / session_id
    h.write_chunks(database, chunk_dir, session_id, blocks, chunk_seconds=10)
    first = make_agent(database, tmp_path, session_id)
    first.process_available_chunks()
    count_after_first = len(segment_rows(database, session_id))
    assert count_after_first >= 1
    # Re-running with a fresh agent (cursor reload) must be a no-op.
    second = make_agent(database, tmp_path, session_id)
    assert second.process_available_chunks() == ()
    assert len(segment_rows(database, session_id)) == count_after_first


def test_corrupt_chunk_fails_closed(database, tmp_path):
    session_id = h.make_session(database)
    chunk_dir = tmp_path / "capture" / session_id
    blocks = h.envelope_blocks([("speech", 3.0), ("silence", 1.0)])
    h.write_chunks(database, chunk_dir, session_id, blocks)
    chunk_file = next(chunk_dir.glob("chunk_*.wav"))
    payload = bytearray(chunk_file.read_bytes())
    payload[-4] ^= 0xFF
    chunk_file.write_bytes(bytes(payload))
    agent = make_agent(database, tmp_path, session_id)
    with pytest.raises(CorruptCaptureChunk):
        agent.process_available_chunks()
    assert segment_rows(database, session_id) == []


def test_seal_tail_flushes_open_voiced_utterance(database, tmp_path):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 3.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id)
    assert agent.process_available_chunks() == ()
    sealed = agent.seal_tail()
    assert len(sealed) == 1
    assert sealed[0].duration_ms == pytest.approx(3_000, abs=20)
    # Second seal_tail is a no-op (already checkpointed).
    assert agent.seal_tail() == ()


def test_segment_files_match_checksum_and_repository(tmp_path, database):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 3.0), ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id)
    sealed = agent.process_available_chunks()
    repository = SessionRepository(database)
    for segment in sealed:
        row = repository.get_segment(session_id, segment.sequence)
        assert row.audio_sha256 == segment.audio_sha256
        assert row.audio_path.exists()
        assert not list(row.audio_path.parent.glob("*.tmp"))
