"""Release English captions in the order they were spoken.

`subtitle.committed` has always been ordered: it passes through the durable
translation cursor, which releases one segment at a time. `subtitle.current`
-- the English-first caption added by D-046 -- did not. It was published
directly by whichever ASR worker finished first, and with two workers running
concurrently a short segment routinely overtakes the longer one before it.

Measured on a live hour of lecture audio: 3 of the first 14 captions arrived
out of order. On screen that means the reader sees the next sentence, then the
one before it. The durable record is unaffected -- segments carry their own
sequence -- but `websocket_events` is the replay log, so a reconnecting client
was handed the same inversion again.

Strict ordering alone would be wrong. If one segment never produces a caption
at all -- a permanent ASR failure, a discarded segment -- a strict gate holds
every later caption forever, and a frozen screen is worse than a brief
inversion. So the gate gives up on a gap after `stall_seconds` and releases
what it is holding, in order.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CaptionOrderGate:
    """Buffers out-of-order captions briefly, then releases them in sequence."""

    emit: Callable[[dict], None]
    stall_seconds: float = 3.0
    max_buffered: int = 16
    _next: int = 0
    _pending: dict[int, dict] = field(default_factory=dict, repr=False)
    _waiting_since: float | None = field(default=None, repr=False)
    _clock: Callable[[], float] = time.monotonic

    def offer(self, sequence: int, payload: dict) -> None:
        """Accept one caption; emit whatever is now releasable."""
        if sequence < self._next:
            # Already past it. Emitting is the lesser evil: a duplicate is
            # visible and harmless, a dropped caption is neither.
            self.emit(payload)
            return
        self._pending[sequence] = payload
        self._drain()
        if self._pending and self._waiting_since is None:
            self._waiting_since = self._clock()
        if len(self._pending) > self.max_buffered:
            # The gap is not going to close. Release rather than grow.
            self._give_up_on_the_gap()

    def tick(self) -> None:
        """Called periodically so a stalled gap cannot freeze the display.

        Without this, a gap only ever resolves when the *next* caption arrives
        -- so if the missing segment is the last thing that fails, whatever is
        buffered behind it is never shown at all.
        """
        if not self._pending:
            return
        if self._waiting_since is None:
            self._waiting_since = self._clock()
            return
        if self._clock() - self._waiting_since >= self.stall_seconds:
            self._give_up_on_the_gap()

    def flush(self) -> None:
        """Release everything held, in order. For shutdown and finalization."""
        for sequence in sorted(self._pending):
            self.emit(self._pending.pop(sequence))
            self._next = max(self._next, sequence + 1)
        self._waiting_since = None

    # ------------------------------------------------------------------ internals

    def _drain(self) -> None:
        while self._next in self._pending:
            self.emit(self._pending.pop(self._next))
            self._next += 1
        self._waiting_since = self._clock() if self._pending else None

    def _give_up_on_the_gap(self) -> None:
        if not self._pending:
            return
        lowest = min(self._pending)
        self._next = lowest
        self._drain()
