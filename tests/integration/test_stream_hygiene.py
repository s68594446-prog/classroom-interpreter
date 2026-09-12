"""Three ways the live stream could go quietly wrong.

1. Subscribers are process-wide, not per-session. The publisher fans every
   session's events out to every open socket, and the connection handler never
   checked which session an event belonged to. A second tab left open on an
   earlier class was shown the current class's captions -- interim ones
   unconditionally, because they bypass the replay cursor by design.

2. A subscriber that falls 1024 events behind was discarded in silence. The
   socket stayed open, delivered nothing ever again, and never closed -- so the
   client's reconnect (which would have replayed the gap from its cursor) never
   fired. The screen freezes with 已连接 still showing.

3. The interim-translation interval is a throttle *within* one sentence. It was
   carried across the gap between sentences, so the first Chinese line of every
   new sentence waited out the remainder of the previous sentence's interval.
"""

from __future__ import annotations

import time

from classroom_interpreter.domain.models import (
    EPHEMERAL_EVENT_SEQUENCE,
    WebSocketEvent,
    WebSocketEventKind,
)

from test_live_api import make_client, receive_json_or_fail
from test_partial_translation import _always_has_pending_audio, _let_the_loop_run


def _filler(session_id: str) -> WebSocketEvent:
    """A real event, so a full queue is filled the way the publisher fills it."""
    return WebSocketEvent(
        event_sequence=EPHEMERAL_EVENT_SEQUENCE,
        kind=WebSocketEventKind.SUBTITLE_PARTIAL,
        session_id=session_id,
        payload={"english_text": "filler"},
    )


# ------------------------------------------------ 1. cross-session delivery


def test_a_socket_is_not_shown_another_classs_captions(tmp_path):
    """Mutation that turns this red: drop the `event.session_id != session_id`
    guard in the websocket loop."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Watched"}).json()
        watched = view["session"]["id"]
        other = runtime.repository.create_session("Elsewhere", app_version="t").id
        with client.websocket_connect(f"/ws/sessions/{watched}?cursor=99999") as ws:
            receive_json_or_fail(ws, what="system.ready")
            runtime.publisher.publish_ephemeral(
                other,
                WebSocketEventKind.SUBTITLE_PARTIAL,
                {"english_text": "a sentence from a different class"},
            )
            runtime.publisher.publish_ephemeral(
                watched,
                WebSocketEventKind.SUBTITLE_PARTIAL,
                {"english_text": "this class"},
            )
            frame = receive_json_or_fail(ws, what="the watched session's partial")

    assert frame["session_id"] == watched
    assert frame["payload"]["english_text"] == "this class", (
        "the socket delivered a caption belonging to another class"
    )


# ------------------------------------------------- 2. a dropped subscriber


def test_a_full_subscriber_is_flagged_rather_than_forgotten(tmp_path):
    """Mutation that turns this red: drop the `setattr(queue, "dropped", True)`
    line in `_fan_out`."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Full"}).json()
        session_id = view["session"]["id"]
        queue = runtime.publisher.subscribe()
        while not queue.full():
            queue.put_nowait(_filler(session_id))
        runtime.publisher.publish_ephemeral(
            session_id, WebSocketEventKind.SUBTITLE_PARTIAL, {"english_text": "x"}
        )

    assert queue.dropped is True, (
        "a subscriber that fell behind was discarded with nothing to say so"
    )
    assert queue not in runtime.publisher._subscribers


def test_a_dropped_subscriber_closes_its_connection(tmp_path):
    """A silent drop is indistinguishable from a quiet lecture. Closing is the
    repair: the client reconnects with its cursor and replays the gap.

    Mutation that turns this red: remove the `queue.dropped` check (or the
    `websocket.close()`) in the websocket loop.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Behind"}).json()
        session_id = view["session"]["id"]
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            receive_json_or_fail(ws, what="system.ready")
            queue = _wait_for_subscriber(runtime)
            # Fill the handler's own queue, as a stalled client would: the
            # lecture keeps producing while nothing is being read.
            while not queue.full():
                queue.put_nowait(_filler(session_id))
            runtime.publisher.publish_ephemeral(
                session_id, WebSocketEventKind.SUBTITLE_PARTIAL, {"english_text": "x"}
            )
            assert queue.dropped is True
            closed = _closed_within(ws, seconds=10.0)

    assert closed, (
        "the connection stayed open after its subscriber was dropped, so the "
        "client would never reconnect and the screen would stay frozen"
    )


def _wait_for_subscriber(runtime, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        subscribers = list(runtime.publisher._subscribers)
        if subscribers:
            return subscribers[0]
        time.sleep(0.05)
    raise AssertionError("the connection never subscribed")


def _closed_within(ws, *, seconds: float) -> bool:
    """Drain buffered frames until the socket closes, or give up.

    Each read runs in its own daemon thread: the test client blocks forever on
    an empty queue, and a connection that never closes is precisely the bug
    under test, so a blocked read must fail rather than hang the suite.
    """
    import queue as _queue
    import threading

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        inbox: "_queue.Queue" = _queue.Queue(maxsize=1)

        def pump() -> None:
            try:
                inbox.put(("message", ws.receive()))
            except BaseException as exc:
                inbox.put(("closed", exc))

        threading.Thread(target=pump, daemon=True, name="ws-drain").start()
        try:
            kind, value = inbox.get(timeout=2.0)
        except _queue.Empty:
            return False
        if kind == "closed":
            return True
        if isinstance(value, dict) and value.get("type") == "websocket.close":
            return True
    return False


# --------------------------------------- 3. the first Chinese of a sentence


class _TwoSentenceASR:
    name = "two-sentence"
    model = "two-sentence"

    def __init__(self):
        self.texts = ["the combined ratio is about", "value at risk measures the"]
        self.index = 0

    async def transcribe_bytes(self, wav_bytes: bytes) -> str:
        return self.texts[min(self.index, len(self.texts) - 1)]


class _CountingTranslator:
    name = "counting"
    model = "counting"

    def __init__(self):
        self.calls = 0

    async def translate_text(self, english_text: str) -> str:
        self.calls += 1
        return "中文"


def test_a_new_sentence_does_not_wait_out_the_old_sentences_throttle(tmp_path):
    """Mutation that turns this red: stop resetting `last_translated_at` (and
    `last_translated_text`) when the open utterance ends.

    The interval here is 30 s, far longer than the test runs. Without the reset
    the second sentence's Chinese could not appear at all; with it, the first
    partial of a new sentence always translates.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    asr = _TwoSentenceASR()
    translator = _CountingTranslator()
    runtime.settings = runtime.settings.model_copy(
        update={
            "partial_translation_interval_seconds": 30.0,
            "partial_transcription_interval_seconds": 0.1,
            "partial_transcription_min_audio_ms": 100,
        }
    )
    with client:
        client.post("/api/sessions", json={"course_name": "Two"})
        runtime.asr_provider = asr
        runtime.translation_provider = translator
        _always_has_pending_audio(runtime)
        _let_the_loop_run(runtime, 0.8)
        assert translator.calls == 1, "the first sentence should translate once"
        # The utterance seals: no open audio for a moment, then a new sentence.
        runtime._segmenter.pending_utterance_wav = lambda *a, **k: b""
        _let_the_loop_run(runtime, 0.5)
        asr.index = 1
        _always_has_pending_audio(runtime)
        _let_the_loop_run(runtime, 0.8)

    assert translator.calls >= 2, (
        "the new sentence's Chinese was held back by the previous sentence's "
        "throttle"
    )
