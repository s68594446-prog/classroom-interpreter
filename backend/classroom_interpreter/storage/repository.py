from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from classroom_interpreter.domain.models import (
    ContextWindow,
    SegmentRecord,
    SegmentState,
    SessionRecord,
    SessionStatus,
    TranscriptResult,
    TranslationResult,
)
from classroom_interpreter.domain.state_machines import (
    ensure_segment_transition,
    ensure_session_transition,
)

from .database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdempotencyConflict(RuntimeError):
    pass


class OrderingConflict(RuntimeError):
    pass


class SourceHashMismatch(RuntimeError):
    pass


class CommitOutcome(StrEnum):
    CREATED = "CREATED"
    ALREADY_COMMITTED = "ALREADY_COMMITTED"


@dataclass(slots=True)
class SessionRepository:
    database: Database

    def create_session(
        self, course_name: str, *, app_version: str, session_id: str | None = None
    ) -> SessionRecord:
        clean_name = course_name.strip()
        if not clean_name:
            raise ValueError("course_name must not be empty")
        session_id = session_id or str(uuid.uuid4())
        now = _now_iso()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    id, course_name, status, created_at, app_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    clean_name,
                    SessionStatus.NEW.value,
                    now,
                    app_version,
                    now,
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _session_from_row(row)

    def transition_session(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        reason_code: str,
        actor: str,
    ) -> SessionRecord:
        now = _now_iso()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            current = SessionStatus(row["status"])
            ensure_session_transition(current, target)
            started_at = row["started_at"]
            ended_at = row["ended_at"]
            duration_ms = row["duration_ms"]
            if target is SessionStatus.RECORDING and started_at is None:
                started_at = now
            if target is SessionStatus.FINALIZING and ended_at is None:
                ended_at = now
                if started_at:
                    start = datetime.fromisoformat(started_at)
                    end = datetime.fromisoformat(ended_at)
                    duration_ms = max(0, int((end - start).total_seconds() * 1000))
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, started_at = ?, ended_at = ?, duration_ms = ?,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (target.value, started_at, ended_at, duration_ms, now, session_id),
            )
            connection.execute(
                """
                INSERT INTO state_transition_audits(
                    entity_type, entity_id, from_state, to_state,
                    reason_code, actor, created_at
                ) VALUES ('session', ?, ?, ?, ?, ?, ?)
                """,
                (session_id, current.value, target.value, reason_code, actor, now),
            )
        return self.get_session(session_id)

    def commit_summary_saved(
        self,
        session_id: str,
        *,
        summary_path: Path,
        summary_hash: str,
        summary_model: str,
    ) -> SessionRecord:
        """Atomically bind verified summary metadata to the purge-gate state.

        The summary file is fsynced and read-back verified by SummaryAgent before
        this method is called. Metadata, state and audit share one transaction,
        so an exception at any DB boundary cannot expose SUMMARY_SAVED without
        the exact path/hash needed for independent purge validation.
        """
        now = _now_iso()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            current = SessionStatus(row["status"])
            if current is SessionStatus.SUMMARY_SAVED:
                if (
                    row["summary_path"] == str(summary_path)
                    and row["summary_hash"] == summary_hash
                    and row["summary_model"] == summary_model
                ):
                    return self.get_session(session_id)
                raise IdempotencyConflict(
                    "SUMMARY_SAVED metadata differs from verified summary"
                )
            ensure_session_transition(current, SessionStatus.SUMMARY_SAVED)
            connection.execute(
                """
                UPDATE sessions
                SET status = 'SUMMARY_SAVED', summary_path = ?, summary_hash = ?,
                    summary_model = ?, summary_saved_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    str(summary_path),
                    summary_hash,
                    summary_model,
                    now,
                    now,
                    session_id,
                ),
            )
            _insert_transition_audit(
                connection,
                entity_type="session",
                entity_id=session_id,
                from_state=current.value,
                to_state=SessionStatus.SUMMARY_SAVED.value,
                reason_code="SUMMARY_SAVED",
                actor="summary_agent",
                created_at=now,
            )
        return self.get_session(session_id)

    def create_segment(
        self,
        *,
        session_id: str,
        sequence: int,
        start_ms: int,
        end_ms: int,
        overlap_ms: int,
        audio_path: Path,
        audio_sha256: str,
        segment_id: str | None = None,
    ) -> SegmentRecord:
        if sequence < 0 or start_ms < 0 or end_ms <= start_ms:
            raise ValueError("invalid segment timing or sequence")
        duration_ms = end_ms - start_ms
        segment_id = segment_id or str(uuid.uuid4())
        now = _now_iso()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM segments WHERE session_id = ? AND sequence = ?",
                (session_id, sequence),
            ).fetchone()
            if existing is not None:
                same_source = (
                    existing["audio_sha256"] == audio_sha256
                    and existing["start_ms"] == start_ms
                    and existing["end_ms"] == end_ms
                )
                if not same_source:
                    raise IdempotencyConflict(
                        f"segment key {session_id}:{sequence} already has different source"
                    )
                return _segment_from_row(existing)
            connection.execute(
                """
                INSERT INTO segments(
                    id, session_id, sequence, start_ms, end_ms, duration_ms,
                    overlap_ms, audio_path, audio_sha256, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    session_id,
                    sequence,
                    start_ms,
                    end_ms,
                    duration_ms,
                    overlap_ms,
                    str(audio_path),
                    audio_sha256,
                    SegmentState.CAPTURED.value,
                    now,
                    now,
                ),
            )
        return self.get_segment(session_id, sequence)

    def get_segment(self, session_id: str, sequence: int) -> SegmentRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE session_id = ? AND sequence = ?",
                (session_id, sequence),
            ).fetchone()
        if row is None:
            raise KeyError(f"{session_id}:{sequence}")
        return _segment_from_row(row)

    def transition_segment(
        self,
        session_id: str,
        sequence: int,
        target: SegmentState,
        *,
        reason_code: str,
        actor: str,
    ) -> SegmentRecord:
        now = _now_iso()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE session_id = ? AND sequence = ?",
                (session_id, sequence),
            ).fetchone()
            if row is None:
                raise KeyError(f"{session_id}:{sequence}")
            current = SegmentState(row["state"])
            ensure_segment_transition(current, target)
            connection.execute(
                "UPDATE segments SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, now, row["id"]),
            )
            connection.execute(
                """
                INSERT INTO state_transition_audits(
                    entity_type, entity_id, from_state, to_state,
                    reason_code, actor, created_at
                ) VALUES ('segment', ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], current.value, target.value, reason_code, actor, now),
            )
        return self.get_segment(session_id, sequence)

    def promote_captured_segments(self) -> int:
        """Move all CAPTURED segments to ASR_PENDING (audited). Returns how
        many rows were promoted."""
        now = _now_iso()
        promoted = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id, state FROM segments WHERE state = ?",
                (SegmentState.CAPTURED.value,),
            ).fetchall()
            for row in rows:
                ensure_segment_transition(
                    SegmentState(row["state"]), SegmentState.ASR_PENDING
                )
                connection.execute(
                    "UPDATE segments SET state = ?, updated_at = ? WHERE id = ?",
                    (SegmentState.ASR_PENDING.value, now, row["id"]),
                )
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=row["state"],
                    to_state=SegmentState.ASR_PENDING.value,
                    reason_code="ASR_SCHEDULER_PROMOTED",
                    actor="transcription_agent",
                    created_at=now,
                )
                promoted += 1
        return promoted

    def claim_next_asr_segment(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
        max_attempts: int,
    ) -> SegmentRecord | None:
        """CAS-claim the next ASR-eligible segment across all sessions.

        Handles pending work, retry/backoff rows whose delay elapsed, and
        expired leases. Rows past the retry budget transition to ASR_FAILED
        instead of being claimed again. Provider calls never run inside this
        transaction.
        """
        now = _now_iso()
        lease_expires = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM segments
                WHERE (state = 'ASR_PENDING'
                           AND (asr_next_attempt_at IS NULL
                                OR asr_next_attempt_at <= :now))
                   OR (state = 'ASR_RETRY'
                           AND asr_next_attempt_at IS NOT NULL
                           AND asr_next_attempt_at <= :now)
                   OR (state = 'ASR_PROCESSING'
                           AND lease_expires_at IS NOT NULL
                           AND lease_expires_at <= :now)
                ORDER BY session_id, sequence
                LIMIT 1
                """,
                {"now": now},
            ).fetchone()
            if row is None:
                return None
            current = SegmentState(row["state"])
            if current is SegmentState.ASR_PENDING:
                ensure_segment_transition(current, SegmentState.ASR_PROCESSING)
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=current.value,
                    to_state=SegmentState.ASR_PROCESSING.value,
                    reason_code="ASR_LEASE_ACQUIRED",
                    actor=worker_id,
                    created_at=now,
                )
            else:
                if current is SegmentState.ASR_RETRY:
                    ensure_segment_transition(current, SegmentState.ASR_PENDING)
                    _insert_transition_audit(
                        connection,
                        entity_type="segment",
                        entity_id=row["id"],
                        from_state=current.value,
                        to_state=SegmentState.ASR_PENDING.value,
                        reason_code="ASR_BACKOFF_ELAPSED",
                        actor=worker_id,
                        created_at=now,
                    )
                    current = SegmentState.ASR_PENDING
                elif current is SegmentState.ASR_PROCESSING:
                    ensure_segment_transition(current, SegmentState.ASR_PENDING)
                    _insert_transition_audit(
                        connection,
                        entity_type="segment",
                        entity_id=row["id"],
                        from_state=current.value,
                        to_state=SegmentState.ASR_PENDING.value,
                        reason_code="ASR_LEASE_EXPIRED",
                        actor=worker_id,
                        created_at=now,
                    )
                    current = SegmentState.ASR_PENDING
                ensure_segment_transition(current, SegmentState.ASR_PROCESSING)
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=current.value,
                    to_state=SegmentState.ASR_PROCESSING.value,
                    reason_code="ASR_LEASE_REACQUIRED",
                    actor=worker_id,
                    created_at=now,
                )
            attempt_number = row["asr_retry_count"] + 1
            if attempt_number > max_attempts:
                self._exhaust_segment_in_transaction(
                    connection,
                    row,
                    current=current,
                    actor=worker_id,
                    now=now,
                )
                return None
            connection.execute(
                """
                UPDATE segments
                SET state = 'ASR_PROCESSING', lease_owner = ?,
                    lease_expires_at = ?, asr_retry_count = ?,
                    asr_next_attempt_at = NULL, updated_at = ?
                WHERE id = ? AND state IN ('ASR_PENDING', 'ASR_RETRY', 'ASR_PROCESSING')
                """,
                (worker_id, lease_expires, attempt_number, now, row["id"]),
            )
            connection.execute(
                """
                INSERT INTO processing_attempts(
                    id, session_id, segment_id, stage, attempt_number, started_at
                ) VALUES (?, ?, ?, 'ASR', ?, ?)
                """,
                (str(uuid.uuid4()), row["session_id"], row["id"], attempt_number, now),
            )
            return self.get_segment(row["session_id"], row["sequence"])

    def finish_asr_attempt(
        self,
        segment_id: str,
        *,
        attempt_number: int,
        outcome: str,
        latency_ms: int | None = None,
        error_category: str | None = None,
        http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE processing_attempts
                SET finished_at = ?, outcome = ?, latency_ms = ?,
                    error_category = ?, http_status = ?, provider_request_id = ?
                WHERE segment_id = ? AND stage = 'ASR' AND attempt_number = ?
                """,
                (
                    _now_iso(),
                    outcome,
                    latency_ms,
                    error_category,
                    http_status,
                    provider_request_id,
                    segment_id,
                    attempt_number,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(f"attempt row missing for {segment_id} #{attempt_number}")

    def mark_asr_retry(
        self,
        session_id: str,
        sequence: int,
        *,
        category: str,
        safe_message: str,
        next_attempt_at: str,
        http_status: int | None = None,
    ) -> SegmentRecord:
        now = _now_iso()
        with self.database.transaction() as connection:
            row = _require_segment(connection, session_id, sequence)
            current = SegmentState(row["state"])
            ensure_segment_transition(current, SegmentState.ASR_RETRY)
            connection.execute(
                """
                UPDATE segments
                SET state = 'ASR_RETRY', lease_owner = NULL, lease_expires_at = NULL,
                    asr_next_attempt_at = ?, last_error_category = ?,
                    last_error_safe_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_attempt_at, category, safe_message, now, row["id"]),
            )
            if http_status is not None:
                connection.execute(
                    """
                    UPDATE processing_attempts SET http_status = ?
                    WHERE segment_id = ? AND stage = 'ASR'
                      AND attempt_number = (SELECT MAX(attempt_number)
                                            FROM processing_attempts
                                            WHERE segment_id = ? AND stage = 'ASR')
                    """,
                    (http_status, row["id"], row["id"]),
                )
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=current.value,
                to_state=SegmentState.ASR_RETRY.value,
                reason_code=f"ASR_RETRY_{category}",
                actor="transcription_agent",
                created_at=now,
            )
        return self.get_segment(session_id, sequence)

    def mark_asr_failed(
        self,
        session_id: str,
        sequence: int,
        *,
        category: str,
        safe_message: str,
        http_status: int | None = None,
    ) -> SegmentRecord:
        now = _now_iso()
        with self.database.transaction() as connection:
            row = _require_segment(connection, session_id, sequence)
            current = SegmentState(row["state"])
            ensure_segment_transition(current, SegmentState.ASR_FAILED)
            self._fail_segment_in_transaction(
                connection,
                row,
                category=category,
                message=safe_message,
                reason_code=f"ASR_FAILED_{category}",
                actor="transcription_agent",
                now=now,
            )
            if http_status is not None:
                connection.execute(
                    """
                    UPDATE processing_attempts SET http_status = ?
                    WHERE segment_id = ? AND stage = 'ASR'
                      AND attempt_number = (SELECT MAX(attempt_number)
                                            FROM processing_attempts
                                            WHERE segment_id = ? AND stage = 'ASR')
                    """,
                    (http_status, row["id"], row["id"]),
                )
        return self.get_segment(session_id, sequence)

    def _exhaust_segment_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        current: SegmentState,
        actor: str,
        now: str,
    ) -> None:
        """Retire an over-budget row through state-machine-legal steps only.

        Whatever the claimable state was, it is walked to ASR_PROCESSING via
        allowed transitions and then to ASR_FAILED; each step is audited.
        """
        state = current
        if state is SegmentState.ASR_RETRY:
            ensure_segment_transition(state, SegmentState.ASR_PENDING)
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=state.value,
                to_state=SegmentState.ASR_PENDING.value,
                reason_code="ASR_BACKOFF_ELAPSED",
                actor=actor,
                created_at=now,
            )
            state = SegmentState.ASR_PENDING
        if state is SegmentState.ASR_PENDING:
            ensure_segment_transition(state, SegmentState.ASR_PROCESSING)
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=state.value,
                to_state=SegmentState.ASR_PROCESSING.value,
                reason_code="ASR_LEASE_ACQUIRED",
                actor=actor,
                created_at=now,
            )
            state = SegmentState.ASR_PROCESSING
        self._fail_segment_in_transaction(
            connection,
            row,
            category="RETRY_BUDGET_EXHAUSTED",
            message="ASR retry budget exhausted",
            reason_code="ASR_RETRIES_EXHAUSTED",
            actor=actor,
            now=now,
        )

    def _fail_segment_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        category: str,
        message: str,
        reason_code: str,
        actor: str,
        now: str,
    ) -> None:
        current = SegmentState(row["state"])
        ensure_segment_transition(current, SegmentState.ASR_FAILED)
        connection.execute(
            """
            UPDATE segments
            SET state = 'ASR_FAILED', lease_owner = NULL, lease_expires_at = NULL,
                last_error_category = ?, last_error_safe_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (category, message, now, row["id"]),
        )
        _insert_transition_audit(
            connection,
            entity_type="segment",
            entity_id=row["id"],
            from_state=current.value,
            to_state=SegmentState.ASR_FAILED.value,
            reason_code=reason_code,
            actor=actor,
            created_at=now,
        )
        connection.execute(
            """
            UPDATE sessions
            SET last_error_category = ?, last_error_safe_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (category, message, now, row["session_id"]),
        )

    def update_session_error(
        self, session_id: str, *, category: str, safe_message: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET last_error_category = ?, last_error_safe_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (category, safe_message, _now_iso(), session_id),
            )

    def commit_asr_result(
        self, session_id: str, sequence: int, result: TranscriptResult
    ) -> CommitOutcome:
        now = _now_iso()
        with self.database.transaction() as connection:
            segment = _require_segment(connection, session_id, sequence)
            if result.segment_id != segment["id"]:
                raise IdempotencyConflict("ASR result segment_id does not match durable key")
            existing = connection.execute(
                "SELECT * FROM asr_results WHERE segment_id = ?", (segment["id"],)
            ).fetchone()
            if existing is not None:
                if (
                    existing["english_text"] == result.english_text
                    and existing["provider"] == result.provider
                    and existing["model"] == result.model
                ):
                    return CommitOutcome.ALREADY_COMMITTED
                raise IdempotencyConflict("ASR result already committed with different content")
            current = SegmentState(segment["state"])
            ensure_segment_transition(current, SegmentState.ASR_DONE)
            connection.execute(
                """
                INSERT INTO asr_results(
                    segment_id, session_id, segment_sequence, english_text,
                    provider, model, provider_request_id, latency_ms,
                    audio_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment["id"],
                    session_id,
                    sequence,
                    result.english_text,
                    result.provider,
                    result.model,
                    result.provider_request_id,
                    result.latency_ms,
                    result.audio_seconds,
                    result.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE segments SET state = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ? WHERE id = ?
                """,
                (SegmentState.ASR_DONE.value, now, segment["id"]),
            )
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=segment["id"],
                from_state=current.value,
                to_state=SegmentState.ASR_DONE.value,
                reason_code="ASR_RESULT_COMMITTED",
                actor="transcription_agent",
                created_at=now,
            )
        return CommitOutcome.CREATED

    def claim_next_translation_segment(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
        max_attempts: int,
    ) -> SegmentRecord | None:
        """CAS-claim any translation-eligible segment.

        Provider work is deliberately not head-of-line ordered. Durable staged
        results are released later by the per-session sequencer (D-034).
        """
        now = _now_iso()
        lease_expires = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM segments
                WHERE (state = 'TRANSLATION_RETRY'
                           AND translation_next_attempt_at IS NOT NULL
                           AND translation_next_attempt_at <= :now)
                   OR (state = 'TRANSLATION_PROCESSING'
                           AND lease_expires_at IS NOT NULL
                           AND lease_expires_at <= :now)
                ORDER BY session_id, sequence
                LIMIT 1
                """,
                {"now": now},
            ).fetchone()
            reclaimed = row is not None
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM segments
                    WHERE state = 'ASR_DONE'
                    ORDER BY session_id, sequence
                    LIMIT 1
                    """,
                ).fetchone()
            if row is None:
                return None
            current = SegmentState(row["state"])
            if current is SegmentState.ASR_DONE:
                ensure_segment_transition(current, SegmentState.TRANSLATION_PENDING)
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=current.value,
                    to_state=SegmentState.TRANSLATION_PENDING.value,
                    reason_code="TRANSLATION_PROMOTED",
                    actor=worker_id,
                    created_at=now,
                )
                current = SegmentState.TRANSLATION_PENDING
            elif current is SegmentState.TRANSLATION_RETRY:
                ensure_segment_transition(current, SegmentState.TRANSLATION_PENDING)
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=current.value,
                    to_state=SegmentState.TRANSLATION_PENDING.value,
                    reason_code="TRANSLATION_BACKOFF_ELAPSED",
                    actor=worker_id,
                    created_at=now,
                )
                current = SegmentState.TRANSLATION_PENDING
            elif current is SegmentState.TRANSLATION_PROCESSING and not reclaimed:
                pass  # fallthrough handled below (should not occur)
            elif current is SegmentState.TRANSLATION_PROCESSING:
                ensure_segment_transition(current, SegmentState.TRANSLATION_PENDING)
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=current.value,
                    to_state=SegmentState.TRANSLATION_PENDING.value,
                    reason_code="TRANSLATION_LEASE_EXPIRED",
                    actor=worker_id,
                    created_at=now,
                )
                current = SegmentState.TRANSLATION_PENDING
            if current is SegmentState.TRANSLATION_PENDING:
                ensure_segment_transition(current, SegmentState.TRANSLATION_PROCESSING)
                _insert_transition_audit(
                    connection,
                    entity_type="segment",
                    entity_id=row["id"],
                    from_state=current.value,
                    to_state=SegmentState.TRANSLATION_PROCESSING.value,
                    reason_code="TRANSLATION_LEASE_ACQUIRED",
                    actor=worker_id,
                    created_at=now,
                )
            attempt_number = row["translation_retry_count"] + 1
            if attempt_number > max_attempts:
                self._fail_translation_in_transaction(
                    connection,
                    row,
                    category="RETRY_BUDGET_EXHAUSTED",
                    message="translation retry budget exhausted",
                    reason_code="TRANSLATION_RETRIES_EXHAUSTED",
                    actor=worker_id,
                    now=now,
                )
                return None
            connection.execute(
                """
                UPDATE segments
                SET state = 'TRANSLATION_PROCESSING', lease_owner = ?,
                    lease_expires_at = ?, translation_retry_count = ?,
                    translation_next_attempt_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_expires, attempt_number, now, row["id"]),
            )
            connection.execute(
                """
                INSERT INTO processing_attempts(
                    id, session_id, segment_id, stage, attempt_number, started_at
                ) VALUES (?, ?, ?, 'TRANSLATION', ?, ?)
                """,
                (str(uuid.uuid4()), row["session_id"], row["id"], attempt_number, now),
            )
            return self.get_segment(row["session_id"], row["sequence"])

    def get_or_create_translation_context_snapshot(
        self,
        session_id: str,
        sequence: int,
        context: ContextWindow,
        *,
        prompt_version: int,
    ) -> ContextWindow:
        """Persist the exact first context used for a provider attempt."""
        canonical = json.dumps(
            context.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = _now_iso()
        with self.database.transaction() as connection:
            segment = _require_segment(connection, session_id, sequence)
            existing = connection.execute(
                "SELECT * FROM translation_context_snapshots WHERE segment_id = ?",
                (segment["id"],),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO translation_context_snapshots(
                        segment_id, session_id, segment_sequence, prompt_version,
                        context_json, context_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment["id"], session_id, sequence, prompt_version,
                        canonical, digest, now,
                    ),
                )
                stored = canonical
            else:
                stored = existing["context_json"]
                if hashlib.sha256(stored.encode("utf-8")).hexdigest() != existing["context_sha256"]:
                    raise IdempotencyConflict("translation context snapshot hash mismatch")
        return ContextWindow.model_validate_json(stored)

    def load_translation_context_snapshot(
        self, session_id: str, sequence: int
    ) -> ContextWindow | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT snapshot.context_json, snapshot.context_sha256
                FROM translation_context_snapshots snapshot
                JOIN segments segment ON segment.id = snapshot.segment_id
                WHERE segment.session_id = ? AND segment.sequence = ?
                """,
                (session_id, sequence),
            ).fetchone()
        if row is None:
            return None
        canonical = row["context_json"]
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != row["context_sha256"]:
            raise IdempotencyConflict("translation context snapshot hash mismatch")
        return ContextWindow.model_validate_json(canonical)

    def stage_translation_result(
        self, session_id: str, sequence: int, result: TranslationResult
    ) -> CommitOutcome:
        """Durably buffer a provider result without making it publicly visible."""
        now = _now_iso()
        with self.database.transaction() as connection:
            segment = _require_segment(connection, session_id, sequence)
            if result.segment_id != segment["id"]:
                raise IdempotencyConflict(
                    "translation result segment_id does not match durable key"
                )
            asr = connection.execute(
                "SELECT english_text FROM asr_results WHERE segment_id = ?",
                (segment["id"],),
            ).fetchone()
            if asr is None:
                raise OrderingConflict("translation requires a committed ASR result")
            expected_hash = hashlib.sha256(asr["english_text"].encode("utf-8")).hexdigest()
            if expected_hash != result.english_text_sha256:
                raise SourceHashMismatch(
                    "translation source hash does not match committed English"
                )
            existing = connection.execute(
                "SELECT * FROM translation_staged_results WHERE segment_id = ?",
                (segment["id"],),
            ).fetchone()
            if existing is not None:
                if (
                    existing["chinese_text"] == result.chinese_text
                    and existing["english_text_sha256"] == result.english_text_sha256
                    and existing["provider"] == result.provider
                    and existing["model"] == result.model
                ):
                    return CommitOutcome.ALREADY_COMMITTED
                raise IdempotencyConflict(
                    "translation result already staged with different content"
                )
            if SegmentState(segment["state"]) is not SegmentState.TRANSLATION_PROCESSING:
                raise OrderingConflict("only a processing translation may be staged")
            connection.execute(
                """
                INSERT INTO translation_staged_results(
                    segment_id, session_id, segment_sequence, chinese_text,
                    english_text_sha256, provider, model, provider_request_id,
                    latency_ms, created_at, staged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment["id"], session_id, sequence, result.chinese_text,
                    result.english_text_sha256, result.provider, result.model,
                    result.provider_request_id, result.latency_ms,
                    result.created_at.isoformat(), now,
                ),
            )
            connection.execute(
                """
                UPDATE segments SET lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (now, segment["id"]),
            )
        return CommitOutcome.CREATED

    def release_next_translation_result(self) -> dict | None:
        """Release one staged result at a session's durable ordered cursor."""
        now = _now_iso()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT staged.*, asr.english_text, seg.state AS segment_state
                FROM sessions session
                JOIN translation_staged_results staged
                  ON staged.session_id = session.id
                 AND staged.segment_sequence = session.next_translation_sequence
                JOIN segments seg ON seg.id = staged.segment_id
                JOIN asr_results asr ON asr.segment_id = staged.segment_id
                ORDER BY staged.session_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            current = SegmentState(row["segment_state"])
            ensure_segment_transition(current, SegmentState.DONE)
            expected_hash = hashlib.sha256(row["english_text"].encode("utf-8")).hexdigest()
            if expected_hash != row["english_text_sha256"]:
                raise SourceHashMismatch("staged translation source hash mismatch")
            connection.execute(
                """
                INSERT INTO translation_results(
                    segment_id, session_id, segment_sequence, chinese_text,
                    english_text_sha256, provider, model, provider_request_id,
                    latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["segment_id"], row["session_id"], row["segment_sequence"],
                    row["chinese_text"], row["english_text_sha256"], row["provider"],
                    row["model"], row["provider_request_id"], row["latency_ms"],
                    row["created_at"],
                ),
            )
            connection.execute(
                "UPDATE segments SET state = 'DONE', updated_at = ? WHERE id = ?",
                (now, row["segment_id"]),
            )
            updated = connection.execute(
                """
                UPDATE sessions
                SET next_translation_sequence = next_translation_sequence + 1,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND next_translation_sequence = ?
                """,
                (now, row["session_id"], row["segment_sequence"]),
            )
            if updated.rowcount != 1:
                raise OrderingConflict("translation cursor changed during release")
            connection.execute(
                "DELETE FROM translation_staged_results WHERE segment_id = ?",
                (row["segment_id"],),
            )
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["segment_id"],
                from_state=current.value,
                to_state=SegmentState.DONE.value,
                reason_code="TRANSLATION_RESULT_RELEASED",
                actor="translation_sequencer",
                created_at=now,
            )
            return {
                "session_id": row["session_id"],
                "sequence": row["segment_sequence"],
                "segment_id": row["segment_id"],
                "english_text": row["english_text"],
                "chinese_text": row["chinese_text"],
            }

    def finish_translation_attempt(
        self,
        segment_id: str,
        *,
        attempt_number: int,
        outcome: str,
        latency_ms: int | None = None,
        error_category: str | None = None,
        http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE processing_attempts
                SET finished_at = ?, outcome = ?, latency_ms = ?,
                    error_category = ?, http_status = ?, provider_request_id = ?
                WHERE segment_id = ? AND stage = 'TRANSLATION' AND attempt_number = ?
                """,
                (
                    _now_iso(),
                    outcome,
                    latency_ms,
                    error_category,
                    http_status,
                    provider_request_id,
                    segment_id,
                    attempt_number,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(
                    f"attempt row missing for {segment_id} TRANSLATION #{attempt_number}"
                )

    def mark_translation_retry(
        self,
        session_id: str,
        sequence: int,
        *,
        category: str,
        safe_message: str,
        next_attempt_at: str,
        http_status: int | None = None,
    ) -> SegmentRecord:
        now = _now_iso()
        with self.database.transaction() as connection:
            row = _require_segment(connection, session_id, sequence)
            current = SegmentState(row["state"])
            ensure_segment_transition(current, SegmentState.TRANSLATION_RETRY)
            connection.execute(
                """
                UPDATE segments
                SET state = 'TRANSLATION_RETRY', lease_owner = NULL,
                    lease_expires_at = NULL, translation_next_attempt_at = ?,
                    last_error_category = ?, last_error_safe_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_attempt_at, category, safe_message, now, row["id"]),
            )
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=current.value,
                to_state=SegmentState.TRANSLATION_RETRY.value,
                reason_code=f"TRANSLATION_RETRY_{category}",
                actor="translation_agent",
                created_at=now,
            )
        return self.get_segment(session_id, sequence)

    def mark_translation_failed(
        self,
        session_id: str,
        sequence: int,
        *,
        category: str,
        safe_message: str,
        http_status: int | None = None,
    ) -> SegmentRecord:
        now = _now_iso()
        with self.database.transaction() as connection:
            row = _require_segment(connection, session_id, sequence)
            self._fail_translation_in_transaction(
                connection,
                row,
                category=category,
                message=safe_message,
                reason_code=f"TRANSLATION_FAILED_{category}",
                actor="translation_agent",
                now=now,
            )
            if http_status is not None:
                connection.execute(
                    """
                    UPDATE processing_attempts SET http_status = ?
                    WHERE segment_id = ? AND stage = 'TRANSLATION'
                      AND attempt_number = (SELECT MAX(attempt_number)
                                            FROM processing_attempts
                                            WHERE segment_id = ? AND stage = 'TRANSLATION')
                    """,
                    (http_status, row["id"], row["id"]),
                )
        return self.get_segment(session_id, sequence)

    def _fail_translation_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        category: str,
        message: str,
        reason_code: str,
        actor: str,
        now: str,
    ) -> None:
        """Walk a translation row to TRANSLATION_FAILED through legal steps."""
        state = SegmentState(row["state"])
        if state is SegmentState.TRANSLATION_RETRY:
            ensure_segment_transition(state, SegmentState.TRANSLATION_PENDING)
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=state.value,
                to_state=SegmentState.TRANSLATION_PENDING.value,
                reason_code="TRANSLATION_BACKOFF_ELAPSED",
                actor=actor,
                created_at=now,
            )
            state = SegmentState.TRANSLATION_PENDING
        if state is SegmentState.ASR_DONE:
            ensure_segment_transition(state, SegmentState.TRANSLATION_PENDING)
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=state.value,
                to_state=SegmentState.TRANSLATION_PENDING.value,
                reason_code="TRANSLATION_PROMOTED",
                actor=actor,
                created_at=now,
            )
            state = SegmentState.TRANSLATION_PENDING
        if state is SegmentState.TRANSLATION_PENDING:
            ensure_segment_transition(state, SegmentState.TRANSLATION_PROCESSING)
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=row["id"],
                from_state=state.value,
                to_state=SegmentState.TRANSLATION_PROCESSING.value,
                reason_code="TRANSLATION_LEASE_ACQUIRED",
                actor=actor,
                created_at=now,
            )
            state = SegmentState.TRANSLATION_PROCESSING
        ensure_segment_transition(state, SegmentState.TRANSLATION_FAILED)
        connection.execute(
            """
            UPDATE segments
            SET state = 'TRANSLATION_FAILED', lease_owner = NULL, lease_expires_at = NULL,
                last_error_category = ?, last_error_safe_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (category, message, now, row["id"]),
        )
        _insert_transition_audit(
            connection,
            entity_type="segment",
            entity_id=row["id"],
            from_state=state.value,
            to_state=SegmentState.TRANSLATION_FAILED.value,
            reason_code=reason_code,
            actor=actor,
            created_at=now,
        )
        connection.execute(
            """
            UPDATE sessions
            SET last_error_category = ?, last_error_safe_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (category, message, now, row["session_id"]),
        )

    def commit_translation_result_direct(
        self, session_id: str, sequence: int, result: TranslationResult
    ) -> CommitOutcome:
        """Legacy direct public commit: TEST/REPOSITORY-INTERNAL USE ONLY.

        Production workers must `stage_translation_result` and the
        `TranslationSequencer` alone releases via
        `release_next_translation_result`. This direct path is kept for
        repository-level idempotency/ordering/hash regression tests and
        potential migration tooling; do not call it from runtime code
        (D-034). The idempotency, source-hash and ordering guards here are
        kept intact — they are not weakened shortcuts.
        """
        now = _now_iso()
        with self.database.transaction() as connection:
            segment = _require_segment(connection, session_id, sequence)
            if result.segment_id != segment["id"]:
                raise IdempotencyConflict(
                    "translation result segment_id does not match durable key"
                )
            existing = connection.execute(
                "SELECT * FROM translation_results WHERE segment_id = ?",
                (segment["id"],),
            ).fetchone()
            if existing is not None:
                if (
                    existing["chinese_text"] == result.chinese_text
                    and existing["english_text_sha256"]
                    == result.english_text_sha256
                    and existing["provider"] == result.provider
                    and existing["model"] == result.model
                ):
                    return CommitOutcome.ALREADY_COMMITTED
                raise IdempotencyConflict(
                    "translation result already committed with different content"
                )
            session = connection.execute(
                "SELECT next_translation_sequence FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            expected_sequence = session["next_translation_sequence"]
            if sequence != expected_sequence:
                raise OrderingConflict(
                    f"session expects translation sequence {expected_sequence}, got {sequence}"
                )
            prior_incomplete = connection.execute(
                """
                SELECT sequence FROM segments
                WHERE session_id = ? AND sequence < ? AND state <> ?
                ORDER BY sequence LIMIT 1
                """,
                (session_id, sequence, SegmentState.DONE.value),
            ).fetchone()
            if prior_incomplete is not None:
                raise OrderingConflict(
                    f"segment {sequence} cannot commit before {prior_incomplete['sequence']}"
                )
            asr = connection.execute(
                "SELECT english_text FROM asr_results WHERE segment_id = ?",
                (segment["id"],),
            ).fetchone()
            if asr is None:
                raise OrderingConflict("translation requires a committed ASR result")
            expected_hash = hashlib.sha256(
                asr["english_text"].encode("utf-8")
            ).hexdigest()
            if expected_hash != result.english_text_sha256:
                raise SourceHashMismatch(
                    "translation source hash does not match committed English"
                )
            current = SegmentState(segment["state"])
            ensure_segment_transition(current, SegmentState.DONE)
            connection.execute(
                """
                INSERT INTO translation_results(
                    segment_id, session_id, segment_sequence, chinese_text,
                    english_text_sha256, provider, model, provider_request_id,
                    latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment["id"],
                    session_id,
                    sequence,
                    result.chinese_text,
                    result.english_text_sha256,
                    result.provider,
                    result.model,
                    result.provider_request_id,
                    result.latency_ms,
                    result.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE segments SET state = ?, updated_at = ? WHERE id = ?",
                (SegmentState.DONE.value, now, segment["id"]),
            )
            connection.execute(
                """
                UPDATE sessions
                SET next_translation_sequence = next_translation_sequence + 1,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND next_translation_sequence = ?
                """,
                (now, session_id, sequence),
            )
            _insert_transition_audit(
                connection,
                entity_type="segment",
                entity_id=segment["id"],
                from_state=current.value,
                to_state=SegmentState.DONE.value,
                reason_code="TRANSLATION_RESULT_COMMITTED",
                actor="translation_agent",
                created_at=now,
            )
        return CommitOutcome.CREATED


def _require_segment(
    connection: sqlite3.Connection, session_id: str, sequence: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM segments WHERE session_id = ? AND sequence = ?",
        (session_id, sequence),
    ).fetchone()
    if row is None:
        raise KeyError(f"{session_id}:{sequence}")
    return row


def _insert_transition_audit(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    from_state: str,
    to_state: str,
    reason_code: str,
    actor: str,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO state_transition_audits(
            entity_type, entity_id, from_state, to_state,
            reason_code, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            from_state,
            to_state,
            reason_code,
            actor,
            created_at,
        ),
    )


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        course_name=row["course_name"],
        status=SessionStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=(datetime.fromisoformat(row["started_at"]) if row["started_at"] else None),
        ended_at=(datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None),
        duration_ms=row["duration_ms"],
        summary_path=Path(row["summary_path"]) if row["summary_path"] else None,
        summary_hash=row["summary_hash"],
        summary_model=row["summary_model"],
        app_version=row["app_version"],
        purged_at=(datetime.fromisoformat(row["purged_at"]) if row["purged_at"] else None),
        next_translation_sequence=row["next_translation_sequence"],
        last_error_category=row["last_error_category"],
        last_error_safe_message=row["last_error_safe_message"],
    )


def _segment_from_row(row: sqlite3.Row) -> SegmentRecord:
    return SegmentRecord(
        id=row["id"],
        session_id=row["session_id"],
        sequence=row["sequence"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        duration_ms=row["duration_ms"],
        overlap_ms=row["overlap_ms"],
        audio_path=Path(row["audio_path"]),
        audio_sha256=row["audio_sha256"],
        state=SegmentState(row["state"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
