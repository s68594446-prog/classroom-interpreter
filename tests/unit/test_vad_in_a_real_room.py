"""Segmentation has to work through a microphone, not just on clean PCM.

Every VAD parameter in this project was calibrated against synthesized audio
fed straight into the segmenter. Run against a real microphone in a real room
for an hour, the segmenter effectively stopped segmenting: measured p50 segment
duration was 29.0s against a 30.0s cap and a 6s preferred length, and 16 of 36
segments closed because they hit the ceiling rather than because anyone paused.

Three separate causes, each fixed here:

  1. A single 20ms frame above threshold reset the silence run to zero, so one
     keystroke or reverb tail in an otherwise quiet gap held a segment open.
  2. `segment_preferred_seconds` was validated in Settings and never passed to
     the segmenter, so it did nothing whatsoever.
  3. The threshold was a fixed absolute RMS, which cannot hold across rooms,
     microphones and macOS input gain.

Measured on 800s of the captured audio: p50 27.2s -> 6.6s, p90 30.0s -> 9.5s,
segments at the cap 16/36 -> 0.
"""

from __future__ import annotations

import numpy as np
import pytest

from classroom_interpreter.agents.segmentation import (
    FRAME_MS,
    FRAME_SAMPLES,
    SegmentAgent,
    SegmenterConfig,
    SegmenterState,
)
from classroom_interpreter.config import Settings

import helpers_phase3 as h


def make_agent(database, tmp_path, session_id, **config):
    defaults = dict(min_seconds=2.0, max_seconds=30.0, silence_milliseconds=700,
                    overlap_milliseconds=0, threshold_rms=300.0)
    defaults.update(config)
    return SegmentAgent(database, tmp_path / "s" / session_id, session_id,
                        SegmenterConfig(**defaults))


def classify(agent, rms_values):
    """Run raw RMS levels through the real frame classifier."""
    state = agent._state
    out = []
    for value in rms_values:
        threshold = agent._update_threshold(state, value)
        out.append(value >= threshold)
    return out


# ------------------------------------------------------------- noise floor


def test_a_session_that_starts_mid_sentence_can_still_hear(database, tmp_path):
    """The bug in the first version of the adaptive estimator.

    Seeding the floor from the first frame calibrated it to speech whenever
    recording began while someone was already talking -- after which the
    threshold sat four times above the speech level and nothing was ever voiced
    again. The floor is seeded from its minimum and only learns from frames
    judged NOT to be speech.

    Mutation that turns this red: seed `floor` from `rms` instead of
    `noise_floor_min_rms`.
    """
    agent = make_agent(database, tmp_path, h.make_session(database))
    voiced = classify(agent, [2800.0] * 200)
    assert all(voiced), "recording that starts mid-sentence heard nothing"


def test_loud_speech_cannot_drag_the_threshold_above_quiet_speech(database, tmp_path):
    """Mutation that turns this red: update the floor on every frame."""
    agent = make_agent(database, tmp_path, h.make_session(database))
    classify(agent, [6000.0] * 500)          # a long loud passage
    assert classify(agent, [900.0] * 5)[0], "quiet speech fell below the threshold"


def test_the_floor_rises_to_meet_a_noisy_room(database, tmp_path):
    """The reason the threshold is adaptive at all.

    Mutation that turns this red: return `config.threshold_rms` unconditionally.
    """
    agent = make_agent(database, tmp_path, h.make_session(database),
                       threshold_rms=300.0, noise_floor_multiplier=4.0)
    state = agent._state
    for _ in range(4000):
        agent._update_threshold(state, 900.0)   # sustained room tone
    assert agent._update_threshold(state, 900.0) > 900.0, (
        "a room whose background sits at 900 would have every frame treated "
        "as speech, and no segment would ever close"
    )


def test_a_quiet_room_behaves_exactly_like_the_fixed_threshold(database, tmp_path):
    """Adaptivity must not change anything when it is not needed."""
    agent = make_agent(database, tmp_path, h.make_session(database))
    state = agent._state
    for _ in range(500):
        agent._update_threshold(state, 0.0)
    assert agent._update_threshold(state, 0.0) == pytest.approx(300.0)


# -------------------------------------------------------------- hysteresis


def test_a_single_loud_frame_does_not_cancel_a_silence_run(database, tmp_path):
    """The measured cause of segments running to the cap.

    Mutation that turns this red: reset `silence_run` on any voiced frame.
    """
    session_id = h.make_session(database)
    agent = make_agent(database, tmp_path, session_id, voiced_onset_milliseconds=200)
    agent._state = SegmenterState(in_utterance=True, silence_run=40,
                                  utterance_voiced_frames=200,
                                  pending_pcm=bytearray(b"\x01\x00" * 16000))
    frame = np.full(FRAME_SAMPLES, 6000, dtype="<i2").tobytes()
    agent._process_frame(frame, {"start_ms": 0})
    assert agent._state.silence_run == 40, (
        "one 20ms blip wiped out a silence run that was nearly long enough "
        "to close the segment"
    )


def test_sustained_speech_does_cancel_a_silence_run(database, tmp_path):
    """Hysteresis must not deafen the segmenter to actual speech."""
    session_id = h.make_session(database)
    agent = make_agent(database, tmp_path, session_id, voiced_onset_milliseconds=200)
    agent._state = SegmenterState(in_utterance=True, silence_run=40,
                                  utterance_voiced_frames=200,
                                  pending_pcm=bytearray(b"\x01\x00" * 16000))
    frame = np.full(FRAME_SAMPLES, 6000, dtype="<i2").tobytes()
    for _ in range(25):                      # 250ms > the 200ms onset
        agent._process_frame(frame, {"start_ms": 0})
    assert agent._state.silence_run == 0


# ------------------------------------------------------- preferred length


def test_a_long_utterance_closes_at_the_first_breath_after_the_preferred_length(
    database, tmp_path
):
    """A lecturer who never pauses for 700ms used to be cut at 30s, mid-word.

    Mutation that turns this red: always require `silence_milliseconds`.
    """
    session_id = h.make_session(database)
    # 9s of speech, then a 400ms gap -- too short for the 700ms rule, long
    # enough once the segment has passed its 6s preferred length.
    blocks = h.envelope_blocks([("speech", 9.0), ("silence", 0.4), ("speech", 3.0),
                                ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id, preferred_seconds=6.0,
                       preferred_silence_milliseconds=300, silence_milliseconds=700)
    sealed = agent.process_available_chunks()
    assert len(sealed) >= 2, (
        "the 400ms breath after 9s of speech did not close the segment; it "
        "would have run on to the 30s cap"
    )
    assert sealed[0].duration_ms < 11_000


def test_a_short_utterance_still_needs_the_full_silence_gap(database, tmp_path):
    """The relaxed rule must not fragment normal speech rhythm.

    A 400ms pause two seconds into a sentence is a breath, not a boundary.

    Mutation that turns this red: apply the relaxed gap regardless of duration.
    """
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 2.0), ("silence", 0.4), ("speech", 2.0),
                                ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id, preferred_seconds=6.0,
                       preferred_silence_milliseconds=300, silence_milliseconds=700)
    sealed = agent.process_available_chunks()
    assert len(sealed) == 1, "a mid-sentence breath was treated as a boundary"


def test_the_noise_floor_survives_a_restart(database, tmp_path):
    """Crash-resume must reproduce the same segments.

    The threshold is now state, so if it were not checkpointed a restarted
    segmenter would re-derive the post-checkpoint region with a different
    threshold and could cut different segments.

    Mutation that turns this red: drop `noise_floor_rms` from the checkpoint
    INSERT or from `_load_cursor`.
    """
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 3.0), ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    agent = make_agent(database, tmp_path, session_id)
    agent.process_available_chunks()

    with database.connect() as connection:
        row = connection.execute(
            "SELECT noise_floor_rms FROM pipeline_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    assert row is not None and row["noise_floor_rms"] > 0, (
        "the noise floor was not checkpointed; a restart would segment "
        "differently from the first pass"
    )

    resumed = make_agent(database, tmp_path, session_id)
    restored = resumed._load_cursor()
    assert restored.noise_floor_rms == pytest.approx(row["noise_floor_rms"])


def test_settings_expose_the_new_dials(tmp_path):
    settings = Settings(_env_file=None, classroom_interpreter_data_dir=tmp_path / "d")
    assert settings.vad_voiced_onset_milliseconds > 0
    assert settings.vad_noise_floor_multiplier >= 1.0
    assert settings.segment_preferred_silence_milliseconds > 0
