from __future__ import annotations

import shutil

from fastapi import APIRouter, Request

from classroom_interpreter.domain.models import (
    HealthComponent,
    HealthSnapshot,
    HealthStatus,
)

router = APIRouter()

# Content-free thresholds; calibrated defaults, not user data.
DISK_GREEN_BYTES = 2 * 1024**3
DISK_YELLOW_BYTES = 512 * 1024**2


def _microphone_component(request: Request) -> HealthComponent:
    listener = getattr(request.app.state, "audio_listener", None)
    runtime = getattr(request.app.state, "runtime", None)
    if listener is None and runtime is not None:
        listener = getattr(runtime, "listener", None)
    if listener is None:
        return HealthComponent(
            name="microphone",
            status=HealthStatus.UNKNOWN,
            reason_code="CAPTURE_NOT_ATTACHED",
        )
    snapshot = listener.snapshot()
    if snapshot.get("capture_active"):
        status, reason = HealthStatus.GREEN, "CAPTURE_ACTIVE"
    elif snapshot.get("state") == "PAUSED":
        status, reason = HealthStatus.YELLOW, "CAPTURE_PAUSED"
    elif snapshot.get("reason_code"):
        status, reason = HealthStatus.RED, str(snapshot["reason_code"])
    else:
        status, reason = HealthStatus.UNKNOWN, "CAPTURE_IDLE"
    return HealthComponent(name="microphone", status=status, reason_code=reason)


def _storage_component(request: Request) -> HealthComponent:
    settings = request.app.state.settings
    root = settings.classroom_interpreter_data_dir
    if not root.exists():
        root = root.parent
    free = shutil.disk_usage(root).free
    if free >= DISK_GREEN_BYTES:
        status, reason = HealthStatus.GREEN, "STORAGE_OK"
    elif free >= DISK_YELLOW_BYTES:
        status, reason = HealthStatus.YELLOW, "STORAGE_LOW"
    else:
        status, reason = HealthStatus.RED, "STORAGE_CRITICAL"
    return HealthComponent(name="storage", status=status, reason_code=reason)


def _pipeline_component(request: Request, name: str, failed_states: tuple[str, ...]) -> HealthComponent:
    runtime = getattr(request.app.state, "runtime", None)
    session_id = getattr(runtime, "active_session", None) if runtime else None
    if runtime is None or session_id is None:
        return HealthComponent(name=name, status=HealthStatus.UNKNOWN, reason_code="NO_ACTIVE_SESSION")
    progress = runtime.progress(session_id)
    counts = progress["segment_counts"]
    failed = sum(counts.get(state, 0) for state in failed_states)
    if failed:
        return HealthComponent(name=name, status=HealthStatus.RED, reason_code="SEGMENT_FAILURES")
    if progress["pending_segments"] > 20:
        return HealthComponent(name=name, status=HealthStatus.YELLOW, reason_code="BACKLOG_HIGH")
    return HealthComponent(name=name, status=HealthStatus.GREEN, reason_code="OK")


def _provider_component(request: Request) -> HealthComponent:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return HealthComponent(name="provider", status=HealthStatus.UNKNOWN, reason_code="NO_RUNTIME")
    if runtime.asr_provider is None or runtime.translation_provider is None:
        return HealthComponent(
            name="provider",
            status=HealthStatus.RED,
            reason_code="MIMO_KEY_MISSING",
        )
    session_id = getattr(runtime, "active_session", None)
    if session_id is None:
        return HealthComponent(name="provider", status=HealthStatus.GREEN, reason_code="OK")
    record = runtime.repository.get_session(session_id)
    category = getattr(record, "last_error_category", None)
    if category in ("AUTH", "BALANCE", "CONTENT_FILTER"):
        return HealthComponent(name="provider", status=HealthStatus.RED, reason_code=f"PROVIDER_{category}")
    if category in ("TIMEOUT", "NETWORK", "SERVER", "RATE_LIMIT"):
        return HealthComponent(name="provider", status=HealthStatus.YELLOW, reason_code=f"PROVIDER_{category}")
    return HealthComponent(name="provider", status=HealthStatus.GREEN, reason_code="OK")


def _supervision_component(request: Request) -> HealthComponent:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return HealthComponent(
            name="runtime-supervision",
            status=HealthStatus.UNKNOWN,
            reason_code="NO_RUNTIME",
        )
    store = runtime.health_store
    if runtime.worker_faults:
        return HealthComponent(
            name="runtime-supervision",
            status=HealthStatus.RED,
            reason_code="WORKER_EXCEPTION_RECORDED",
        )
    if store.last_flush_error or store.dropped_samples:
        return HealthComponent(
            name="runtime-supervision",
            status=HealthStatus.YELLOW,
            reason_code="METRICS_DEGRADED",
        )
    return HealthComponent(
        name="runtime-supervision",
        status=HealthStatus.GREEN,
        reason_code="OK",
    )


@router.get("/health/capture")
def capture_health(request: Request) -> dict:
    # Optional injected backend-owned capture; HTTP reads never acquire microphone.
    listener = getattr(request.app.state, "audio_listener", None)
    runtime = getattr(request.app.state, "runtime", None)
    if listener is None and runtime is not None:
        listener = getattr(runtime, "listener", None)
    if listener is None:
        return {"capture_active": False, "reason_code": "CAPTURE_NOT_ATTACHED"}
    return listener.snapshot()


@router.get("/health", response_model=HealthSnapshot)
def health(request: Request) -> HealthSnapshot:
    settings = request.app.state.settings
    components = (
        _microphone_component(request),
        _storage_component(request),
        _pipeline_component(request, "asr", ("ASR_FAILED",)),
        _pipeline_component(request, "translation", ("TRANSLATION_FAILED",)),
        _provider_component(request),
        _supervision_component(request),
    )
    if any(c.status is HealthStatus.RED for c in components):
        overall = HealthStatus.RED
    elif any(c.status is HealthStatus.YELLOW for c in components):
        overall = HealthStatus.YELLOW
    elif all(c.status is HealthStatus.GREEN for c in components):
        overall = HealthStatus.GREEN
    else:
        overall = HealthStatus.UNKNOWN
    return HealthSnapshot(
        status=overall,
        phase=settings.app_phase,
        app_version=settings.app_version,
        components=components,
    )
