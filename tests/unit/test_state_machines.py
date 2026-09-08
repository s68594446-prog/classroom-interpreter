import pytest

from classroom_interpreter.domain.models import SegmentState, SessionStatus
from classroom_interpreter.domain.state_machines import (
    SEGMENT_TRANSITIONS,
    SESSION_TRANSITIONS,
    InvalidTransition,
    ensure_segment_transition,
    ensure_session_transition,
)


def test_every_session_state_is_declared() -> None:
    assert set(SESSION_TRANSITIONS) == set(SessionStatus)


def test_session_happy_path_is_allowed() -> None:
    path = [
        SessionStatus.NEW,
        SessionStatus.RECORDING,
        SessionStatus.FINALIZING,
        SessionStatus.SUMMARIZING,
        SessionStatus.SUMMARY_SAVED,
        SessionStatus.PURGING,
        SessionStatus.PURGED,
    ]
    for current, target in zip(path[:-1], path[1:], strict=True):
        ensure_session_transition(current, target)


def test_purge_cannot_start_before_summary_saved() -> None:
    with pytest.raises(InvalidTransition):
        ensure_session_transition(SessionStatus.SUMMARIZING, SessionStatus.PURGING)


def test_purged_is_terminal() -> None:
    with pytest.raises(InvalidTransition):
        ensure_session_transition(SessionStatus.PURGED, SessionStatus.RECORDING)


def test_every_segment_state_is_declared() -> None:
    assert set(SEGMENT_TRANSITIONS) == set(SegmentState)


def test_segment_happy_path_is_allowed() -> None:
    path = [
        SegmentState.CAPTURED,
        SegmentState.ASR_PENDING,
        SegmentState.ASR_PROCESSING,
        SegmentState.ASR_DONE,
        SegmentState.TRANSLATION_PENDING,
        SegmentState.TRANSLATION_PROCESSING,
        SegmentState.DONE,
    ]
    for current, target in zip(path[:-1], path[1:], strict=True):
        ensure_segment_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SegmentState.ASR_PROCESSING, SegmentState.ASR_PENDING),
        (SegmentState.TRANSLATION_PROCESSING, SegmentState.TRANSLATION_PENDING),
    ],
)
def test_expired_leases_can_return_to_pending(
    current: SegmentState, target: SegmentState
) -> None:
    ensure_segment_transition(current, target)


def test_segment_cannot_skip_asr() -> None:
    with pytest.raises(InvalidTransition):
        ensure_segment_transition(
            SegmentState.CAPTURED, SegmentState.TRANSLATION_PENDING
        )
