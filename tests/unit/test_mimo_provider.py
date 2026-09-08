from __future__ import annotations

import asyncio
import base64
import json

import pytest

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    ProviderErrorCategory,
)
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mimo import (
    MAX_ENCODED_AUDIO_BYTES,
    MimoASRProvider,
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
