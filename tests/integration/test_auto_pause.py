"""Auto-pause after a long silence (user request, 2026-09-11).

A class that ended, a break, or a microphone left running should stop burning
disk and provider quota. The threshold is measured in *captured audio* via the
segmenter's durable silence run, not a wall clock, so a stalled pipeline cannot
fake it and a restart resumes the count instead of resetting it.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from classroom_interpreter.agents.segmentation import FRAME_MS
from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.storage import Database

from test_live_api import make_client, make_settings


def test_settings_reject_an_auto_pause_shorter_than_the_segment_gap(tmp_path):
    """The threshold must not collide with ordinary sentence gaps.

    With auto-pause below the segment silence threshold, every between-sentence
    gap that closes a segment would also pause the class -- the feature would
    fire constantly and recording would stop mid-lecture.
    """
    with pytest.raises(ValueError, match="auto-pause"):
        Settings(
            _env_file=None,
            classroom_interpreter_data_dir=tmp_path / "d",
            segment_silence_milliseconds=700,
            auto_pause_silence_seconds=0.5,
        )


def test_auto_pause_is_disabled_by_zero(tmp_path):
    """0 must mean off, not "pause immediately"."""
    s = Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path / "d",
        auto_pause_silence_seconds=0,
    )
    assert s.auto_pause_silence_seconds == 0


@contextmanager
def recording_session(tmp_path, idle_ms, threshold=30.0):
    """Yield a live RECORDING session with the silence reading forced.

    Two things this deliberately does *not* do. It does not swap the whole
    segmenter -- that would remove `seal_tail`, so shutdown would fail and the
    session would land in RECOVERY_REQUIRED for reasons unrelated to
    auto-pause. And it yields from inside the client context rather than
    returning: returning out of `with client:` tears the app down, finalizes
    the session, and every status assertion then reads RECOVERY_REQUIRED. Both
    mistakes produce failures that look like auto-pause bugs and are not.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        # Disable auto-pause BEFORE the session exists. The default threshold is
        # 30s and the fake listener writes silent audio, so the live segmenter
        # loop would otherwise reach the threshold on real silence and pause the
        # session before the test has installed its own reading -- a race that
        # looks exactly like a logic bug.
        runtime.settings = runtime.settings.model_copy(
            update={"auto_pause_silence_seconds": 0}
        )
        view = client.post("/api/sessions", json={"course_name": "Quiet"}).json()
        session_id = view["session"]["id"]
        runtime._segmenter.idle_silence_ms = lambda: idle_ms  # type: ignore[method-assign]
        runtime.settings = runtime.settings.model_copy(
            update={"auto_pause_silence_seconds": threshold}
        )
        yield client, database, runtime, session_id


def status_of(client, session_id):
    return client.get(f"/api/sessions/{session_id}").json()["session"]["status"]


def test_long_silence_pauses_the_session(tmp_path):
    """Past the threshold, recording stops on its own.

    Mutation that turns this red: never call `_maybe_auto_pause` from the
    segmenter loop, or compare against the wrong unit (seconds vs ms).
    """
    with recording_session(tmp_path, idle_ms=31_000) as (client, _db, runtime, sid):
        # No "still RECORDING first" assertion here on purpose: the live
        # segmenter loop polls every 0.5s and may legitimately have paused
        # already. Requiring RECORDING first made this test fail *because the
        # feature worked*. What matters is the end state and the reason flag.
        asyncio.run(runtime._maybe_auto_pause())
        assert status_of(client, sid) == "PAUSED"
        assert runtime.auto_paused_at_silence_seconds == 30.0


def test_an_ordinary_speaking_pause_does_not_stop_the_class(tmp_path):
    """The failure that would matter most: pausing mid-lecture.

    A lecturer pausing for breath, writing on the board, or waiting for a
    question produces seconds of silence. Anything short of the threshold must
    leave the session RECORDING.

    Mutation that turns this red: drop the threshold comparison, or use `<=`
    against 0 so any silence qualifies.
    """
    with recording_session(tmp_path, idle_ms=29_000) as (client, _db, runtime, sid):
        asyncio.run(runtime._maybe_auto_pause())
        assert status_of(client, sid) == "RECORDING"
        assert runtime.auto_paused_at_silence_seconds is None


def test_auto_pause_is_idempotent_and_does_not_fight_a_paused_session(tmp_path):
    """The loop runs every 0.5s; it must not re-pause or error once paused."""
    with recording_session(tmp_path, idle_ms=45_000) as (client, _db, runtime, sid):
        for _ in range(3):
            asyncio.run(runtime._maybe_auto_pause())
        assert status_of(client, sid) == "PAUSED"


def test_zero_threshold_never_pauses_however_long_the_silence(tmp_path):
    """Mutation guard: disabling must actually disable."""
    with recording_session(tmp_path, idle_ms=10_000_000, threshold=0) as (
        client, _db, runtime, sid
    ):
        asyncio.run(runtime._maybe_auto_pause())
        assert status_of(client, sid) == "RECORDING"


def test_segmenter_reports_no_idle_silence_while_speech_is_open(tmp_path):
    """Silence inside a sentence is speech rhythm, not an idle room.

    Without this, a lecturer who pauses mid-sentence for longer than the
    threshold would have the class paused out from under them while they are
    still talking.
    """
    from classroom_interpreter.agents.segmentation import SegmenterState

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        client.post("/api/sessions", json={"course_name": "Mid"}).json()
        segmenter = runtime._segmenter
        segmenter._state = SegmenterState(
            in_utterance=True, silence_run=10_000 // FRAME_MS
        )
        assert segmenter.idle_silence_ms() == 0
        segmenter._state = SegmenterState(
            in_utterance=False, silence_run=10_000 // FRAME_MS
        )
        assert segmenter.idle_silence_ms() == 10_000


def test_state_payload_distinguishes_auto_pause_from_a_user_pause(tmp_path):
    """The UI must be able to explain why recording stopped.

    Without this the user sees PAUSED with no reason and reasonably assumes
    something broke. A pause the user pressed must NOT be labelled automatic.

    Mutation that turns this red: report the reason regardless of who paused,
    or forget to clear it in `pause()`.
    """
    with recording_session(tmp_path, idle_ms=31_000) as (client, _db, runtime, sid):
        asyncio.run(runtime._maybe_auto_pause())
        auto = runtime._state_payload(sid)
        assert auto["status"] == "PAUSED"
        assert auto["auto_paused_after_seconds"] == 30.0

    # A pause the user pressed carries no automatic reason.
    with recording_session(tmp_path, idle_ms=0) as (client, _db, runtime, sid):
        asyncio.run(runtime.pause(sid))
        manual = runtime._state_payload(sid)
        assert manual["status"] == "PAUSED"
        assert manual["auto_paused_after_seconds"] is None
