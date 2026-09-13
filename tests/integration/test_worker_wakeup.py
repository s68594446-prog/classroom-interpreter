"""Workers slept out a poll timer with work already waiting.

Durable rows are the only queue (D-025), and the ASR and translation loops
polled that queue every 0.3s. So a segment sealed a moment after a worker went
to sleep waited out the rest of the interval before anyone looked at it -- and
the ASR leg and the translation leg each paid it separately, in front of every
caption.

The nudge is not a queue and nothing depends on it for correctness: if it is
ever missed, the poll still finds the work. It only removes the waiting.
"""

from __future__ import annotations

import asyncio
import time

from classroom_interpreter.runtime.pipeline import SessionOrchestrator
from classroom_interpreter.storage import Database

from test_live_api import FakeASR, FakeTranslation, make_settings


async def _bounded(coro, seconds: float = 3.0):
    """Every `_idle` in this module runs under a deadline.

    An `_idle` that never returns is one of the failures under test -- drop the
    timeout from its `asyncio.wait` and it blocks forever. Without a deadline
    that hangs the suite instead of failing it, which hides the bug.
    """
    await asyncio.wait_for(coro, timeout=seconds)


def make_runtime(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    database.migrate()
    return SessionOrchestrator(
        make_settings(tmp_path),
        database,
        asr_provider=FakeASR(),
        translation_provider=FakeTranslation(),
    )


class _Segmenter:
    """Seals one segment on its first call, nothing afterwards."""

    def __init__(self):
        self.calls = 0

    def process_available_chunks(self):
        self.calls += 1
        return ("one sealed segment",) if self.calls == 1 else ()

    def seal_tail(self):
        return None

    def idle_silence_ms(self):
        return 0


def test_sealing_a_segment_wakes_the_workers(tmp_path):
    """Mutation that turns this red: drop the `self._work_ready.set()` from
    the segmenter loop."""
    runtime = make_runtime(tmp_path)
    runtime.active_session = None  # keeps auto-pause out of this test
    runtime._segmenter = _Segmenter()

    async def exercise():
        runtime._stop.clear()
        runtime._work_ready.clear()
        task = asyncio.create_task(runtime._segmenter_loop())
        await asyncio.sleep(0.15)
        runtime._stop.set()
        await task
        return runtime._work_ready.is_set()

    assert asyncio.run(exercise()), (
        "a sealed segment did not wake anything; the next worker to look at it "
        "would have slept out its poll interval first"
    )


def test_a_woken_worker_does_not_wait_for_the_timer(tmp_path):
    """Mutation that turns this red: make `_idle` wait on `self._stop` alone,
    as it did before."""
    runtime = make_runtime(tmp_path)

    async def exercise():
        runtime._stop.clear()
        runtime._work_ready.set()
        started = time.monotonic()
        await _bounded(runtime._idle(5.0))
        return time.monotonic() - started

    elapsed = asyncio.run(exercise())
    assert elapsed < 1.0, f"waited {elapsed:.2f}s with work already available"


def test_the_poll_interval_is_still_the_backstop(tmp_path):
    """The nudge is an optimisation. A missed one must cost latency, never
    work: the timer still has to fire on its own.

    Mutation that turns this red: wait on `_work_ready` with no timeout.
    """
    runtime = make_runtime(tmp_path)

    async def exercise():
        runtime._stop.clear()
        runtime._work_ready.clear()
        started = time.monotonic()
        # A deadline, not a bare await: an `_idle` that never returns is the
        # very failure under test, and it would hang the suite instead of
        # failing it.
        await _bounded(runtime._idle(0.2))
        return time.monotonic() - started

    elapsed = asyncio.run(exercise())
    assert 0.15 <= elapsed < 2.0, f"the poll interval did not fire ({elapsed:.2f}s)"


def test_the_nudge_is_consumed_so_it_cannot_spin(tmp_path):
    """A latched event would turn every idle loop into a busy loop.

    Mutation that turns this red: remove the `self._work_ready.clear()` at the
    end of `_idle`.
    """
    runtime = make_runtime(tmp_path)

    async def exercise():
        runtime._stop.clear()
        runtime._work_ready.set()
        await _bounded(runtime._idle(5.0))
        started = time.monotonic()
        await _bounded(runtime._idle(0.2))
        return time.monotonic() - started

    elapsed = asyncio.run(exercise())
    assert elapsed >= 0.15, (
        f"the second idle returned in {elapsed:.2f}s: the nudge stayed set and "
        "the worker would spin on the database"
    )
