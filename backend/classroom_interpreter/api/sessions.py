from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.agents.cleanup import PurgeRefused
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
        record = runtime.repository.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND") from None
    payload = _session_payload(request, runtime, session_id)
    payload["committed_history"] = _committed_history(runtime, session_id)
    return payload


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
