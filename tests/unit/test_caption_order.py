"""English captions must appear in the order they were spoken.

Found by running a live hour of lecture audio and comparing the order captions
arrived in against the order the sentences were played: 3 of the first 14
`subtitle.current` events were inverted, while every `subtitle.committed` event
was in order. The translation stream passes through a durable ordered cursor;
the English-first stream added by D-046 was published straight from whichever
of the two ASR workers finished first.
"""

from __future__ import annotations

import pytest

from classroom_interpreter.runtime.caption_order import CaptionOrderGate


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_gate(**kwargs):
    seen: list[int] = []
    clock = Clock()
    gate = CaptionOrderGate(
        lambda payload: seen.append(payload["sequence"]), _clock=clock, **kwargs
    )
    return gate, seen, clock


def test_in_order_captions_pass_straight_through():
    """Ordering must cost nothing in the common case."""
    gate, seen, _ = make_gate()
    for sequence in range(5):
        gate.offer(sequence, {"sequence": sequence})
        assert seen[-1] == sequence, "an in-order caption was delayed"
    assert seen == [0, 1, 2, 3, 4]


def test_an_overtaking_caption_waits_for_its_predecessor():
    """The exact live failure: segment 1 finished ASR before segment 0.

    Mutation that turns this red: emit directly in `offer` instead of buffering.
    """
    gate, seen, _ = make_gate()
    gate.offer(1, {"sequence": 1})
    assert seen == [], "caption 1 was shown before caption 0 had arrived"
    gate.offer(0, {"sequence": 0})
    assert seen == [0, 1]


def test_a_long_run_is_released_in_order():
    gate, seen, _ = make_gate()
    for sequence in [2, 0, 1, 5, 3, 4, 6]:
        gate.offer(sequence, {"sequence": sequence})
    assert seen == [0, 1, 2, 3, 4, 5, 6]


def test_a_gap_that_never_closes_does_not_freeze_the_screen():
    """The reason this is not a strict gate.

    If segment 3 fails ASR permanently, a strict ordering gate holds 4, 5, 6
    and every caption after them for the rest of the class. A frozen display is
    worse than one inversion.

    Mutation that turns this red: make `tick` a no-op.
    """
    gate, seen, clock = make_gate(stall_seconds=3.0)
    gate.offer(0, {"sequence": 0})
    gate.offer(1, {"sequence": 1})
    gate.offer(2, {"sequence": 2})
    assert seen == [0, 1, 2]

    gate.offer(4, {"sequence": 4})   # 3 never arrives
    assert seen == [0, 1, 2]

    clock.now += 1.0
    gate.tick()
    assert seen == [0, 1, 2], "gave up on the gap far too early"

    clock.now += 2.5
    gate.tick()
    assert seen == [0, 1, 2, 4], "a permanently missing segment froze the display"

    gate.offer(5, {"sequence": 5})
    assert seen == [0, 1, 2, 4, 5], "the gate did not resume after giving up"


def test_tick_is_harmless_when_nothing_is_held():
    gate, seen, clock = make_gate()
    for _ in range(5):
        clock.now += 10
        gate.tick()
    assert seen == []


def test_a_burst_of_gaps_is_bounded():
    """The buffer must not grow without limit while a gap stays open.

    Mutation that turns this red: remove the `max_buffered` check.
    """
    gate, seen, _ = make_gate(max_buffered=4, stall_seconds=1000)
    for sequence in range(1, 8):    # 0 never arrives
        gate.offer(sequence, {"sequence": sequence})
    assert seen, "the buffer grew unboundedly instead of releasing"
    assert seen == sorted(seen)


def test_a_caption_already_past_is_still_shown_not_dropped():
    """A duplicate is visible and harmless; a dropped caption is neither."""
    gate, seen, _ = make_gate()
    gate.offer(0, {"sequence": 0})
    gate.offer(1, {"sequence": 1})
    gate.offer(0, {"sequence": 0})
    assert seen == [0, 1, 0]


def test_flush_releases_everything_in_order():
    """Finalization stops the tick, so anything held would be stranded.

    Mutation that turns this red: drop the `flush()` call in
    `stop_workers_for_finalization`.
    """
    gate, seen, _ = make_gate()
    gate.offer(3, {"sequence": 3})
    gate.offer(2, {"sequence": 2})
    assert seen == []
    gate.flush()
    assert seen == [2, 3]
