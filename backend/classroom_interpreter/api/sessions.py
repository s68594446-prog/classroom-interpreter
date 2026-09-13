from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.agents.cleanup import PurgeRefused
from classroom_interpreter.providers.summary_contract import validate_summary_document
from classroom_interpreter.runtime.pipeline import (
    CaptureStartError,
    SessionNotFoundError,
    SingleActiveSessionError,
)

router = APIRouter(prefix="/api")


class StartClassRequest(BaseModel):
    course_name: str = Field(min_length=1, max_length=200)


def _get_runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="RUNTIME_NOT_CONFIGURED")
    return runtime


def _session_payload(request: Request, runtime, session_id: str) -> dict:
    record = runtime.repository.get_session(session_id)
    return {
        "session": _record_dict(record),
        "progress": runtime.progress(session_id),
    }


def _record_dict(record) -> dict:
    return {
        "id": record.id,
        "course_name": record.course_name,
        "status": record.status.value,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "ended_at": record.ended_at.isoformat() if record.ended_at else None,
        "duration_ms": record.duration_ms,
        "summary_path": str(record.summary_path) if record.summary_path else None,
        "summary_hash": record.summary_hash,
        "summary_model": record.summary_model,
        "app_version": record.app_version,
        "next_translation_sequence": record.next_translation_sequence,
        "last_error_category": getattr(record, "last_error_category", None),
        "last_error_safe_message": getattr(record, "last_error_safe_message", None),
        # The first error is what explains the session; the last one is often
        # only its consequence.
        "first_error_category": getattr(record, "first_error_category", None),
        "first_error_safe_message": getattr(record, "first_error_safe_message", None),
    }


@router.post("/sessions", status_code=201)
async def start_class(payload: StartClassRequest, request: Request) -> dict:
    runtime = _get_runtime(request)
    try:
        record = await runtime.start_class(payload.course_name.strip())
    except SingleActiveSessionError as exc:
        raise HTTPException(status_code=409, detail="SINGLE_ACTIVE_SESSION") from exc
    except CaptureStartError as exc:
        raise HTTPException(status_code=503, detail="PROVIDER_NOT_CONFIGURED") from exc
    except Exception as exc:
        # Microphone open failures surface as 503 CAPTURE_START_FAILED;
        # exception details are content-free (no paths, no audio).
        raise HTTPException(
            status_code=503, detail=f"CAPTURE_START_FAILED:{type(exc).__name__}"
        ) from exc
    return _session_payload(request, runtime, record.id)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict:
    runtime = _get_runtime(request)
    try:
        runtime.repository.get_session(session_id)
    except KeyError:
        # A class that finished correctly has no `sessions` row: purge deletes
        # it and keeps the metadata ledger. Answering 404 made the client that
        # was polling a session it had just ended simply stop updating -- the
        # screen sat on "generating summary" forever for the one outcome that
        # actually worked.
        purged = _purged_payload(runtime, session_id)
        if purged is None:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND") from None
        return purged
    payload = _session_payload(request, runtime, session_id)
    payload["committed_history"] = _committed_history(runtime, session_id)
    return payload


def _purged_payload(runtime, session_id: str) -> dict | None:
    with runtime.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "session": {
            "id": row["session_id"],
            "course_name": row["course_name"],
            "status": "PURGED",
            "created_at": row["started_at"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_ms": row["duration_ms"],
            "summary_path": row["summary_path"],
            "summary_hash": row["summary_hash"],
            "summary_model": row["summary_model"],
            "app_version": row["app_version"],
            "next_translation_sequence": 0,
            "last_error_category": None,
            "last_error_safe_message": None,
            "first_error_category": None,
            "first_error_safe_message": None,
        },
        "progress": runtime.progress(session_id),
        "committed_history": [],
    }


def _committed_history(runtime, session_id: str) -> list[dict]:
    with runtime.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT s.sequence, a.english_text, t.chinese_text,
                   s.start_ms, s.end_ms
            FROM segments s
            JOIN asr_results a ON a.segment_id = s.id
            JOIN translation_results t ON t.segment_id = s.id
            WHERE s.session_id = ? AND s.state = 'DONE'
            ORDER BY s.sequence
            """,
            (session_id,),
        ).fetchall()
    return [
        {
            "sequence": row["sequence"],
            "english_text": row["english_text"],
            "chinese_text": row["chinese_text"],
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
        }
        for row in rows
    ]


@router.post("/sessions/{session_id}/pause")
async def pause_session(session_id: str, request: Request) -> dict:
    runtime = _get_runtime(request)
    try:
        record = await runtime.pause(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SESSION_NOT_ACTIVE") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail=f"PAUSE_FAILED:{type(exc).__name__}"
        ) from exc
    return _record_dict(record)


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str, request: Request) -> dict:
    runtime = _get_runtime(request)
    try:
        record = await runtime.resume(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SESSION_NOT_ACTIVE") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail=f"RESUME_FAILED:{type(exc).__name__}"
        ) from exc
    return _record_dict(record)


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, request: Request) -> dict:
    runtime = _get_runtime(request)
    try:
        record = await runtime.end_class(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SESSION_NOT_ACTIVE") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail=f"END_FAILED:{type(exc).__name__}"
        ) from exc
    return _record_dict(record)


@router.get("/recovery")
def recovery_list(request: Request) -> dict:
    runtime = _get_runtime(request)
    sessions = []
    for record in runtime.unfinished_sessions():
        sessions.append(
            {
                **_record_dict(record),
                "progress": runtime.progress(record.id),
            }
        )
    return {"sessions": sessions}


@router.post("/recovery/{session_id}/resume")
async def recovery_resume(session_id: str, request: Request) -> dict:
    runtime = _get_runtime(request)
    try:
        record = await runtime.recover(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND") from None
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail=f"RECOVERY_RESUME_FAILED:{type(exc).__name__}"
        ) from exc
    return _record_dict(record)


@router.delete("/recovery/{session_id}")
def recovery_delete(session_id: str, request: Request) -> dict:
    """Destructive, explicit user action for unfinished sessions only. The
    client must present an action-time confirmation before calling this."""
    runtime = _get_runtime(request)
    try:
        return runtime.delete_session(session_id)
    except PurgeRefused as exc:
        raise HTTPException(status_code=409, detail=f"PURGE_REFUSED:{exc}") from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND") from None


def _summaries_root(request: Request) -> Path:
    return (
        request.app.state.settings.classroom_interpreter_data_dir / "summaries"
    ).resolve()


def _summary_binding(runtime, session_id: str) -> tuple[str, str, str | None] | None:
    """(summary_path, summary_hash, course_name) for a class, live or purged.

    A completed class no longer has a `sessions` row: purge deletes it and
    keeps `purged_session_metadata` as the retained ledger. Reading only the
    live table would make the summary unreachable for exactly the classes that
    finished correctly.
    """
    try:
        record = runtime.repository.get_session(session_id)
        if record.summary_path and record.summary_hash:
            return str(record.summary_path), record.summary_hash, record.course_name
        return None
    except KeyError:
        pass
    with runtime.database.connect() as connection:
        row = connection.execute(
            """
            SELECT course_name, summary_path, summary_hash
            FROM purged_session_metadata WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return row["summary_path"], row["summary_hash"], row["course_name"]


@router.get("/summaries")
def summary_list(request: Request) -> dict:
    """Every class that produced a summary, newest first.

    Without this the product's only deliverable was unreachable. A class was
    recorded, transcribed, translated, summarised, verified and then purged --
    and the summary was left as a filesystem path in a database column that no
    endpoint served and no screen showed.

    It has to read both tables. Purge deletes the `sessions` row and keeps
    `purged_session_metadata`, so a class that completed *correctly* is exactly
    the one the live table has forgotten.
    """
    runtime = _get_runtime(request)
    with runtime.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT id AS session_id, course_name, status, started_at, ended_at,
                   duration_ms,
                   COALESCE(summary_saved_at, ended_at, created_at) AS finished_at
            FROM sessions
            WHERE summary_path IS NOT NULL AND summary_hash IS NOT NULL
            UNION ALL
            SELECT session_id, course_name, status, started_at, ended_at,
                   duration_ms, purged_at AS finished_at
            FROM purged_session_metadata
            ORDER BY finished_at DESC
            """
        ).fetchall()
    return {
        "summaries": [
            {
                "session_id": row["session_id"],
                "course_name": row["course_name"],
                "status": row["status"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration_ms": row["duration_ms"],
                "finished_at": row["finished_at"],
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}/summary")
def session_summary(session_id: str, request: Request) -> dict:
    """The saved summary document for one class.

    Re-verified on every read, with the same discipline cleanup uses before it
    deletes anything: the path must resolve inside the summary store and must
    not be a symlink, and the bytes must still hash to what was recorded when
    the summary was saved. A summary that no longer matches its hash is not
    served as if it were the class's notes -- it is reported as altered.
    """
    runtime = _get_runtime(request)
    binding = _summary_binding(runtime, session_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="NO_SUMMARY_YET")
    raw_path, expected_hash, course_name = binding
    path = Path(raw_path)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=410, detail="SUMMARY_FILE_MISSING")
    resolved = path.resolve()
    if not resolved.is_relative_to(_summaries_root(request)):
        raise HTTPException(status_code=409, detail="SUMMARY_OUTSIDE_STORE")
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise HTTPException(status_code=409, detail="SUMMARY_HASH_MISMATCH")
    text = payload.decode("utf-8")
    try:
        validate_summary_document(text)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="SUMMARY_INVALID") from exc
    return {
        "session_id": session_id,
        "course_name": course_name,
        "markdown": text,
        "filename": resolved.name,
    }


class GlossaryTermRequest(BaseModel):
    """A term the user typed. Never derived from a recording."""

    term: str = Field(min_length=1, max_length=120)
    preferred_chinese: str | None = Field(default=None, max_length=120)


@router.get("/glossary/{course_name}")
def glossary_list(course_name: str, request: Request) -> dict:
    """Terms for a course.

    Keyed by course rather than session so a weekly course keeps its vocabulary
    without re-entry.
    """
    runtime = _get_runtime(request)
    return {"terms": runtime.repository.list_glossary_terms(course_name)}


@router.post("/glossary/{course_name}")
def glossary_add(
    course_name: str, payload: GlossaryTermRequest, request: Request
) -> dict:
    runtime = _get_runtime(request)
    try:
        return runtime.repository.add_glossary_term(
            course_name, payload.term, payload.preferred_chinese
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/glossary/term/{term_id}")
def glossary_delete(term_id: str, request: Request) -> dict:
    runtime = _get_runtime(request)
    if not runtime.repository.delete_glossary_term(term_id):
        raise HTTPException(status_code=404, detail="TERM_NOT_FOUND")
    return {"deleted": term_id}
