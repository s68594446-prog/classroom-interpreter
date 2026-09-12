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


def lecture_envelope(seconds: float, seed: int = 20260912):
    """A frame-level loudness curve with the statistics that broke the VAD.

    Measured from the 54 seconds of real audio that produced one translated
    line and then nothing:

        below   75 rms :  4.3% of frames, longest run   30ms
        below  150 rms : 32.0% of frames, longest run  240ms
        below  300 rms : 46.8% of frames

    Two properties matter, and an earlier version of this generator had
    neither, so it passed against the very bug that shipped:

    1. The DEEP dips are extremely brief -- at most 30ms. An estimator that
       only corrects downward when it sees a frame quieter than itself gets
       almost no opportunity to correct.
    2. The recording is not uniform. Its second half was continuously loud:
       the measured threshold climbed steadily from 300 to 1743 across the
       last twenty seconds, because nothing in that stretch fell below the
       rising floor. A generator that puts a clean gap after every phrase
       hands any estimator a reset it will not get in a real lecture.
    """
    import random

    rng = random.Random(seed)
    out = []
    total = int(seconds * 100)
    # First third: ordinary speech with real gaps between phrases.
    while len(out) < total // 3:
        for _ in range(rng.randint(60, 140)):
            out.append(rng.uniform(600, 2400))
        for _ in range(rng.randint(8, 24)):
            out.append(rng.uniform(150, 260))
        if rng.random() < 0.35:
            for _ in range(rng.randint(1, 3)):
                out.append(rng.uniform(60, 80))
    # Remainder: a speaker who does not stop. Dips stay shallow -- breaths and
    # syllable boundaries, never silence.
    while len(out) < total:
        for _ in range(rng.randint(80, 200)):
            out.append(rng.uniform(700, 2600))
        for _ in range(rng.randint(3, 10)):
            out.append(rng.uniform(320, 520))
    return out[:total]


def test_the_threshold_can_never_exceed_the_signal_for_long(database, tmp_path):
    """THE regression test. This is the failure that reached the owner.

    The previous estimator leaked upward exponentially with no upper bound.
    Over 54 seconds of real continuous speech it drove the threshold from 300
    to 1743 -- above the speaker's own level -- and segmentation stopped dead
    after the first sentence: one translated line in a minute of audio.

    The tests that existed ran a few hundred frames against a forgiving
    synthetic pattern, and a drift that takes tens of seconds to matter is
    invisible to both.

    Mutation that turns this red: replace the windowed min/max with an
    unbounded accumulator, e.g. `floor *= 1.0008` on every loud frame.
    """
    agent = make_agent(database, tmp_path, h.make_session(database))
    state = agent._state
    levels = lecture_envelope(90)
    heard = 0
    worst = 0.0
    WARMUP = 1000                                    # 10s to fill the window
    for index, level in enumerate(levels):
        threshold = agent._update_threshold(state, level)
        if level >= threshold:
            heard += 1
        if index < WARMUP:
            # Before the window has seen a quiet stretch the threshold sits at
            # the signal itself, which makes everything voiced -- the safe
            # direction, and not what this test is about.
            continue
        worst = max(worst, threshold)
        assert threshold <= 1200, (
            f"at {index / 100:.1f}s the threshold reached {threshold:.0f}, "
            "climbing into the speech band (600-2400) it is supposed to "
            "detect -- this is the unbounded drift that stopped segmentation "
            "after one sentence"
        )
    speech_frames = sum(1 for v in levels if v >= 600)
    assert heard >= speech_frames * 0.9, (
        f"only {heard} of {speech_frames} speech frames were heard; the "
        f"threshold peaked at {worst:.0f}"
    )


def test_quiet_playback_is_still_heard(database, tmp_path):
    """Two real recordings, one segmented correctly and one produced ZERO
    segments with the same absolute threshold.

    Their frame levels: p5=93 / p50=1706 for the loud one, p5=77 / p50=393 for
    the quiet one. A threshold calibrated for the first sits above the median
    frame of the second. Only the RATIO between background and speech is
    stable across playback volume and microphone distance.

    Mutation that turns this red: return a constant `config.threshold_rms`.
    """
    agent = make_agent(database, tmp_path, h.make_session(database))
    state = agent._state
    rng = __import__("random").Random(7)
    heard = 0
    for index in range(3000):
        level = 77.0 if index % 100 < 20 else rng.uniform(300, 600)   # the quiet recording
        threshold = agent._update_threshold(state, level)
        if level >= threshold:
            heard += 1
    assert heard > 1500, (
        "a quietly played recording was treated as silence throughout, which "
        "is exactly what produced zero segments from 54 seconds of audio"
    )


def test_a_noisy_room_raises_the_threshold(database, tmp_path):
    """The reason the threshold is adaptive at all.

    A hall whose background sits at 900 must not have every frame treated as
    speech, or no segment would ever close.

    Mutation that turns this red: return `config.threshold_rms` unconditionally.
    """
    agent = make_agent(database, tmp_path, h.make_session(database))
    state = agent._state
    rng = __import__("random").Random(11)
    for index in range(3000):
        level = 900.0 if index % 100 < 30 else rng.uniform(2500, 5000)
        threshold = agent._update_threshold(state, level)
    assert threshold > 900.0, (
        "background at 900 would be treated as speech and segments would never "
        "close on silence"
    )


def test_the_threshold_recovers_after_a_loud_passage(database, tmp_path):
    """Bounded is not enough; it also has to come back down.

    After a loud stretch the estimate must fall far enough to hear normal
    speech again, and within seconds rather than minutes.
    """
    agent = make_agent(database, tmp_path, h.make_session(database))
    state = agent._state
    for _ in range(2000):
        agent._update_threshold(state, 6000.0)
    heard = 0
    for _ in range(1500):                            # 15s of ordinary speech
        threshold = agent._update_threshold(state, 700.0)
        if 700.0 >= threshold:
            heard += 1
    assert heard > 500, (
        "after a loud passage the segmenter stayed deaf to ordinary speech"
    )


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
