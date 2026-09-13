from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .models import SegmentState, SessionStatus


class InvalidTransition(ValueError):
    """Raised when a persisted entity attempts an unapproved state change."""


SESSION_TRANSITIONS: Mapping[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.NEW: frozenset({SessionStatus.RECORDING}),
    SessionStatus.RECORDING: frozenset(
        {SessionStatus.PAUSED, SessionStatus.FINALIZING, SessionStatus.RECOVERY_REQUIRED}
    ),
    SessionStatus.PAUSED: frozenset(
        {SessionStatus.RECORDING, SessionStatus.FINALIZING, SessionStatus.RECOVERY_REQUIRED}
    ),
    SessionStatus.FINALIZING: frozenset(
        {SessionStatus.SUMMARIZING, SessionStatus.RECOVERY_REQUIRED}
    ),
    SessionStatus.SUMMARIZING: frozenset(
        {SessionStatus.SUMMARY_SAVED, SessionStatus.RECOVERY_REQUIRED}
    ),
    SessionStatus.SUMMARY_SAVED: frozenset(
        {SessionStatus.PURGING, SessionStatus.RECOVERY_REQUIRED}
    ),
    SessionStatus.PURGING: frozenset(
        {SessionStatus.PURGED, SessionStatus.RECOVERY_REQUIRED}
    ),
    SessionStatus.RECOVERY_REQUIRED: frozenset(
        {
            SessionStatus.RECORDING,
            SessionStatus.FINALIZING,
            SessionStatus.SUMMARIZING,
            SessionStatus.PURGING,
        }
    ),
    SessionStatus.PURGED: frozenset(),
}


SEGMENT_TRANSITIONS: Mapping[SegmentState, frozenset[SegmentState]] = {
    SegmentState.CAPTURED: frozenset({SegmentState.ASR_PENDING}),
    SegmentState.ASR_PENDING: frozenset({SegmentState.ASR_PROCESSING}),
    SegmentState.ASR_PROCESSING: frozenset(
        {
            SegmentState.ASR_DONE,
            SegmentState.ASR_RETRY,
            SegmentState.ASR_FAILED,
            SegmentState.ASR_PENDING,
            SegmentState.NO_SPEECH,
        }
    ),
    SegmentState.ASR_RETRY: frozenset({SegmentState.ASR_PENDING}),
    # Terminal. A silent segment is never reprocessed: the answer will not
    # change, and every retry of it is a paid call for nothing.
    SegmentState.NO_SPEECH: frozenset(),
    SegmentState.ASR_FAILED: frozenset({SegmentState.ASR_PENDING}),
    SegmentState.ASR_DONE: frozenset({SegmentState.TRANSLATION_PENDING}),
    SegmentState.TRANSLATION_PENDING: frozenset({SegmentState.TRANSLATION_PROCESSING}),
    SegmentState.TRANSLATION_PROCESSING: frozenset(
        {
            SegmentState.DONE,
            SegmentState.TRANSLATION_RETRY,
            SegmentState.TRANSLATION_FAILED,
            SegmentState.TRANSLATION_PENDING,
        }
    ),
    SegmentState.TRANSLATION_RETRY: frozenset({SegmentState.TRANSLATION_PENDING}),
    SegmentState.TRANSLATION_FAILED: frozenset({SegmentState.TRANSLATION_PENDING}),
    SegmentState.DONE: frozenset(),
}


def _ensure_transition(current: StrEnum, target: StrEnum, table: Mapping) -> None:
    if target not in table[current]:
        raise InvalidTransition(f"Transition {current.value} -> {target.value} is not allowed")


def ensure_session_transition(current: SessionStatus, target: SessionStatus) -> None:
    _ensure_transition(current, target, SESSION_TRANSITIONS)


def ensure_segment_transition(current: SegmentState, target: SegmentState) -> None:
    _ensure_transition(current, target, SEGMENT_TRANSITIONS)

