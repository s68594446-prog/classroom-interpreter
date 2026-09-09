from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from classroom_interpreter.agents.context import estimate_tokens
from classroom_interpreter.audio.durable import sync_directory
from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.providers.base import SummaryProvider
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.summary_contract import (
    SUMMARY_PROMPT_VERSION,
    validate_summary_document,
)
from classroom_interpreter.storage import Database
from classroom_interpreter.storage.repository import SessionRepository

_replace_summary_file = os.replace


class SummaryValidationError(RuntimeError):
    """Raised when source transcript or provider output fails validation."""


@dataclass(slots=True)
class SummaryAgent:
    """Hierarchical, resumable summary generation (D-030).

    English transcript is the factual source (D-019). Every step is durable:
    chunk boundaries, per-chunk notes, and job progress live in SQLite and are
    reused after a crash. The final document passes structural validation
    BEFORE any file write, then follows the atomic protocol: temp write →
    fsync → atomic rename → directory fsync → read-back → re-validation →
    SHA256 match → SUMMARY_SAVED. `NO VALID SUMMARY = NO PURGE` is enforced by
    the session state machine plus independent revalidation in Phase 7.
    """

    database: Database
    provider: SummaryProvider
    summaries_root: Path
    session_store_root: Path
    chunk_token_budget: int = 3000
    max_attempts: int = 3

    # ------------------------------------------------------------- transcript

    def _load_done_segments(self, session_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.sequence, s.start_ms, s.end_ms, a.english_text
                FROM segments s
                JOIN asr_results a ON a.segment_id = s.id
                WHERE s.session_id = ? AND s.state = 'DONE'
                ORDER BY s.sequence
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def verify_drained(self, session_id: str) -> None:
        """All segment sequences must be contiguous and DONE before summary."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT sequence, state FROM segments WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        sequences = [row["sequence"] for row in rows]
        if sequences != list(range(len(sequences))):
            raise SummaryValidationError("segment sequences are not contiguous")
        bad = [row["sequence"] for row in rows if row["state"] != "DONE"]
        if bad:
            raise SummaryValidationError(
                f"segments not DONE: {bad[:5]}" + (" ..." if len(bad) > 5 else "")
            )

    # ------------------------------------------------------------------ chunks

    def _persist_chunk(
        self,
        session_id: str,
        job_id: str,
        chunk_index: int,
        start_sequence: int,
        end_sequence: int,
    ) -> None:
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM summary_chunks WHERE job_id = ? AND chunk_index = ?",
                (job_id, chunk_index),
            ).fetchone()
            if exists:
                return
            connection.execute(
                """
                INSERT INTO summary_chunks(
                    id, session_id, job_id, chunk_index, start_sequence, end_sequence,
                    state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    str(uuid.uuid4()),
                    session_id,
                    job_id,
                    chunk_index,
                    start_sequence,
                    end_sequence,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def _ensure_chunks(
        self, session_id: str, job_id: str, segments: list[dict]
    ) -> int:
        """Cut chunk boundaries once by token budget; existing chunks are
        never re-cut, so resume is stable."""
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) AS n FROM summary_chunks WHERE job_id = ?",
                (job_id,),
            ).fetchone()["n"]
        if existing:
            return existing

        current: list[dict] = []
        chunk_index = 0
        remaining = self.chunk_token_budget
        for segment in segments:
            tokens = estimate_tokens(segment["english_text"])
            if current and tokens > remaining:
                self._persist_chunk(
                    session_id, job_id, chunk_index,
                    current[0]["sequence"], current[-1]["sequence"],
                )
                chunk_index += 1
                current = []
                remaining = self.chunk_token_budget
            current.append(segment)
            remaining -= tokens
        if current:
            self._persist_chunk(
                session_id, job_id, chunk_index,
                current[0]["sequence"], current[-1]["sequence"],
            )
            chunk_index += 1
        return chunk_index

    def _chunk_material(
        self, session_id: str, start_sequence: int, end_sequence: int
    ) -> str:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.english_text FROM segments s
                JOIN asr_results a ON a.segment_id = s.id
                WHERE s.session_id = ? AND s.sequence >= ? AND s.sequence <= ?
                ORDER BY s.sequence
                """,
                (session_id, start_sequence, end_sequence),
            ).fetchall()
        return "\n\n".join(row["english_text"] for row in rows)

    # --------------------------------------------------------------------- job

    def _ensure_job(self, session_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM summary_jobs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO summary_jobs(
                        id, session_id, state, schema_version, chunk_count,
                        completed_chunk_count, updated_at
                    ) VALUES (?, ?, 'PENDING', ?, 0, 0, ?)
                    """,
                    (str(uuid.uuid4()), session_id, f"summary-prompt-v{SUMMARY_PROMPT_VERSION}", now),
                )
                row = connection.execute(
                    "SELECT * FROM summary_jobs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        return dict(row)

    def _set_job_state(self, job_id: str, state: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE summary_jobs SET state = ?, updated_at = ? WHERE id = ?",
                (state, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def _complete_chunk(
        self,
        job_id: str,
        chunk_row_id: str,
        notes: str,
        provider_name: str,
        model: str,
        latency_ms: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE summary_chunks
                SET state = 'DONE', section_summary = ?, provider = ?, model = ?,
                    latency_ms = ?, updated_at = ?
                WHERE id = ? AND state != 'DONE'
                """,
                (notes, provider_name, model, latency_ms, now, chunk_row_id),
            )
            connection.execute(
                """
                UPDATE summary_jobs
                SET completed_chunk_count = (
                    SELECT COUNT(*) FROM summary_chunks WHERE job_id = ? AND state = 'DONE'
                ), updated_at = ?
                WHERE id = ?
                """,
                (job_id, now, job_id),
            )

    # ------------------------------------------------------------------- main

    async def summarize_async(self, session_id: str) -> Path:
        repository = SessionRepository(self.database)
        record = repository.get_session(session_id)
        if record.status is SessionStatus.SUMMARY_SAVED:
            return self._verify_saved_summary(record)
        self.verify_drained(session_id)

        job = self._ensure_job(session_id)
        if job["state"] == "SAVED":
            refreshed = repository.get_session(session_id)
            if refreshed.status is SessionStatus.SUMMARY_SAVED:
                return self._verify_saved_summary(refreshed)
        self._set_job_state(job["id"], "CHUNKING")

        segments = self._load_done_segments(session_id)
        if not segments:
            raise SummaryValidationError("no committed transcript material")
        chunk_count = self._ensure_chunks(session_id, job["id"], segments)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE summary_jobs SET chunk_count = ?, updated_at = ? WHERE id = ?",
                (chunk_count, datetime.now(timezone.utc).isoformat(), job["id"]),
            )
            chunk_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM summary_chunks WHERE job_id = ? ORDER BY chunk_index",
                    (job["id"],),
                ).fetchall()
            ]

        self._set_job_state(job["id"], "CHUNKS_IN_PROGRESS")
        for chunk in chunk_rows:
            if chunk["state"] == "DONE":
                continue
            material = self._chunk_material(
                session_id, chunk["start_sequence"], chunk["end_sequence"]
            )
            notes: str | None = None
            last_error: Exception | None = None
            started = time.monotonic()
            for _attempt in range(1, self.max_attempts + 1):
                try:
                    notes = await self.provider.summarize_chunk(
                        chunk_index=chunk["chunk_index"], transcript_material=material
                    )
                    break
                except ProviderFailure as failure:
                    last_error = failure
                    if not failure.error.retryable:
                        break
            if notes is None:
                self._set_job_state(job["id"], "FAILED")
                raise SummaryValidationError(
                    f"chunk {chunk['chunk_index']} failed: "
                    + (last_error.safe_message if last_error else "unknown provider failure")
                )
            self._complete_chunk(
                job["id"],
                chunk["id"],
                notes,
                self.provider.name,
                self.provider.model,
                int((time.monotonic() - started) * 1000),
            )

        self._set_job_state(job["id"], "SYNTHESIS")
        with self.database.connect() as connection:
            done_notes = [
                row["section_summary"]
                for row in connection.execute(
                    """
                    SELECT section_summary FROM summary_chunks
                    WHERE job_id = ? AND state = 'DONE' ORDER BY chunk_index
                    """,
                    (job["id"],),
                ).fetchall()
            ]
        document = await self.provider.synthesize(
            course_name=record.course_name,
            date=_date_text(record),
            duration_text=_duration_text(record),
            chunk_summaries=done_notes,
        )
        document = validate_summary_document(document)

        self._set_job_state(job["id"], "SAVING")
        summary_path = self._atomic_write(session_id, record.course_name, document)
        saved_hash = hashlib.sha256(summary_path.read_bytes()).hexdigest()

        # Only now advance the session: metadata, state and audit commit in one
        # SQLite transaction. SUMMARY_SAVED can never be visible without the
        # path/hash needed by the independent purge gate.
        repository.commit_summary_saved(
            session_id,
            summary_path=summary_path,
            summary_hash=saved_hash,
            summary_model=self.provider.model,
        )
        self._set_job_state(job["id"], "SAVED")
        return summary_path

    # ------------------------------------------------------------ atomic save

    def _summary_path(self, session_id: str, course_name: str) -> Path:
        date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        directory = self.summaries_root
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / f"{date_text}-{_safe_course_name(course_name)}-{session_id}.md"

    def _atomic_write(self, session_id: str, course_name: str, document: str) -> Path:
        """temp → fsync → atomic rename → dir fsync → read-back → re-validate.
        The final path always lives outside the session directory."""
        final_path = self._summary_path(session_id, course_name)
        session_root = (self.session_store_root / session_id).resolve()
        if final_path.resolve().is_relative_to(session_root):
            raise SummaryValidationError("summary path must live outside the session tree")
        temporary = final_path.with_suffix(".md.tmp")
        payload = document.encode("utf-8")
        if temporary.is_symlink():
            raise SummaryValidationError("summary temporary path is a symlink")
        if temporary.exists():
            # A prior process may have died after temp fsync but before rename.
            # It is derived output and safe to regenerate from durable summary
            # work; never follow or overwrite a symlink.
            temporary.unlink()
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_summary_file(temporary, final_path)
        sync_directory(final_path.parent)
        read_back = final_path.read_bytes()
        if hashlib.sha256(read_back).hexdigest() != hashlib.sha256(payload).hexdigest():
            raise SummaryValidationError("summary read-back hash mismatch")
        validate_summary_document(read_back.decode("utf-8"))
        return final_path

    def _verify_saved_summary(self, record) -> Path:
        if not record.summary_path or not record.summary_hash:
            raise SummaryValidationError("session claims SAVED but has no summary record")
        path = Path(record.summary_path)
        if path.is_symlink() or not path.is_file():
            raise SummaryValidationError("saved summary file is missing")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != record.summary_hash:
            raise SummaryValidationError("saved summary hash mismatch")
        return path


def _date_text(record) -> str:
    reference = record.started_at or record.created_at
    return reference.date().isoformat()


def _duration_text(record) -> str:
    ms = record.duration_ms or 0
    total_minutes = int(ms // 60000)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} 小时 {minutes} 分钟"


def _safe_course_name(course_name: str) -> str:
    cleaned = []
    for char in course_name.strip():
        if char in '/\\:*?"<>|' or ord(char) < 32:
            cleaned.append("_")
        else:
            cleaned.append(char)
    text = "".join(cleaned).strip(". ")
    return text[:60] or "未命名课程"
