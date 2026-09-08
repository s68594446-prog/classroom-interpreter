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
