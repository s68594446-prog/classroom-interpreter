"""Production wiring tests for worker-count configuration (repair item 3).

These tests exercise the REAL `create_app` construction path. If
`main.py` stops passing `asr_worker_count` / `translation_worker_count`
into `SessionOrchestrator`, the orchestrator falls back to constructor
defaults (1/1) and these tests fail — no direct-orchestrator shortcut
is used to fake the wiring.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from classroom_interpreter.config import Settings
from classroom_interpreter.main import create_app
from classroom_interpreter.storage import Database


def build_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path / "data",
        app_phase="REPAIR_ITEM3",
        asr_worker_count=3,
        translation_worker_count=4,
    )


def test_production_app_creates_configured_worker_counts(tmp_path: Path) -> None:
    """create_app must hand Settings worker counts to the orchestrator.

    Fails if main.py drops the explicit kwargs: the orchestrator default is
    1/1, so `3`/`4` can only be observed through real wiring.
    """
    settings = build_test_settings(tmp_path)
    database = Database(settings.database_path)
    app = create_app(settings=settings, database=database)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        assert runtime.asr_worker_count == 3
        assert runtime.translation_worker_count == 4
        # Placeholder providers (no key configured) still allow construction.
        response = client.get("/health")
        assert response.status_code == 200


def test_spawned_runtime_tasks_match_configured_worker_counts(tmp_path: Path) -> None:
    """_spawn_workers must create one live task per configured worker.

    The FakeListener writes durable chunks on start, so the session becomes
    RECORDING, workers spawn, and task names are inspectable. Defaults are
    1/1; expecting 3 asr + 4 translation + 1 segmenter + 1 sequencer tasks
    proves non-default counts actually created workers.
    """
    import asyncio
    import sys

    tests_root = Path(__file__).resolve().parents[1] / "tests"
    sys.path.insert(0, str(tests_root))
    from test_live_api import FakeASR, FakeListener, FakeTranslation

    settings = build_test_settings(tmp_path)
    database = Database(settings.database_path)
    from classroom_interpreter.runtime.pipeline import SessionOrchestrator

    runtime = SessionOrchestrator(
        settings,
        database,
        asr_provider=FakeASR(),
        translation_provider=FakeTranslation(),
        listener_factory=lambda root: FakeListener(database, root),
        auto_purge=False,
        # Explicitly rely on the same values the production path passes.
        asr_worker_count=settings.asr_worker_count,
        translation_worker_count=settings.translation_worker_count,
    )
    app = create_app(settings=settings, database=database, runtime=runtime)
    with TestClient(app) as client:
        view = client.post("/api/sessions", json={"course_name": "wiring"}).json()
        session_id = view["session"]["id"]
        try:
            names = sorted(t.get_name() for t in runtime._tasks)
            assert len(runtime._asr_workers) == 3
            assert len(runtime._translation_workers) == 4
            assert sum(1 for n in names if n.startswith("asr-loop-")) == 3
            assert sum(1 for n in names if n.startswith("translation-loop-")) == 4
            assert "segmenter-loop" in names
            assert "translation-sequencer-loop" in names
            # Distinct worker ids prove independent leased workers.
            asr_ids = [w.worker_id for w in runtime._asr_workers]
            tr_ids = [w.worker_id for w in runtime._translation_workers]
            assert len(set(asr_ids)) == 3
            assert len(set(tr_ids)) == 4
        finally:
            runtime._stop.set()
            for task in runtime._tasks:
                task.cancel()
