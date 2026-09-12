"""Interim (partial) transcription — the latency architecture change (D-046).

Before this, the first caption waited for a segment to seal: ~6s of speech plus
~1s of ASR. Transcribing the utterance still being spoken puts English on
screen roughly 2s behind the speaker. Measured on the live endpoint,
transcribing the first 1.5s of a sentence returned usable text in 0.80s.

The property that matters most here is what partials must NOT do: they are
display-only and must never enter the durable pipeline, or they would corrupt
the English-as-source-of-truth contract the summary depends on.
"""

from __future__ import annotations

import asyncio

import pytest

from classroom_interpreter.agents.segmentation import SegmenterState
from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import (
    EPHEMERAL_EVENT_SEQUENCE,
    WebSocketEventKind,
)

from test_live_api import make_client
from test_purge import CONTENT_TABLES


def test_partial_can_be_disabled(tmp_path):
    """0 means off: the extra ASR calls are a cost dial, not mandatory."""
    s = Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path / "d",
        partial_transcription_interval_seconds=0,
    )
    assert s.partial_transcription_interval_seconds == 0


def test_no_partial_loop_is_spawned_when_disabled(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    runtime.settings = runtime.settings.model_copy(
        update={"partial_transcription_interval_seconds": 0}
    )
    with client:
        client.post("/api/sessions", json={"course_name": "Off"})
        names = [t.get_name() for t in runtime._tasks]
        assert "partial-transcription-loop" not in names


def test_partial_loop_is_spawned_when_enabled(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        client.post("/api/sessions", json={"course_name": "On"})
        names = [t.get_name() for t in runtime._tasks]
        assert "partial-transcription-loop" in names


def test_pending_wav_is_only_offered_mid_utterance(tmp_path):
    """Silence between sentences must not be transcribed as a partial."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        client.post("/api/sessions", json={"course_name": "Pending"})
        seg = runtime._segmenter
        speech = bytearray(b"\x01\x00" * 16000 * 2)  # 2s

        seg._state = SegmenterState(in_utterance=True, pending_pcm=speech)
        assert seg.pending_utterance_wav() is not None

        seg._state = SegmenterState(in_utterance=False, pending_pcm=speech)
        assert seg.pending_utterance_wav() is None, "not speaking -> no partial"

        seg._state = SegmenterState(in_utterance=True, pending_pcm=bytearray(b"\x01\x00" * 80))
        assert seg.pending_utterance_wav() is None, "too little audio -> no partial"


def test_pending_wav_is_a_valid_standalone_wav(tmp_path):
    """The provider gets bytes, so the header must be real, not raw PCM."""
    import io
    import wave

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        client.post("/api/sessions", json={"course_name": "Wav"})
        runtime._segmenter._state = SegmenterState(
            in_utterance=True, pending_pcm=bytearray(b"\x01\x00" * 16000 * 2)
        )
        data = runtime._segmenter.pending_utterance_wav()
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 32000


class _RecordingASR:
    """Returns a fixed partial and counts calls."""

    name = "fake"
    model = "fake"

    def __init__(self, text="partial text"):
        self.text = text
        self.calls = 0

    async def transcribe_bytes(self, wav_bytes: bytes) -> str:
        self.calls += 1
        return self.text


def test_partials_reach_subscribers_but_leave_no_durable_trace(tmp_path):
    """THE load-bearing test.

    A partial may be wrong, truncated, or superseded a second later. If one
    ever reached the database it would become part of the English transcript
    the summary treats as fact. It must reach live subscribers and nothing
    else -- not `asr_results`, not `segments`, and not `websocket_events`,
    which is the replay log a reconnecting client reads back.

    Mutations that turn this red: publish partials through `publish()` instead
    of `publish_ephemeral()`, or commit them through the ASR pipeline.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Partial"}).json()
        session_id = view["session"]["id"]

        queue = runtime.publisher.subscribe()
        try:
            runtime.publisher.publish_ephemeral(
                session_id,
                WebSocketEventKind.SUBTITLE_PARTIAL,
                {"english_text": "interim words here"},
            )
            event = queue.get_nowait()
        finally:
            runtime.publisher.unsubscribe(queue)

    assert event.kind is WebSocketEventKind.SUBTITLE_PARTIAL
    assert event.payload["english_text"] == "interim words here"
    assert event.event_sequence == EPHEMERAL_EVENT_SEQUENCE, (
        "a partial must not carry a real sequence, or it would advance a "
        "client's replay cursor past events it never received"
    )

    # The pipeline is live and legitimately writing sealed segments, so the
    # assertion is that this partial's *text* never appears -- not that the
    # tables are empty, which they never would be.
    with database.connect() as connection:
        leaked = connection.execute(
            "SELECT COUNT(*) AS n FROM asr_results"
            " WHERE session_id = ? AND english_text LIKE ?",
            (session_id, "%interim words here%"),
        ).fetchone()["n"]
    assert leaked == 0, "partial text reached asr_results, the summary's source"
    # Same reasoning for the replay log: the live pipeline is publishing real
    # events throughout, so count is meaningless -- the text is what matters.
    with database.connect() as connection:
        replayed = connection.execute(
            "SELECT COUNT(*) AS n FROM websocket_events"
            " WHERE session_id = ? AND payload_json LIKE ?",
            (session_id, "%interim words here%"),
        ).fetchone()["n"]
    assert replayed == 0, (
        "partial was written to websocket_events; a reconnect would replay "
        "stale interim text for a sentence that finished long ago"
    )
    # The tables a leak would land in are the ones purge must clear.
    assert "asr_results" in CONTENT_TABLES
    assert "websocket_events" in CONTENT_TABLES


def test_identical_partials_are_not_republished(tmp_path):
    """A caption that re-renders unchanged every 2s is visual noise.

    The loop keeps the last text and skips when it has not moved.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        client.post("/api/sessions", json={"course_name": "Dedup"})
        asr = _RecordingASR("same text")
        runtime.asr_provider = asr
        runtime._segmenter._state = SegmenterState(
            in_utterance=True, pending_pcm=bytearray(b"\x01\x00" * 16000 * 2)
        )
        loop = asyncio.new_event_loop()
        try:
            last = ""
            emitted = 0
            for _ in range(3):
                wav = runtime._segmenter.pending_utterance_wav()
                text = loop.run_until_complete(asr.transcribe_bytes(wav))
                if text and text != last:
                    emitted += 1
                    last = text
        finally:
            loop.close()
    assert asr.calls == 3
    assert emitted == 1, "unchanged text must publish once, not every interval"


def test_partial_actually_reaches_a_websocket_client(tmp_path):
    """The gap that made D-046 dead on arrival.

    Every other test here checks the *publisher* fan-out, and the publisher was
    always correct. The WebSocket endpoint was not: it forwards an event only
    when `event.event_sequence > last`, `last` starts at the client's cursor
    (-1 by default), and an ephemeral event's sequence is exactly -1. `-1 > -1`
    is false, so every interim caption was dropped one layer below the code
    that produced it.

    Nothing caught it. The publisher tests subscribe to the queue directly, and
    the latency measurement that reported 6.67s -> 2.32s was taken off that
    same queue -- so the number was real and the user still saw nothing.

    Mutation that turns this red: restore the bare `event.event_sequence > last`
    comparison in `api/websocket.py`.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "WSPartial"}).json()
        session_id = view["session"]["id"]

        with client.websocket_connect(f"/ws/sessions/{session_id}?cursor=-1") as ws:
            assert ws.receive_json()["kind"] == "system.ready"
            runtime.publisher.publish_ephemeral(
                session_id,
                WebSocketEventKind.SUBTITLE_PARTIAL,
                {"english_text": "the agent commission"},
            )
            ws.send_text("ping")
            seen = []
            while True:
                message = ws.receive_json()
                if message["kind"] == "system.pong":
                    break
                seen.append(message)

    partials = [m for m in seen if m["kind"] == WebSocketEventKind.SUBTITLE_PARTIAL]
    assert partials, (
        "no interim caption reached the client; D-046 produced text that the "
        "WebSocket layer silently discarded"
    )
    assert partials[0]["payload"]["english_text"] == "the agent commission"


def test_a_partial_never_advances_the_replay_cursor(tmp_path):
    """The reason the sequence is -1 in the first place.

    Letting partials through must not cost the property that motivated making
    them ephemeral: a client's cursor has to keep tracking committed events
    only, or a reconnect would skip real captions that were never delivered.

    Mutation that turns this red: set `last` from an ephemeral event's sequence
    in `api/websocket.py`.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Cursor"}).json()
        session_id = view["session"]["id"]

        with client.websocket_connect(f"/ws/sessions/{session_id}?cursor=-1") as ws:
            assert ws.receive_json()["kind"] == "system.ready"
            runtime.publisher.publish_ephemeral(
                session_id,
                WebSocketEventKind.SUBTITLE_PARTIAL,
                {"english_text": "interim"},
            )
            runtime.publisher.publish(
                session_id, WebSocketEventKind.SUBTITLE_CURRENT,
                {"sequence": 0, "segment_id": "s0", "english_text": "committed"},
            )
            ws.send_text("ping")
            seen = []
            while True:
                message = ws.receive_json()
                if message["kind"] == "system.pong":
                    break
                seen.append(message)

    kinds = [m["kind"] for m in seen]
    assert WebSocketEventKind.SUBTITLE_PARTIAL in kinds
    assert WebSocketEventKind.SUBTITLE_CURRENT in kinds, (
        "the committed caption published after a partial was swallowed -- the "
        "partial advanced the cursor past it"
    )
