from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.providers.summary_contract import validate_summary_document
from classroom_interpreter.storage import Database
from classroom_interpreter.storage.repository import SessionRepository

MANIFEST_SCHEMA_VERSION = 1


class PurgeRefused(RuntimeError):
    """Cleanup refuses the operation: the gate or containment rules failed.

    NO VALID SUMMARY = NO PURGE is enforced here, independently of the state
    machine: the retained summary is re-read, re-validated and re-hashed
    before and after every deletion pass.
    """


@dataclass(slots=True)
class CleanupAgent:
    """Idempotent, exact-manifest purge of session-scoped temporary content.

    Cleanup accepts only a validated manifest it built itself from the durable
    DB state; every path is canonicalized, symlink-rejected and strictly
    contained within the session directory. It never deletes the application
    root or the summary store, and never decides summary validity — it
    revalidates an already-SAVED session. Deletion is logical (no forensic
    erase claim).
    """

    database: Database
    summaries_root: Path
    session_store_root: Path

    # ------------------------------------------------------------- manifest

    def session_dir(self, session_id: str) -> Path:
        return self.session_store_root / session_id

    def build_manifest(self, session_id: str) -> dict:
        session_dir = self.session_dir(session_id).resolve()
        summaries_root = self.summaries_root.resolve()
        if not session_dir.is_dir():
            raise PurgeRefused("session directory does not exist")
        if session_dir == self.session_store_root.resolve():
            raise PurgeRefused("session root itself is not purgeable")
        if session_dir.is_relative_to(summaries_root):
            raise PurgeRefused("session directory must not live inside the summary store")
        files: list[str] = []
        for path in sorted(session_dir.rglob("*")):
            if path.is_symlink():
                raise PurgeRefused(f"symlink inside session tree: {path.name}")
            if path.is_file():
                resolved = path.resolve()
                if not resolved.is_relative_to(session_dir):
                    raise PurgeRefused("manifest path escapes the session directory")
                files.append(str(path))
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "session_id": session_id,
            "session_dir": str(session_dir),
            "files": files,
        }

    # ------------------------------------------------------------- journal

    def _journal(self, session_id: str, step: str, target: str, outcome: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO purge_journal(session_id, step, target, outcome, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, step, target, outcome, datetime.now(timezone.utc).isoformat()),
            )

    # ------------------------------------------------------- summary checks

    def _revalidate_summary(self, record) -> Path:
        """Independent gate: NO VALID SUMMARY = NO PURGE."""
        if record.status not in ("SUMMARY_SAVED", "PURGING"):
            raise PurgeRefused(f"NO_VALID_SUMMARY: session status is {record.status}")
        if not record.summary_path or not record.summary_hash:
            raise PurgeRefused("NO_VALID_SUMMARY: no summary record on the session")
        path = Path(record.summary_path)
        if path.is_symlink() or not path.is_file():
            raise PurgeRefused("NO_VALID_SUMMARY: summary file is missing")
        resolved = path.resolve()
        if resolved.is_relative_to(self.session_dir(record.id).resolve()):
            raise PurgeRefused("summary must live outside the session directory")
        if not resolved.is_relative_to(self.summaries_root.resolve()):
            raise PurgeRefused("summary does not live in the summary store")
        payload = path.read_bytes()
        if not payload:
            raise PurgeRefused("NO_VALID_SUMMARY: summary file is empty")
        if hashlib.sha256(payload).hexdigest() != record.summary_hash:
            raise PurgeRefused("NO_VALID_SUMMARY: summary hash mismatch")
        validate_summary_document(payload.decode("utf-8"))
        return path

    # ----------------------------------------------------------------- purge

    def purge(self, session_id: str) -> dict:
        """Purge a SUMMARY_SAVED session: files, then rows, then the allowlisted
        metadata row. Idempotent and resumable after partial completion."""
        repository = SessionRepository(self.database)
        try:
            record = repository.get_session(session_id)
        except KeyError:
            return {"outcome": "ALREADY_PURGED", "files_deleted": 0}
        summary_path = self._revalidate_summary(record)

        if record.status == "SUMMARY_SAVED":
            repository.transition_session(
                session_id,
                SessionStatus.PURGING,
                reason_code="PURGE_AUTHORIZED",
                actor="cleanup_agent",
            )
        elif record.status != "PURGING":
            raise PurgeRefused("session is not in a purgeable state")

        manifest = self._load_or_skip_manifest(session_id, resume=record.status == "PURGING")
        files_deleted = 0
        if manifest is not None:
            for target in manifest["files"]:
                path = Path(target)
                if path.exists():
                    path.unlink()
                    files_deleted += 1
                    self._journal(session_id, "delete_file", target, "DELETED")
                else:
                    self._journal(session_id, "delete_file", target, "ABSENT")
            self._remove_dirs(session_id, Path(manifest["session_dir"]))

            residue = [target for target in manifest["files"] if Path(target).exists()]
            if residue:
                raise PurgeRefused("purge residue detected; refusing to finalize")
        if (not summary_path.is_file()) or (
            hashlib.sha256(summary_path.read_bytes()).hexdigest()
            != repository.get_session(session_id).summary_hash
        ):
            raise PurgeRefused("retained summary changed during purge")

        self._finalize_rows(session_id, record, summary_path, files_deleted)
        return {
            "outcome": "PURGED",
            "files_deleted": files_deleted,
            "summary_path": str(summary_path),
        }

    def _load_or_skip_manifest(self, session_id: str, *, resume: bool) -> dict | None:
        """Build the manifest; a missing directory only means the file phase
        already completed on an earlier pass (crash resume)."""
        try:
            return self.build_manifest(session_id)
        except PurgeRefused as exc:
            if "does not exist" in str(exc):
                return None
            raise

    def _remove_dirs(self, session_id: str, session_dir: Path) -> None:
        if session_dir.exists():
            for directory in sorted(
                (p for p in session_dir.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                directory.rmdir()
            session_dir.rmdir()
        self._journal(session_id, "remove_session_dir", str(session_dir), "REMOVED")

    def _finalize_rows(self, session_id: str, record, summary_path: Path, files_deleted: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                return  # already finalized (idempotent)
            segment_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM segments WHERE session_id = ?", (session_id,)
                ).fetchall()
            ]
            connection.execute(
                """
                INSERT INTO purged_session_metadata(
                    session_id, course_name, started_at, ended_at, duration_ms,
                    summary_path, summary_hash, summary_model, app_version,
                    purged_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PURGED')
                """,
                (
                    session_id,
                    record.course_name,
                    (record.started_at or record.created_at).isoformat(),
                    (record.ended_at or record.created_at).isoformat(),
                    record.duration_ms or 0,
                    str(summary_path),
                    record.summary_hash,
                    record.summary_model or "unknown",
                    record.app_version,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            # Session-linked audits are removed with the temporary row; the
            # metadata ledger above is the only retained record.
            connection.execute(
                "DELETE FROM state_transition_audits WHERE entity_id = ?",
                (session_id,),
            )
            for segment_id in segment_ids:
                connection.execute(
                    "DELETE FROM state_transition_audits WHERE entity_id = ?",
                    (segment_id,),
                )
        self._journal(session_id, "finalize_metadata", session_id, "PURGED")

    # ------------------------------------------------- unfinished-session delete

    def delete_unfinished(self, session_id: str) -> dict:
        """User-requested deletion of an unfinished session (destructive,
        explicit, never automatic). Sessions claiming a valid saved summary
        must go through purge instead."""
        repository = SessionRepository(self.database)
        try:
            record = repository.get_session(session_id)
        except KeyError:
            return {"outcome": "ALREADY_DELETED", "files_deleted": 0}
        if record.status == "SUMMARY_SAVED":
            raise PurgeRefused("session has a saved summary; purge applies instead")
        if record.status == "PURGED":
            return {"outcome": "ALREADY_DELETED", "files_deleted": 0}

        manifest = self._load_or_skip_manifest(session_id, resume=True)
        files_deleted = 0
        if manifest is not None:
            for target in manifest["files"]:
                path = Path(target)
                if path.exists():
                    path.unlink()
                    files_deleted += 1
                    self._journal(session_id, "delete_file", target, "DELETED")
            self._remove_dirs(session_id, Path(manifest["session_dir"]))

        with self.database.transaction() as connection:
            session = connection.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                return {"outcome": "DELETED", "files_deleted": files_deleted}
            segment_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM segments WHERE session_id = ?", (session_id,)
                ).fetchall()
            ]
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            connection.execute(
                "DELETE FROM state_transition_audits WHERE entity_id = ?", (session_id,)
            )
            for segment_id in segment_ids:
                connection.execute(
                    "DELETE FROM state_transition_audits WHERE entity_id = ?",
                    (segment_id,),
                )
        self._journal(session_id, "delete_complete", session_id, "DELETED")
        return {"outcome": "DELETED", "files_deleted": files_deleted}
