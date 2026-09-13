from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from classroom_interpreter.domain.models import (
    AudioSegmentRef,
    ProviderError,
    ProviderErrorCategory,
    TranscriptResult,
    TranslationRequest,
    TranslationResult,
    utc_now,
)
from .errors import EmptyTranscript, ProviderFailure

# Official MiMo documentation: encoded audio payload limit is 10 MB.
MAX_ENCODED_AUDIO_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0

# Categories that must never enter an automatic retry loop.
NON_RETRYABLE = frozenset(
    {
        ProviderErrorCategory.AUTH,
        ProviderErrorCategory.BALANCE,
        ProviderErrorCategory.CONTENT_FILTER,
    }
)


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HttpPost = Callable[[str, Mapping[str, str], Mapping[str, Any]], Awaitable[ProviderHttpResponse]]


# A glossary is capped before it reaches a prompt. An unbounded list would grow
# the request on every segment, and past a point extra terms stop helping and
# start crowding the instruction that matters.
MAX_GLOSSARY_TERMS = 60


def render_glossary_hint(terms: Sequence[Mapping[str, Any]], *, for_translation: bool) -> str:
    """Render glossary terms into a prompt fragment, or "" when there are none.

    Two different jobs from one list. For ASR the terms are spelling anchors:
    measured on synthesized lecture audio, residual transcription error was
    almost entirely domain-term spelling rather than misheard speech. For
    translation they pin terminology so the same English term does not drift
    between synonyms from one segment to the next -- a lecture that renders
    "strike price" three different ways is harder to revise from than one
    consistent choice.
    """
    if not terms:
        return ""
    capped = list(terms)[:MAX_GLOSSARY_TERMS]
    if for_translation:
        lines = []
        for entry in capped:
            term = str(entry.get("term", "")).strip()
            if not term:
                continue
            chinese = (entry.get("preferred_chinese") or "").strip()
            lines.append(f"- {term} → {chinese}" if chinese else f"- {term}")
        if not lines:
            return ""
        return (
            "\n\nThis course uses the following terminology. Where a term below has a "
            "Chinese rendering, use exactly that rendering; keep the others "
            "consistent throughout:\n" + "\n".join(lines)
        )
    names = [str(e.get("term", "")).strip() for e in capped]
    names = [n for n in names if n]
    if not names:
        return ""
    return (
        "\n\nThe speaker may have a non-native accent. This lecture uses these exact "
        "terms; prefer them when a word is ambiguous:\n- " + "\n- ".join(names)
    )


def build_asr_payload(
    wav_bytes: bytes,
    *,
    model: str,
    language: str,
    glossary: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the documented chat/completions ASR request body.

    Verbatim official shape (verified 2026-09-08): POST /v1/chat/completions,
    model `mimo-v2.5-asr`, one user message whose content holds a single
    `input_audio` item with a `data:audio/wav;base64,<...>` data URL, and
    `asr_options.language` explicitly set (V1: en).
    """
    if not wav_bytes:
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.MALFORMED,
                retryable=False,
                safe_message="empty segment audio payload",
            )
        )
    encoded = base64.b64encode(wav_bytes).decode("ascii")
    if len(encoded) > MAX_ENCODED_AUDIO_BYTES:
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.MALFORMED,
                retryable=False,
                safe_message="encoded audio payload exceeds provider limit",
            )
        )
    messages: list[dict[str, Any]] = []
    hint = render_glossary_hint(glossary, for_translation=False)
    if hint:
        # A system message is the ONLY channel the real ASR endpoint accepts.
        # Measured against the live API on 2026-09-12: adding a `text` part
        # beside the audio -- in either order -- is rejected with
        # 400 "Param Incorrect", and `asr_options.hotwords` / `asr_options.prompt`
        # are accepted but silently ignored (output unchanged). The system
        # message demonstrably works: the same audio transcribed "Itô's lemma"
        # without it and "Ito's lemma" with it, matching the glossary entry.
        messages.append({"role": "system", "content": hint.strip()})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": f"data:audio/wav;base64,{encoded}",
                        "format": "wav",
                    },
                }
            ],
        }
    )
    return {
        "model": model,
        "messages": messages,
        "asr_options": {"language": language},
        "stream": False,
    }


def parse_asr_response(body: bytes) -> tuple[str, str | None]:
    """Extract (english_text, provider_request_id) from a 200 response."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.MALFORMED,
                retryable=True,
                safe_message="response body is not valid JSON",
            )
        ) from None
    if not isinstance(payload, dict):
        raise ProviderFailure(_malformed("response is not an object"))
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        if "content_filter" in code.lower():
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.CONTENT_FILTER,
                    retryable=False,
                    provider_code=code or None,
                    safe_message="provider returned a content filter code",
                )
            )
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.MALFORMED,
                retryable=True,
                provider_code=code or None,
                safe_message="provider returned an error object",
            )
        )
    choices = payload.get("choices")
    request_id = str(payload["id"]) if isinstance(payload.get("id"), str) else None
    if not isinstance(choices, list) or not choices:
        raise _malformed("response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise _malformed("choice is not an object")
    if first.get("finish_reason") == "content_filter":
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.CONTENT_FILTER,
                retryable=False,
                safe_message="provider finish_reason is content_filter",
            )
        )
    message = first.get("message")
    if not isinstance(message, dict):
        raise _malformed("choice has no message")
    text = message.get("content")
    if not isinstance(text, str):
        raise _malformed("response content is non-text")
    if not text.strip():
        raise EmptyTranscript("the recogniser returned no text")
    stripped = strip_asr_scaffolding(text)
    if not stripped.strip():
        # The whole response was chat-template scaffolding. Constructing a
        # TranscriptResult from it raised a pydantic ValidationError, which
        # escaped as UNKNOWN/retryable -- so a silent segment was retried five
        # times under a category that says "we do not know what went wrong".
        raise EmptyTranscript("the recogniser returned only scaffolding")
    return stripped, request_id


# Chat-template scaffolding the real `mimo-v2.5-asr` model emits around the
# transcript. Measured against the live API on 2026-09-11: the same audio can
# come back as `<chinese> Good morning, everyone.`, as
# `think>\n<chinese> Today we will...` (an unbalanced `</think>` remnant), or
# with no wrapper at all. It is not stable per-request, and it cannot be turned
# off -- `thinking.type=disabled` and `chat_template_kwargs.enable_thinking`
# both still produce it (they do work for the text models). Left in place these
# tokens enter the English transcript, which is the factual source for the
# summary, so they are stripped here rather than downstream.
_ASR_OPEN_TAGS = ("<chinese>", "<english>", "<transcript>", "<think>")
_ASR_THINK_CLOSE = "think>"


def strip_asr_scaffolding(text: str) -> str:
    """Remove chat-template scaffolding from an ASR transcript.

    Conservative by construction: it only removes a known tag when that tag is
    what the remaining text *starts* with, and it never touches the interior of
    the transcript. A lecturer who says "think about the English tag" keeps
    every word, because nothing here matches mid-string content.
    """
    result = text.strip()
    # Drop a leading `</think>` / `think>` remnant plus anything ahead of it.
    head, sep, tail = result.partition(_ASR_THINK_CLOSE)
    if sep and not head.strip(" \t\n<>/"):
        result = tail.lstrip()
    changed = True
    while changed:
        changed = False
        lowered = result.lstrip().lower()
        for tag in _ASR_OPEN_TAGS:
            if lowered.startswith(tag):
                result = result.lstrip()[len(tag):].lstrip()
                changed = True
                break
    return result.strip()


def strip_interim_scaffolding(text: str) -> str:
    """Extra, riskier stripping that is only safe for interim captions.

    `strip_asr_scaffolding` deliberately leaves a bare leading "think": at the
    start of a fragment, a `</think>` remnant and a lecturer saying "I think we
    should stop here" are indistinguishable, and deleting a speaker's words to
    tidy a transcript is a permanent loss to fix a cosmetic one.

    That reasoning is about the DURABLE record. An interim caption is replaced
    within seconds, never persisted, never translated into the committed
    Chinese and never seen by the summary, so the two errors are not
    comparable: dropping a word from a throwaway line costs a moment of display,
    while showing "think whatever happens, whether you succeed" -- observed on
    real audio -- states that the speaker said "think" when they did not.

    Applied to partials only. The sealed transcript is untouched.
    """
    result = strip_asr_scaffolding(text)
    lowered = result.lower()
    for prefix in ("think ", "think,", "think."):
        if lowered.startswith(prefix):
            return result[len(prefix):].lstrip(" ,.")
    return result


def _malformed(reason: str) -> ProviderFailure:
    return ProviderFailure(
        ProviderError(
            category=ProviderErrorCategory.MALFORMED,
            retryable=True,
            safe_message=reason,
        )
    )


def classify_http_status(status: int, headers: Mapping[str, str]) -> ProviderError:
    retry_after_ms: int | None = None
    raw_retry_after = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw_retry_after = value
            break
    if raw_retry_after:
        try:
            retry_after_ms = max(0, int(float(raw_retry_after) * 1000))
        except ValueError:
            retry_after_ms = None
    if status in (401, 403):
        return ProviderError(
            category=ProviderErrorCategory.AUTH,
            retryable=False,
            http_status=status,
            safe_message="provider rejected credentials",
        )
    if status == 402:
        return ProviderError(
            category=ProviderErrorCategory.BALANCE,
            retryable=False,
            http_status=status,
            safe_message="provider account balance or entitlement error",
        )
    if status == 429:
        return ProviderError(
            category=ProviderErrorCategory.RATE_LIMIT,
            retryable=True,
            http_status=status,
            retry_after_ms=retry_after_ms,
            safe_message="provider rate limit reached",
        )
    if status in (500, 503):
        return ProviderError(
            category=ProviderErrorCategory.SERVER,
            retryable=True,
            http_status=status,
            retry_after_ms=retry_after_ms,
            safe_message="provider server error",
        )
    if 400 <= status < 500:
        return ProviderError(
            category=ProviderErrorCategory.MALFORMED,
            retryable=True,
            http_status=status,
            safe_message="provider rejected the request",
        )
    return ProviderError(
        category=ProviderErrorCategory.SERVER,
        retryable=True,
        http_status=status,
        safe_message="unexpected provider status",
    )


def _default_post(
    api_key: str, base_url: str, timeout_seconds: float
) -> HttpPost:
    """One keep-alive client per provider, not one per request.

    This opened a fresh `AsyncClient` for every call and closed it again, so
    every transcription, every translation and every interim caption paid for a
    new TCP connection and a full TLS handshake -- roughly two extra round
    trips to the endpoint before a single byte of audio was sent. Interim
    transcription multiplied the call count by about four, which multiplied the
    handshakes with it.

    The client is cached per event loop and recreated if the loop changes. It
    is not a module-level singleton because tests, and the TestClient, each run
    their own loop, and an httpx client bound to a closed loop fails on its
    next request.
    """
    state: dict = {}

    async def post(
        url: str, headers: Mapping[str, str], payload: Mapping[str, Any]
    ) -> ProviderHttpResponse:
        import asyncio

        import httpx2

        loop = asyncio.get_running_loop()
        client = state.get("client")
        if client is None or state.get("loop") is not loop or client.is_closed:
            if client is not None and state.get("loop") is loop and not client.is_closed:
                await client.aclose()
            client = httpx2.AsyncClient(
                timeout=timeout_seconds,
                limits=httpx2.Limits(
                    max_keepalive_connections=8, max_connections=16
                ),
            )
            state["client"] = client
            state["loop"] = loop
        response = await client.post(
            url, headers={**headers, "api-key": api_key}, json=dict(payload)
        )
        return ProviderHttpResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )

    return post


class MimoASRProvider:
    """Real MiMo ASR adapter. No silent fallback; a missing key is a hard
    configuration error at construction time, never a mock substitution."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        language: str = "en",
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        post: HttpPost | None = None,
        glossary_provider: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "MIMO_API_KEY is not configured; refusing to start the real provider"
            )
        self.api_key = api_key
        self.name = "mimo"
        self.model = model
        self.language = language
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._post = post or _default_post(api_key, self.base_url, timeout_seconds)
        # This assignment was missing. `transcribe_bytes` called `self._glossary()`
        # and raised AttributeError on every single call; the blanket `except
        # Exception: return ""` below turned that into an empty transcript, so
        # interim captions produced nothing, silently, always.
        self._glossary = glossary_provider or (lambda: ())

    async def transcribe_bytes(self, wav_bytes: bytes) -> str:
        """Transcribe raw WAV for interim display. Returns text, never raises.

        Separate from `transcribe` because a partial has no segment identity:
        no id, no sequence, no durable row, no TranscriptResult. Keeping it on
        a different method makes it impossible to accidentally feed interim
        text into the committed pipeline.
        """
        payload = build_asr_payload(
            wav_bytes,
            model=self.model,
            language=self.language,
            glossary=self._glossary(),
        )
        try:
            response = await asyncio.wait_for(
                self._post(
                    f"{self.base_url}/chat/completions",
                    {"Content-Type": "application/json", "api-key": self.api_key},
                    payload,
                ),
                timeout=self.timeout_seconds,
            )
            if response.status != 200:
                return ""
            text, _ = parse_asr_response(response.body)
            return strip_interim_scaffolding(text)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError, KeyError):
            # Interim display only: a network or parse failure costs a moment of
            # caption and must never disturb the durable pipeline.
            #
            # Deliberately NOT `except Exception`. The AttributeError from the
            # missing `self._glossary` was raised above this block and swallowed
            # by the partial loop's own blanket handler, so interim
            # transcription returned nothing on every call for as long as it
            # existed and no error was recorded anywhere. Narrowing here stops
            # this method adding a second place for a plain bug to disappear;
            # the loop now counts what it catches, so a dead feature shows up as
            # a fault instead of as silence.
            return ""

    async def transcribe(
        self, segment: AudioSegmentRef, *, language: str = "en"
    ) -> TranscriptResult:
        started = utc_now()
        wav_bytes = segment.audio_path.read_bytes()
        # The glossary was plumbed into this class and then never used here, so
        # D-045 -- per-course terminology, measured at 3.1% -> 1.7% word error
        # -- was switched on for nobody. The measurement was taken with a direct
        # probe rather than through this adapter, which is why it looked fine.
        payload = build_asr_payload(
            wav_bytes, model=self.model, language=language, glossary=self._glossary()
        )
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "api-key": self.api_key}
        try:
            response = await asyncio.wait_for(
                self._post(url, headers, payload), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.TIMEOUT,
                    retryable=True,
                    safe_message="provider request timed out",
                )
            ) from None
        except (ConnectionError, OSError) as exc:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.NETWORK,
                    retryable=True,
                    safe_message=f"network failure: {type(exc).__name__}",
                )
            ) from None
        except binascii.Error as exc:  # pragma: no cover - defensive
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.UNKNOWN,
                    retryable=True,
                    safe_message=f"unexpected encoding failure: {type(exc).__name__}",
                )
            ) from None
        if response.status != 200:
            raise ProviderFailure(classify_http_status(response.status, response.headers))
        text, request_id = parse_asr_response(response.body)
        finished = utc_now()
        latency_ms = max(
            0, int((finished - started).total_seconds() * 1000)
        )
        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            audio_seconds=segment.duration_ms / 1000,
            provider_request_id=request_id,
            created_at=finished,
        )


TRANSLATION_PROMPT_VERSION = 1

# Versioned instruction implementing the ARCHITECTURE.md §13 semantic contract.
TRANSLATION_SYSTEM_PROMPT = """You are a university-class realtime translator. Translate the CURRENT English segment into natural, accurate Simplified Chinese.
Rules:
1. Translate faithfully; never summarize, explain, or answer.
2. Add no information the speaker did not say; do not answer questions contained in the text.
3. Preserve finance, insurance, risk-management, actuarial, statistics, mathematics and other technical terminology precisely.
4. Do not alter numbers, formulae, symbols, abbreviations, conditions, or proper names.
5. Use the CONTEXT only to resolve terminology and references; translate only the CURRENT segment.
6. Output only the translation text: no explanations, no Markdown, no prefixes, no quotes."""

# Deterministic wrapper markers that indicate the model returned a prompt echo
# instead of a translation. Content-quality itself is covered by reviewed
# fixtures; this check only rejects structural contract violations.
_OUTPUT_WRAPPER_MARKERS = (
    "translation:",
    "translation：",
    "翻译：",
    "翻译:",
    "note:",
    "answer:",
    "[context]",
    "[current segment]",
)


def render_translation_user_content(request: TranslationRequest) -> str:
    """Structurally separate bounded context from the current segment."""
    if request.context.previous_segments:
        context_lines = ["[CONTEXT] (reference only)"]
        for segment in request.context.previous_segments:
            context_lines.append(f"- EN: {segment.english_text}")
            context_lines.append(f"  ZH: {segment.chinese_text}")
    else:
        context_lines = ["[CONTEXT] (none)"]
    return "\n".join(
        context_lines + ["[CURRENT SEGMENT]", request.current_english_text]
    )


def validate_translation_output(text: str, source_english: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.MALFORMED,
                retryable=True,
                safe_message="translation output is empty",
            )
        )
    first_line = cleaned.splitlines()[0].strip().lower()
    for marker in _OUTPUT_WRAPPER_MARKERS:
        if first_line.startswith(marker):
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.MALFORMED,
                    retryable=True,
                    safe_message="translation output contains a prompt wrapper",
                )
            )
    return cleaned


# `mimo-v2.5` is a reasoning model: by default it spends most of its budget in
# `reasoning_content` before answering. Measured against the live API on
# 2026-09-11 over five lecture sentences, disabling that cut p50 latency from
# 5.40s to 2.29s (max 8.31s -> 2.54s) and completion tokens from ~152 to ~14,
# with no loss of translation quality on any sample -- sentence translation is
# not a task that needs deliberation, and a classroom caption that lands five
# seconds late is not realtime. Two spellings exist; only this one takes effect
# (`enable_thinking` at the top level and `reasoning_effort` were both ignored).
# Summary generation deliberately does NOT use this: it is offline, and the
# reasoning budget is worth paying for there.
NO_THINKING: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}


def _default_translation_post(
    api_key: str, base_url: str, timeout_seconds: float
) -> HttpPost:
    return _default_post(api_key, base_url, timeout_seconds)


class MimoTranslationProvider:
    """Real MiMo translation adapter over the documented chat endpoint.
    Missing key is a hard configuration error, never a silent mock."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        post: HttpPost | None = None,
        glossary_provider: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "MIMO_API_KEY is not configured; refusing to start the real provider"
            )
        self.api_key = api_key
        self.name = "mimo"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._post = post or _default_post(api_key, self.base_url, timeout_seconds)
        self._glossary = glossary_provider or (lambda: ())

    async def translate_text(self, english_text: str) -> str:
        """Translate interim English for display. Returns text, never raises.

        Deliberately mirrors `transcribe_bytes`: it takes a bare string and
        returns a bare string, with no segment id, no sequence, and no
        `TranslationResult`. A partial translation therefore has nothing to
        commit and no `english_text_sha256` to bind, so it cannot be mistaken
        for -- or accidentally routed into -- the durable record the summary
        is built from.

        No context window either. Context is a durable, hashed snapshot tied to
        committed segments (D-035); feeding an uncommitted fragment into it
        would make that hash depend on text that never existed as a fact.
        """
        system_prompt = TRANSLATION_SYSTEM_PROMPT + render_glossary_hint(
            self._glossary(), for_translation=True
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": english_text},
            ],
            "stream": False,
            **NO_THINKING,
        }
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "api-key": self.api_key}
        try:
            response = await asyncio.wait_for(
                self._post(url, headers, payload), timeout=self.timeout_seconds
            )
            if response.status != 200:
                return ""
            text, _ = parse_asr_response(response.body)
            # The same interim stripping the English partial gets. It was
            # applied to `transcribe_bytes` only, so a leading `think` remnant
            # still reached the Chinese line -- where it is less ambiguous, not
            # more: a bare English "think" at the head of text that should be
            # Chinese is scaffolding, not a lecturer's word.
            return strip_interim_scaffolding(text or "")
        except Exception:
            # Display only. A failed partial must never surface as a session
            # error or disturb the committed pipeline.
            return ""

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        started = utc_now()
        # Appended to the system prompt, not the user content: the user turn
        # carries the segment being translated and its context, and mixing
        # course configuration into it would make the context snapshot hash
        # (D-035) depend on glossary edits made long after the fact.
        system_prompt = TRANSLATION_SYSTEM_PROMPT + render_glossary_hint(
            self._glossary(), for_translation=True
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": render_translation_user_content(request),
                },
            ],
            "stream": False,
            **NO_THINKING,
        }
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "api-key": self.api_key}
        try:
            response = await asyncio.wait_for(
                self._post(url, headers, payload), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.TIMEOUT,
                    retryable=True,
                    safe_message="translation request timed out",
                )
            ) from None
        except (ConnectionError, OSError) as exc:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.NETWORK,
                    retryable=True,
                    safe_message=f"network failure: {type(exc).__name__}",
                )
            ) from None
        if response.status != 200:
            raise ProviderFailure(classify_http_status(response.status, response.headers))
        text, request_id = parse_asr_response(response.body)
        cleaned = validate_translation_output(text, request.current_english_text)
        finished = utc_now()
        return TranslationResult(
            segment_id=request.segment_id,
            chinese_text=cleaned,
            english_text_sha256=hashlib.sha256(
                request.current_english_text.encode("utf-8")
            ).hexdigest(),
            provider=self.name,
            model=self.model,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
            provider_request_id=request_id,
            created_at=finished,
        )


class MimoSummaryProvider:
    """Real MiMo summary adapter (text models through the chat endpoint).
    Missing key is a hard configuration error, never a silent mock."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        post: HttpPost | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "MIMO_API_KEY is not configured; refusing to start the real provider"
            )
        self.api_key = api_key
        self.name = "mimo"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._post = post or _default_post(api_key, self.base_url, timeout_seconds)

    async def _complete(self, prompt: str, user_content: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        }
        headers = {"Content-Type": "application/json", "api-key": self.api_key}
        try:
            response = await asyncio.wait_for(
                self._post(f"{self.base_url}/chat/completions", headers, payload),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.TIMEOUT,
                    retryable=True,
                    safe_message="summary request timed out",
                )
            ) from None
        except (ConnectionError, OSError) as exc:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.NETWORK,
                    retryable=True,
                    safe_message=f"network failure: {type(exc).__name__}",
                )
            ) from None
        if response.status != 200:
            raise ProviderFailure(classify_http_status(response.status, response.headers))
        text, _ = parse_asr_response(response.body)
        return text

    async def summarize_chunk(
        self, *, chunk_index: int, transcript_material: str
    ) -> str:
        from .summary_contract import CHUNK_PROMPT, validate_chunk_notes

        return validate_chunk_notes(
            await self._complete(CHUNK_PROMPT, transcript_material)
        )

    async def synthesize(
        self,
        *,
        course_name: str,
        date: str,
        duration_text: str,
        chunk_summaries: Sequence[str],
    ) -> str:
        from .summary_contract import render_synthesis_prompt

        notes = "\n\n".join(
            f"[片段 {index + 1}]\n{notes}"
            for index, notes in enumerate(chunk_summaries)
        )
        prompt = render_synthesis_prompt(
            course_name=course_name, date=date, duration_text=duration_text
        )
        return await self._complete(prompt, notes)
