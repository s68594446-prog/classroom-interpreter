from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
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
from .errors import ProviderFailure

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


def build_asr_payload(
    wav_bytes: bytes, *, model: str, language: str
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
    return {
        "model": model,
        "messages": [
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
        ],
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
    if not isinstance(text, str) or not text.strip():
        raise _malformed("response content is empty or non-text")
    return text.strip(), request_id


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
    async def post(
        url: str, headers: Mapping[str, str], payload: Mapping[str, Any]
    ) -> ProviderHttpResponse:
        import httpx2

        async with httpx2.AsyncClient(timeout=timeout_seconds) as client:
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

    async def transcribe(
        self, segment: AudioSegmentRef, *, language: str = "en"
    ) -> TranscriptResult:
        started = utc_now()
        wav_bytes = segment.audio_path.read_bytes()
        payload = build_asr_payload(wav_bytes, model=self.model, language=language)
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

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        started = utc_now()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": render_translation_user_content(request),
                },
            ],
            "stream": False,
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
