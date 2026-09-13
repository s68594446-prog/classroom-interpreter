from __future__ import annotations

from classroom_interpreter.domain.models import ProviderError


class ProviderFailure(RuntimeError):
    """Raised by provider adapters when a request cannot produce a valid result.

    Carries a classified, content-free ProviderError. Ordinary logs may record
    the category/status/retry hint but never request or response classroom
    content.
    """

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.safe_message or error.category.value)
        self.error = error


class EmptyTranscript(RuntimeError):
    """The recogniser returned no text for this audio.

    Deliberately not a `ProviderFailure`. Empty is an *answer*, and a
    deterministic one: the same audio will come back empty every time, so every
    retry is a paid call that cannot succeed. Treated as a failure it cost five
    attempts per segment and then ASR_FAILED, which stops the whole class from
    being summarised (D-006) -- for two seconds of silence.
    """
