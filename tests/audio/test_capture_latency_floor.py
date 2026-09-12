"""Capture chunk length is the product's dominant latency term.

Measured on a live 60-minute lecture run: English captions arrived p50 12.2s
after the speaker finished a sentence, while the ASR call itself took 1.6s and
translation 1.7s. The missing ~9s was not the model. The segmenter consumes
only *committed* capture chunks, chunks were hard-coded to 20 seconds, and the
writer validated `10 <= seconds <= 30` -- so a speaker waited on average half a
chunk before any processing could start, and no configuration could reduce it.

Sealing a chunk costs ~1.5ms regardless of its length: four fsyncs and two
checksum reads of a small file. At 1-second chunks the entire durability path
is 0.15% of realtime. The floor bought nothing and cost ~5s of every caption.
"""

from __future__ import annotations

import pytest

from classroom_interpreter.audio.durable import ChunkWriter
from classroom_interpreter.config import Settings

import helpers_phase3 as h

RATE = 16000


def test_short_chunks_are_accepted(database, tmp_path):
    """Mutation that turns this red: restore the `10 <= seconds` lower bound."""
    session_id = h.make_session(database)
    writer = ChunkWriter(database, tmp_path / "c", session_id, RATE, 1, 2)
    assert writer.limit == RATE * 2, "a 2-second chunk must hold 2 seconds of frames"


def test_a_chunk_shorter_than_the_silence_gap_is_still_rejected(database, tmp_path):
    """The floor is not removed, only lowered to where it stops helping.

    Below the VAD silence gap (700ms) a shorter chunk cannot close a segment
    any sooner, so it is pure overhead and a sign of misconfiguration.
    """
    session_id = h.make_session(database)
    with pytest.raises(ValueError, match="INVALID_CAPTURE_FORMAT"):
        ChunkWriter(database, tmp_path / "c", session_id, RATE, 1, 0.4)


def test_an_absurdly_long_chunk_is_still_rejected(database, tmp_path):
    session_id = h.make_session(database)
    with pytest.raises(ValueError, match="INVALID_CAPTURE_FORMAT"):
        ChunkWriter(database, tmp_path / "c", session_id, RATE, 1, 45)


def test_short_chunks_seal_and_commit_the_same_audio(database, tmp_path):
    """Latency must not be bought with lost or corrupted audio.

    The same total audio, written in 2-second chunks, must produce the same
    frame count and pass the same verification as one long chunk.
    """
    session_id = h.make_session(database)
    writer = ChunkWriter(database, tmp_path / "short", session_id, RATE, 1, 2)
    pcm = b"\x01\x00" * (RATE * 2)
    sealed = []
    for index in range(5):
        sealed.extend(writer.append(pcm, index * 2 * 10**9, "2026-09-12T00:00:00+00:00", 0))

    assert len(sealed) == 5, "each 2-second write should seal exactly one chunk"
    assert sum(chunk.frame_count for chunk in sealed) == RATE * 10

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT sequence, frame_count FROM capture_chunks"
            " WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
    assert [r["sequence"] for r in rows] == [0, 1, 2, 3, 4]
    assert all(r["frame_count"] == RATE * 2 for r in rows)


def test_the_default_is_short_enough_to_matter(tmp_path):
    """A configurable knob left at 20s would have fixed nothing.

    Mutation that turns this red: set the `capture_chunk_seconds` default back
    to 20.
    """
    settings = Settings(_env_file=None, classroom_interpreter_data_dir=tmp_path / "d")
    assert settings.capture_chunk_seconds <= 3, (
        "the default chunk length is what decides caption latency for anyone "
        "who never touches configuration"
    )
