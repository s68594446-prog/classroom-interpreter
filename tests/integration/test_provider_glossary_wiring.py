"""A constructor argument that is accepted and never stored is a dead feature.

`MimoASRProvider.__init__` took `glossary_provider` and never assigned it.
Two things followed, and both were reported as working:

  * `transcribe_bytes` called `self._glossary()`, raised AttributeError on
    every call, and the blanket `except Exception: return ""` turned that into
    an empty transcript. Interim captions produced nothing, silently, for as
    long as the feature existed.
  * `transcribe` -- the durable path -- never passed a glossary at all, so
    D-045's per-course terminology reached the translator and never the
    recogniser. The 3.1% -> 1.7% word-error measurement behind it was taken
    with a direct probe rather than through this adapter.

Neither was visible from outside: one returned "", the other returned a
perfectly good transcript that simply had not been told the vocabulary.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from classroom_interpreter.domain.models import AudioSegmentRef
from classroom_interpreter.providers.mimo import MimoASRProvider

TERMS = ({"term": "Bornhuetter-Ferguson", "preferred_chinese": "B-F 法"},)


def capturing_post(captured, body='{"choices":[{"message":{"content":"ok"}}]}'):
    async def post(url, headers, payload):
        captured.append(payload)
        return SimpleNamespace(status=200, body=body, headers={})
    return post


def make_provider(captured, glossary=TERMS):
    return MimoASRProvider(
        api_key="k", base_url="https://example.invalid", model="m",
        post=capturing_post(captured), glossary_provider=lambda: glossary,
    )


def payload_text(payload) -> str:
    """Flatten a chat payload to the text a model would actually see."""
    out = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    out.append(str(part.get("text", "")))
    return " ".join(out)


def test_the_provider_keeps_the_glossary_it_was_given():
    """Mutation that turns this red: drop the `self._glossary =` assignment."""
    provider = make_provider([])
    assert hasattr(provider, "_glossary"), (
        "glossary_provider was accepted and thrown away; every call that reads "
        "it raises AttributeError"
    )
    assert list(provider._glossary()) == list(TERMS)


def test_interim_transcription_actually_returns_text():
    """The symptom the owner saw: interim captions, always empty.

    Mutation that turns this red: drop the `self._glossary =` assignment. The
    AttributeError is swallowed and this returns "".
    """
    provider = make_provider([])
    text = asyncio.run(provider.transcribe_bytes(b"RIFFfake"))
    assert text == "ok", (
        "transcribe_bytes returned empty text -- this is what produced zero "
        "interim captions in a live run while everything looked healthy"
    )


def test_interim_transcription_sends_the_glossary():
    captured = []
    provider = make_provider(captured)
    asyncio.run(provider.transcribe_bytes(b"RIFFfake"))
    assert captured, "no request was made"
    assert "Bornhuetter-Ferguson" in payload_text(captured[0])


def test_the_durable_asr_path_sends_the_glossary(tmp_path):
    """D-045's whole point, and it was reaching the translator only.

    Mutation that turns this red: drop `glossary=self._glossary()` from the
    `build_asr_payload` call in `transcribe`.
    """
    import wave

    audio = tmp_path / "seg.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 16000)

    captured = []
    provider = make_provider(captured)
    segment = AudioSegmentRef(
        segment_id="s1", session_id="sess", sequence=0,
        audio_path=audio, audio_sha256="0" * 64, duration_ms=1000,
    )
    asyncio.run(provider.transcribe(segment))
    assert captured, "no request was made"
    assert "Bornhuetter-Ferguson" in payload_text(captured[0]), (
        "the recogniser was never told the course vocabulary, which is the "
        "entire feature"
    )


def test_a_programming_error_inside_the_request_is_not_swallowed():
    """The defect underneath the defect.

    `except Exception: return ""` gives a bug in this method the same outcome as
    a timeout, and this project has already lost a whole feature that way. The
    handler now covers the failures it is meant to tolerate and nothing else.

    (The original AttributeError was raised *above* the try block and swallowed
    by the partial loop instead, so an earlier version of this test passed under
    both the narrow and the blanket handler -- it proved nothing. This one
    raises from inside the request, where the handler actually applies.)

    Mutation that turns this red: widen the handler back to `except Exception`.
    """
    async def buggy_post(url, headers, payload):
        raise TypeError("a plain bug in the request path")

    provider = MimoASRProvider(
        api_key="k", base_url="https://example.invalid", model="m",
        post=buggy_post, glossary_provider=lambda: (),
    )
    with pytest.raises(TypeError):
        asyncio.run(provider.transcribe_bytes(b"RIFFfake"))


def test_a_real_network_failure_is_still_tolerated():
    """Narrowing the handler must not make a partial able to break a class."""
    async def failing_post(url, headers, payload):
        raise ConnectionError("no route to host")

    provider = MimoASRProvider(
        api_key="k", base_url="https://example.invalid", model="m",
        post=failing_post, glossary_provider=lambda: (),
    )
    assert asyncio.run(provider.transcribe_bytes(b"RIFFfake")) == ""


def test_a_timeout_is_still_tolerated():
    async def slow_post(url, headers, payload):
        await asyncio.sleep(10)

    provider = MimoASRProvider(
        api_key="k", base_url="https://example.invalid", model="m",
        post=slow_post, glossary_provider=lambda: (), timeout_seconds=0.05,
    )
    assert asyncio.run(provider.transcribe_bytes(b"RIFFfake")) == ""
