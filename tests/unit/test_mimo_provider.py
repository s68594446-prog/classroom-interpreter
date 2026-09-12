from __future__ import annotations

import asyncio
import base64
import json

import pytest

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    ContextWindow,
    ProviderErrorCategory,
    TranslationRequest,
)
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mimo import (
    MAX_ENCODED_AUDIO_BYTES,
    MimoASRProvider,
    MimoTranslationProvider,
    ProviderHttpResponse,
    build_asr_payload,
    classify_http_status,
    parse_asr_response,
)
from pathlib import Path


def make_ref(tmp_path: Path, size_seconds: float = 0.1) -> AudioSegmentRef:
    wav = tmp_path / "seg_000000.wav"
    # Minimal valid PCM WAV payload (header + silence), big enough for the
    # happy path; payload content is irrelevant to the contract tests.
    import wave

    with wave.open(str(wav), "wb") as handle:
        handle.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\x00\x00" * int(16_000 * size_seconds))
    return AudioSegmentRef(
        segment_id="seg-1",
        session_id="session-1",
        sequence=0,
        audio_path=wav,
        audio_sha256="0" * 64,
        duration_ms=100,
    )


class FakePost:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        result = self.responder(url, headers, payload)
        if isinstance(result, Exception):
            raise result
        return result


def response(status=200, body=b"", headers=None):
    from classroom_interpreter.providers.mimo import ProviderHttpResponse

    return ProviderHttpResponse(status=status, headers=headers or {}, body=body)


def valid_body(text="Hello class.", request_id="req-7"):
    return json.dumps(
        {
            "id": request_id,
            "object": "chat.completion",
            "model": "mimo-v2.5-asr",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"seconds": 1.0},
        }
    ).encode()


def test_payload_contract_matches_documented_mimo_shape():
    wav = b"RIFF-fake-wav-bytes"
    payload = build_asr_payload(wav, model="mimo-v2.5-asr", language="en")
    assert payload["model"] == "mimo-v2.5-asr"
    assert payload["stream"] is False
    assert payload["asr_options"] == {"language": "en"}
    (message,) = payload["messages"]
    assert message["role"] == "user"
    (part,) = message["content"]
    assert part["type"] == "input_audio"
    assert part["input_audio"]["format"] == "wav"
    encoded = part["input_audio"]["data"]
    assert encoded.startswith("data:audio/wav;base64,")
    assert base64.b64decode(encoded.split(",", 1)[1]) == wav


def test_payload_gate_fails_closed_over_10mb_without_network():
    big = b"\x00" * (MAX_ENCODED_AUDIO_BYTES)  # encodes above the 10 MB limit
    post = FakePost(lambda *a: (_ for _ in ()).throw(AssertionError("must not dial")))
    with pytest.raises(ProviderFailure) as excinfo:
        build_asr_payload(big, model="m", language="en")
    assert excinfo.value.error.category is ProviderErrorCategory.MALFORMED
    assert excinfo.value.error.retryable is False


def test_empty_payload_rejected_locally():
    with pytest.raises(ProviderFailure):
        build_asr_payload(b"", model="m", language="en")


def test_parse_response_happy_path():
    text, request_id = parse_asr_response(valid_body())
    assert text == "Hello class."
    assert request_id == "req-7"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        json.dumps({"choices": []}).encode(),
        json.dumps({"choices": [{"message": {"content": ""}}]}).encode(),
        json.dumps({"choices": [{"message": {}}]}).encode(),
        json.dumps({"choices": [{"message": {"content": "  "}}]}).encode(),
        json.dumps({"error": {"code": "InternalError"}}).encode(),
    ],
)
def test_parse_response_malformed_variants(body):
    with pytest.raises(ProviderFailure) as excinfo:
        parse_asr_response(body)
    assert excinfo.value.error.category is ProviderErrorCategory.MALFORMED


def test_parse_response_content_filter_variants():
    body = json.dumps(
        {"choices": [{"finish_reason": "content_filter", "message": {"content": "x"}}]}
    ).encode()
    with pytest.raises(ProviderFailure) as excinfo:
        parse_asr_response(body)
    assert excinfo.value.error.category is ProviderErrorCategory.CONTENT_FILTER
    assert excinfo.value.error.retryable is False

    body = json.dumps({"error": {"code": "content_filter.blocked"}}).encode()
    with pytest.raises(ProviderFailure) as excinfo:
        parse_asr_response(body)
    assert excinfo.value.error.category is ProviderErrorCategory.CONTENT_FILTER


@pytest.mark.parametrize(
    "status,category,retryable",
    [
        (401, ProviderErrorCategory.AUTH, False),
        (403, ProviderErrorCategory.AUTH, False),
        (402, ProviderErrorCategory.BALANCE, False),
        (429, ProviderErrorCategory.RATE_LIMIT, True),
        (500, ProviderErrorCategory.SERVER, True),
        (503, ProviderErrorCategory.SERVER, True),
        (400, ProviderErrorCategory.MALFORMED, True),
        (418, ProviderErrorCategory.MALFORMED, True),
    ],
)
def test_status_classification_matrix(status, category, retryable):
    error = classify_http_status(status, {})
    assert error.category is category
    assert error.retryable is retryable
    assert error.http_status == status


def test_retry_after_header_parsing():
    assert classify_http_status(429, {"Retry-After": "2"}).retry_after_ms == 2000
    assert classify_http_status(429, {"Retry-After": "0.5"}).retry_after_ms == 500
    assert classify_http_status(429, {"Retry-After": "garbage"}).retry_after_ms is None
    assert classify_http_status(429, {}).retry_after_ms is None


def test_missing_api_key_is_configuration_error_not_mock():
    with pytest.raises(ValueError):
        MimoASRProvider(api_key=None, base_url="https://x/v1", model="mimo-v2.5-asr")


def test_transcribe_happy_path_and_request_shape(tmp_path):
    post = FakePost(lambda url, headers, payload: response(body=valid_body()))
    provider = MimoASRProvider(
        api_key="test-key",
        base_url="https://api.xiaomimimo.com/v1/",
        model="mimo-v2.5-asr",
        post=post,
    )
    result = asyncio.run(provider.transcribe(make_ref(tmp_path)))
    assert result.english_text == "Hello class."
    assert result.provider == "mimo"
    assert result.provider_request_id == "req-7"
    assert result.audio_seconds == pytest.approx(0.1)
    (url, headers, payload), = post.calls
    assert url == "https://api.xiaomimimo.com/v1/chat/completions"
    assert headers["api-key"] == "test-key"
    assert payload["asr_options"]["language"] == "en"


def test_transcribe_timeout_and_network(tmp_path):
    async def slow(url, headers, payload):
        await asyncio.sleep(0.2)
        return response(body=valid_body())

    provider = MimoASRProvider(
        api_key="k",
        base_url="https://x/v1",
        model="m",
        timeout_seconds=0.05,
        post=slow,
    )
    with pytest.raises(ProviderFailure) as excinfo:
        asyncio.run(provider.transcribe(make_ref(tmp_path)))
    assert excinfo.value.error.category is ProviderErrorCategory.TIMEOUT
    assert excinfo.value.error.retryable is True

    post = FakePost(lambda *a: ConnectionError("down"))
    provider = MimoASRProvider(api_key="k", base_url="https://x/v1", model="m", post=post)
    with pytest.raises(ProviderFailure) as excinfo:
        asyncio.run(provider.transcribe(make_ref(tmp_path)))
    assert excinfo.value.error.category is ProviderErrorCategory.NETWORK


def test_transcribe_http_error_classification(tmp_path):
    post = FakePost(lambda *a: response(status=429, headers={"Retry-After": "3"}))
    provider = MimoASRProvider(api_key="k", base_url="https://x/v1", model="m", post=post)
    with pytest.raises(ProviderFailure) as excinfo:
        asyncio.run(provider.transcribe(make_ref(tmp_path)))
    assert excinfo.value.error.category is ProviderErrorCategory.RATE_LIMIT
    assert excinfo.value.error.retry_after_ms == 3000

    post = FakePost(lambda *a: response(status=401))
    provider = MimoASRProvider(api_key="k", base_url="https://x/v1", model="m", post=post)
    with pytest.raises(ProviderFailure) as excinfo:
        asyncio.run(provider.transcribe(make_ref(tmp_path)))
    assert excinfo.value.error.category is ProviderErrorCategory.AUTH
    assert excinfo.value.error.retryable is False


# --- Real-API-shape regressions (recorded from the live provider 2026-09-11) ---
#
# Every raw string below was returned by the production `mimo-v2.5-asr`
# endpoint for real synthesized lecture audio. They are kept verbatim so the
# parser is pinned to what the provider actually emits, not to what the docs
# describe.

REAL_ASR_CONTENT_SAMPLES = [
    # Same request shape, three different wrappers -- it is not stable per call.
    (
        "think>\n<chinese> Today we will derive the Black-Scholes equation from "
        "first principles. Value at risk measures the worst expected loss at a "
        "given confidence level.",
        "Today we will derive the Black-Scholes equation from first principles. "
        "Value at risk measures the worst expected loss at a given confidence level.",
    ),
    ("<chinese> Good morning, everyone.", "Good morning, everyone."),
    (
        " The portfolio beta is 1.35, and the annualized volatility reached 22% in Q3 2024.",
        "The portfolio beta is 1.35, and the annualized volatility reached 22% in Q3 2024.",
    ),
]


@pytest.mark.parametrize("raw,expected", REAL_ASR_CONTENT_SAMPLES)
def test_asr_parser_strips_real_chat_template_scaffolding(raw, expected):
    """The English transcript is the factual source for the summary.

    Scaffolding tokens the ASR chat template emits must never reach it. These
    wrappers cannot be disabled at the request level: `thinking.type=disabled`
    and `chat_template_kwargs.enable_thinking=False` both still produced them
    against the live API (they do work for the text models), so stripping here
    is the only place this can be fixed.

    Mutation that turns this red: return `text.strip()` from
    `parse_asr_response` without calling `strip_asr_scaffolding`.
    """
    body = json.dumps(
        {"id": "req-1", "choices": [{"message": {"content": raw}}]}
    ).encode()
    text, request_id = parse_asr_response(body)
    assert text == expected
    assert request_id == "req-1"


@pytest.mark.parametrize(
    "spoken",
    [
        "Now think about what the theorem actually means.",
        "We use <english> tags when parsing XML documents.",
        "1. Consider a European call option with strike price K.",
        "The chinese economy grew last quarter.",
    ],
)
def test_asr_parser_never_edits_the_lecturer_s_own_words(spoken):
    """Stripping must not chew into real speech.

    The guard only removes a known tag when the remaining text *starts* with
    it, so words like "think" or a spoken "<english>" mid-sentence survive
    intact. A transcript that silently loses its first clause would corrupt the
    summary while every other check still passed.

    Mutation that turns this red: match these markers anywhere in the string
    (e.g. `replace()` instead of a prefix test).
    """
    body = json.dumps(
        {"id": "r", "choices": [{"message": {"content": spoken}}]}
    ).encode()
    text, _ = parse_asr_response(body)
    assert text == spoken


def test_translation_payload_disables_provider_side_reasoning():
    """Translation must ship the one thinking-off spelling that works.

    Measured live on 2026-09-11: default reasoning put p50 latency at 5.40s
    (max 8.31s) and ~152 completion tokens per segment; with this flag the same
    five sentences ran at p50 2.29s (max 2.54s) and ~14 tokens, with no quality
    loss. A five-second-late caption is not realtime, so this is a correctness
    property of the feature, not a tuning preference.

    Only this spelling takes effect -- top-level `enable_thinking` and
    `reasoning_effort` were both ignored by the provider.

    Mutation that turns this red: drop `**NO_THINKING` from the payload.
    """
    captured: dict = {}

    async def fake_post(url, headers, payload):
        captured.update(payload)
        return ProviderHttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {"id": "x", "choices": [{"message": {"content": "测试译文。"}}]}
            ).encode(),
        )

    provider = MimoTranslationProvider(
        api_key="test-key",
        model="mimo-v2.5",
        base_url="https://example.invalid/v1",
        post=fake_post,
    )
    request = TranslationRequest(
        segment_id="seg-1",
        session_id="sess-1",
        sequence=0,
        current_english_text="Hello.",
        context=ContextWindow(
            session_id="sess-1",
            current_segment_id="seg-1",
            previous_segments=(),
            window_start_ms=0,
            window_end_ms=1000,
            estimated_tokens=4,
            current_english_text="Hello.",
        ),
    )
    asyncio.run(provider.translate(request))
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_summary_keeps_provider_side_reasoning():
    """Summary is offline and quality-sensitive, so it must NOT disable thinking.

    Pins the deliberate asymmetry with translation: if someone later applies
    NO_THINKING globally "for consistency", the summary silently loses its
    reasoning budget for no latency benefit -- nobody is waiting on it live.
    """
    import inspect

    from classroom_interpreter.providers import mimo as mimo_module

    source = inspect.getsource(mimo_module.MimoSummaryProvider)
    assert "NO_THINKING" not in source


def test_interim_strips_a_leading_think_that_the_transcript_keeps():
    """Observed on real audio: `think whatever happens, whether you succeed`.

    The durable stripper deliberately leaves a bare leading "think", because at
    the start of a fragment it cannot be told apart from a lecturer saying "I
    think". That trade-off is right for a permanent record and wrong for a line
    that is replaced two seconds later and never persisted.

    Mutation that turns this red: return `strip_asr_scaffolding(text)` from
    `strip_interim_scaffolding`.
    """
    from classroom_interpreter.providers.mimo import (
        strip_asr_scaffolding,
        strip_interim_scaffolding,
    )

    raw = "think whatever happens, whether you succeed or you fail"
    assert strip_asr_scaffolding(raw) == raw, "the durable path must not change"
    assert strip_interim_scaffolding(raw) == (
        "whatever happens, whether you succeed or you fail"
    )


def test_interim_stripping_never_touches_the_durable_transcript():
    """The committed English is the summary's factual source; it is untouched.

    Mutation that turns this red: call `strip_interim_scaffolding` from
    `parse_asr_response`.
    """
    from classroom_interpreter.providers.mimo import parse_asr_response

    body = (
        '{"choices":[{"message":{"content":'
        '"think about what the theorem actually means"}}]}'
    )
    text, _ = parse_asr_response(body)
    assert text == "think about what the theorem actually means", (
        "a lecturer's own word was deleted from the permanent record"
    )


def test_interim_keeps_a_sentence_that_merely_contains_think():
    from classroom_interpreter.providers.mimo import strip_interim_scaffolding

    assert strip_interim_scaffolding("I think we should stop here") == (
        "I think we should stop here"
    )
    assert strip_interim_scaffolding("thinking about it differently") == (
        "thinking about it differently"
    )


def test_the_http_client_is_reused_across_requests(monkeypatch):
    """A new client per request means a new TLS handshake per request.

    Every ASR call, every translation and every interim caption paid for a
    fresh TCP connection and a full handshake -- about two extra round trips
    before any audio was sent -- and interim transcription multiplied the call
    count, and the handshakes with it.

    Mutation that turns this red: go back to `async with
    httpx2.AsyncClient(...)` around each request.
    """
    import asyncio

    import httpx2

    from classroom_interpreter.providers.mimo import _default_post

    built = []

    class _FakeResponse:
        status_code = 200
        headers: dict = {}
        content = b"{}"

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            built.append(self)
            self.is_closed = False
            self.posts = 0

        async def post(self, *args, **kwargs):
            self.posts += 1
            return _FakeResponse()

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(httpx2, "AsyncClient", _FakeClient)
    post = _default_post("k", "https://example.invalid", 5.0)

    async def exercise():
        for _ in range(3):
            await post("https://example.invalid/x", {}, {})

    asyncio.run(exercise())

    assert len(built) == 1, f"opened {len(built)} clients for 3 requests"
    assert built[0].posts == 3


def test_a_client_from_a_dead_event_loop_is_replaced(monkeypatch):
    """The cache must be per-loop: an httpx client bound to a closed loop
    fails on its next request, and the TestClient runs its own loop.

    Mutation that turns this red: cache the client globally with no loop check.
    """
    import asyncio

    import httpx2

    from classroom_interpreter.providers.mimo import _default_post

    built = []

    class _FakeResponse:
        status_code = 200
        headers: dict = {}
        content = b"{}"

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            built.append(self)
            self.is_closed = False

        async def post(self, *args, **kwargs):
            return _FakeResponse()

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(httpx2, "AsyncClient", _FakeClient)
    post = _default_post("k", "https://example.invalid", 5.0)

    async def once():
        await post("https://example.invalid/x", {}, {})

    asyncio.run(once())  # one loop
    asyncio.run(once())  # a different loop

    assert len(built) == 2
