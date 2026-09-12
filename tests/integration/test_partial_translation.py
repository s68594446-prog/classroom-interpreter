"""Interim translation -- the half of simultaneous interpretation that was missing.

Interim transcription (D-046) only ever produced English. Chinese came solely
from sealed segments, so the first word of a 6.5s sentence waited the whole
sentence plus ~3.4s of provider round trip -- about 10 seconds -- before any
Chinese existed for it. That is a caption, not simultaneous interpretation.

The constraint is the same one that governed interim transcription, and it is
the reason this is allowed to exist at all: a partial translation must be
incapable of becoming a fact. It has no segment id, no sequence, no
`english_text_sha256`, and no context snapshot, so there is nothing to commit
and nothing for the summary to read.
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
from classroom_interpreter.runtime.pipeline import _has_moved_enough

from test_live_api import make_client
from test_purge import CONTENT_TABLES


# --------------------------------------------------------------- the gate


def test_a_single_extra_word_is_not_worth_a_paid_call():
    """Mutation that turns this red: always return True."""
    assert not _has_moved_enough("the combined ratio is", "the combined ratio is about", 3)


def test_enough_new_words_is_worth_a_call():
    assert _has_moved_enough(
        "the combined ratio is", "the combined ratio is about ninety five percent", 3
    )


def test_a_revision_always_counts_even_when_it_is_shorter():
    """The transcriber correcting itself is exactly when a re-translation matters.

    Mutation that turns this red: compare only word counts.
    """
    assert _has_moved_enough("the agents coming", "the agent commission", 3)


def test_the_first_partial_always_translates():
    assert _has_moved_enough("", "value at risk", 3)


# ------------------------------------------------------------- the wiring


class _FakeProviders:
    name = "fake"
    model = "fake"

    def __init__(self, english="value at risk measures the worst loss"):
        self.english = english
        self.asr_calls = 0
        self.translate_calls = 0

    async def transcribe_bytes(self, wav_bytes: bytes) -> str:
        self.asr_calls += 1
        return self.english

    async def translate_text(self, english_text: str) -> str:
        self.translate_calls += 1
        return "风险价值衡量最坏损失"


def _arm(runtime, providers, **settings):
    runtime.asr_provider = providers
    runtime.translation_provider = providers
    runtime.settings = runtime.settings.model_copy(update=settings)
    _always_has_pending_audio(runtime)


def test_a_chinese_partial_reaches_a_websocket_client(tmp_path):
    """Mutation that turns this red: drop the second `publish_ephemeral`."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    providers = _FakeProviders()
    with client:
        view = client.post("/api/sessions", json={"course_name": "ZhPartial"}).json()
        session_id = view["session"]["id"]
        _arm(runtime, providers)

        with client.websocket_connect(f"/ws/sessions/{session_id}?cursor=-1") as ws:
            assert ws.receive_json()["kind"] == "system.ready"
            chinese = asyncio.run(providers.translate_text("x"))
            runtime.publisher.publish_ephemeral(
                session_id,
                WebSocketEventKind.SUBTITLE_PARTIAL,
                {"english_text": providers.english, "chinese_text": chinese},
            )
            ws.send_text("ping")
            seen = []
            while True:
                message = ws.receive_json()
                if message["kind"] == "system.pong":
                    break
                seen.append(message)

    partials = [m for m in seen if m["kind"] == WebSocketEventKind.SUBTITLE_PARTIAL]
    assert partials, "no interim caption reached the client at all"
    assert partials[-1]["payload"]["chinese_text"] == "风险价值衡量最坏损失"
    assert partials[-1]["event_sequence"] == EPHEMERAL_EVENT_SEQUENCE


def test_a_partial_translation_leaves_no_durable_trace(tmp_path):
    """THE load-bearing test, and the condition for shipping this at all.

    A partial translation is a guess about a sentence that is not finished. If
    one reached `translation_results` it would be bound to an
    `english_text_sha256` for text that was never committed; if it reached
    `websocket_events` -- the replay log -- a reconnecting client would be
    handed stale half-sentences as though they were the record.

    Mutation that turns this red: publish through `publish()` instead of
    `publish_ephemeral()`.
    """
    marker = "本句仅为中间结果"
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "NoTrace"}).json()
        session_id = view["session"]["id"]
        runtime.publisher.publish_ephemeral(
            session_id,
            WebSocketEventKind.SUBTITLE_PARTIAL,
            {"english_text": "interim english", "chinese_text": marker},
        )

    for table, column in (
        ("translation_results", "chinese_text"),
        ("websocket_events", "payload_json"),
    ):
        with database.connect() as connection:
            leaked = connection.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} LIKE ?",
                (f"%{marker}%",),
            ).fetchone()["n"]
        assert leaked == 0, f"partial translation reached {table}"
        assert table in CONTENT_TABLES


def test_english_is_published_without_waiting_for_the_chinese(tmp_path):
    """Blocking the English caption on the translation would undo D-046.

    The English partial exists precisely so a caption does not have to wait for
    a translation round trip. If the loop published them together, the English
    would become as slow as the Chinese.

    Mutation that turns this red: publish one combined event instead of two.
    """
    import inspect

    from classroom_interpreter.runtime import pipeline

    source = inspect.getsource(pipeline.SessionOrchestrator._partial_loop)
    english_only = source.index('{"english_text": text}')
    with_chinese = source.index('"chinese_text": chinese')
    assert english_only < with_chinese, (
        "the English-only publish must come first; otherwise the English "
        "caption waits on the translation"
    )


def _always_has_pending_audio(runtime):
    """Pin the segmenter's pending-audio answer for the duration of a test.

    The live segmenter loop re-derives its state from the durable cursor every
    poll, so state injected from the test is wiped within 100ms. The subject
    here is the partial loop's own decisions -- when it transcribes, when it
    pays for a translation -- not the segmenter's bookkeeping.
    """
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 16000 * 3)
    data = buffer.getvalue()
    runtime._segmenter.pending_utterance_wav = lambda *a, **k: data


def _drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _let_the_loop_run(runtime, seconds):
    """Wait in real time.

    The partial loop lives in the app's own event loop, which TestClient runs
    on a background thread. Driving it with `asyncio.run` would build a second
    loop and the orchestrator's `asyncio.Event` would belong to neither.
    """
    import time as _time

    _time.sleep(seconds)


def test_zero_disables_interim_translation(tmp_path):
    """It is the most expensive dial in the product; it must be switchable off.

    Mutation that turns this red: remove the `if not translate_every` guard.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    providers = _FakeProviders()
    runtime.settings = runtime.settings.model_copy(
        update={
            "partial_translation_interval_seconds": 0,
            "partial_transcription_interval_seconds": 0.2,
            "partial_transcription_min_audio_ms": 100,
        }
    )
    with client:
        client.post("/api/sessions", json={"course_name": "Off"})
        runtime.asr_provider = providers
        runtime.translation_provider = providers
        _always_has_pending_audio(runtime)
        _let_the_loop_run(runtime, 1.2)

    assert providers.asr_calls >= 1, "interim transcription should still run"
    assert providers.translate_calls == 0, "translation ran despite being disabled"


def test_interim_translation_runs_when_enabled(tmp_path):
    """The positive case, so `test_zero_disables...` cannot pass vacuously.

    Mutation that turns this red: never call `translate_text` in the loop.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    providers = _FakeProviders()
    runtime.settings = runtime.settings.model_copy(
        update={
            "partial_translation_interval_seconds": 0.1,
            "partial_transcription_interval_seconds": 0.2,
            "partial_transcription_min_audio_ms": 100,
        }
    )
    with client:
        session = client.post("/api/sessions", json={"course_name": "On"}).json()
        runtime.asr_provider = providers
        runtime.translation_provider = providers
        _always_has_pending_audio(runtime)
        queue = runtime.publisher.subscribe()
        try:
            _let_the_loop_run(runtime, 1.2)
            events = _drain(queue)
        finally:
            runtime.publisher.unsubscribe(queue)

    assert providers.translate_calls >= 1, "interim translation never ran"
    partials = [e for e in events if e.kind is WebSocketEventKind.SUBTITLE_PARTIAL]
    with_chinese = [e for e in partials if e.payload.get("chinese_text")]
    assert with_chinese, "no partial carried a Chinese line"
    assert all(e.event_sequence == EPHEMERAL_EVENT_SEQUENCE for e in partials)


class _GrowingASR:
    """Returns a sentence that grows by one word per call, as real ASR does.

    A fake that returns the SAME text every call cannot test the translation
    gate at all: the loop's English dedup (`text == last_text`) skips before it
    ever reaches the translation code, so removing the gate changes nothing and
    the test passes while proving nothing.
    """

    name = "growing"
    model = "growing"
    WORDS = ("the", "combined", "ratio", "is", "about", "ninety", "five",
             "percent", "which", "is", "profitable", "overall")

    def __init__(self):
        self.asr_calls = 0
        self.translate_calls = 0

    async def transcribe_bytes(self, wav_bytes: bytes) -> str:
        self.asr_calls += 1
        return " ".join(self.WORDS[: min(self.asr_calls + 1, len(self.WORDS))])

    async def translate_text(self, english_text: str) -> str:
        self.translate_calls += 1
        return "综合成本率约为百分之九十五"


def test_growth_of_one_word_does_not_buy_a_new_translation(tmp_path):
    """The gate, isolated from the English dedup that used to mask it.

    The sentence changes on every single pass, so the English caption updates
    every time and the dedup never fires. Anything that limits the number of
    paid translations here is the gate and nothing else.

    Mutation that turns this red: drop the `_has_moved_enough` check.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    providers = _GrowingASR()
    runtime.settings = runtime.settings.model_copy(
        update={
            "partial_translation_interval_seconds": 0.01,
            "partial_translation_min_new_words": 3,
            "partial_transcription_interval_seconds": 0.05,
            "partial_transcription_min_audio_ms": 100,
        }
    )
    with client:
        client.post("/api/sessions", json={"course_name": "Gate"})
        runtime.asr_provider = providers
        runtime.translation_provider = providers
        _always_has_pending_audio(runtime)
        _let_the_loop_run(runtime, 1.2)

    assert providers.asr_calls >= 6, "the loop did not run often enough to judge"
    assert providers.translate_calls >= 1, "the gate blocked everything"
    assert providers.translate_calls * 2 <= providers.asr_calls, (
        f"{providers.translate_calls} paid translations for "
        f"{providers.asr_calls} transcriptions -- the gate is not holding, and "
        "every one of those is money spent to add a word to a line already on "
        "screen"
    )


def test_the_loop_itself_writes_no_durable_partial(tmp_path):
    """Exercises the real loop, not a hand-made `publish_ephemeral` call.

    `test_a_partial_translation_leaves_no_durable_trace` publishes directly, so
    it verifies the publisher rather than the loop's use of it.

    Mutation that turns this red: publish partials with `publish()` in
    `_partial_loop`.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    providers = _GrowingASR()
    runtime.settings = runtime.settings.model_copy(
        update={
            "partial_translation_interval_seconds": 0.01,
            "partial_transcription_interval_seconds": 0.05,
            "partial_transcription_min_audio_ms": 100,
        }
    )
    with client:
        client.post("/api/sessions", json={"course_name": "NoDurable"})
        runtime.asr_provider = providers
        runtime.translation_provider = providers
        _always_has_pending_audio(runtime)
        _let_the_loop_run(runtime, 1.0)

    assert providers.translate_calls >= 1, "nothing was translated, so nothing was proved"
    with database.connect() as connection:
        leaked = connection.execute(
            "SELECT COUNT(*) AS n FROM websocket_events WHERE kind = ?",
            (WebSocketEventKind.SUBTITLE_PARTIAL.value,),
        ).fetchone()["n"]
    assert leaked == 0, (
        "the partial loop wrote interim text to websocket_events, which is the "
        "replay log -- a reconnecting client would be handed half-sentences "
        "from minutes ago as though they were the record"
    )


def test_a_provider_without_translate_text_still_shows_english(tmp_path):
    """Degrade, never regress.

    Every failure in this loop is swallowed so a partial cannot disturb the
    durable pipeline. That makes it easy for a provider missing the method to
    silently take the English caption down with it.
    """

    class _AsrOnly:
        name = "asr-only"
        model = "asr-only"

        def __init__(self):
            self.calls = 0

        async def transcribe_bytes(self, wav_bytes: bytes) -> str:
            self.calls += 1
            return "value at risk measures the worst loss"

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    providers = _AsrOnly()
    runtime.settings = runtime.settings.model_copy(
        update={
            "partial_translation_interval_seconds": 0.1,
            "partial_transcription_interval_seconds": 0.2,
            "partial_transcription_min_audio_ms": 100,
        }
    )
    with client:
        client.post("/api/sessions", json={"course_name": "Degrade"})
        runtime.asr_provider = providers
        runtime.translation_provider = object()          # no translate_text
        _always_has_pending_audio(runtime)
        queue = runtime.publisher.subscribe()
        try:
            _let_the_loop_run(runtime, 1.2)
            events = _drain(queue)
        finally:
            runtime.publisher.unsubscribe(queue)

    partials = [e for e in events if e.kind is WebSocketEventKind.SUBTITLE_PARTIAL]
    assert partials, "a provider without translate_text silenced the English caption too"
    assert partials[0].payload["english_text"]
    assert "chinese_text" not in partials[0].payload


def test_settings_expose_the_cost_dials(tmp_path):
    settings = Settings(_env_file=None, classroom_interpreter_data_dir=tmp_path / "d")
    assert settings.partial_translation_interval_seconds >= (
        settings.partial_transcription_interval_seconds
    ), (
        "translating more often than transcribing would pay for translations "
        "of text that has not changed"
    )
    assert settings.partial_translation_min_new_words >= 1
