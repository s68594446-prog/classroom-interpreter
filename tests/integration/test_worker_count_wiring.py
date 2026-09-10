"""Production wiring tests for worker-count configuration (repair item 3).

Why this file exists: `Settings.asr_worker_count` and
`Settings.translation_worker_count` both default to 2, and
`SessionOrchestrator.__init__` also defaults both to 2. Because the two
defaults coincide, `create_app` could omit the kwargs entirely and every
default-configuration test would still pass while configured concurrency
was silently ignored in production. These tests therefore use non-default
counts (3 ASR / 4 translation) so the configured value can only be
observed if it actually travels from Settings to the orchestrator.

`test_production_app_creates_configured_worker_counts` exercises the real
`create_app` lifespan with no injected runtime, so it is the regression
guard for `main.py`. `test_spawned_runtime_tasks_match_configured_worker_counts`
covers the second half of the path — that the counts create real leased
worker objects and live tasks rather than being stored and ignored.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from classroom_interpreter.config import Settings
from classroom_interpreter.main import create_app
from classroom_interpreter.runtime.pipeline import SessionOrchestrator
from classroom_interpreter.storage import Database

# pytest puts this file's directory on sys.path (no __init__.py here), so the
# shared deterministic fakes are importable directly.
from test_live_api import FakeASR, FakeListener, FakeTranslation

ASR_WORKERS = 3
TRANSLATION_WORKERS = 4


def build_test_settings(tmp_path: Path) -> Settings:
    """Settings with worker counts deliberately different from every default."""
    settings = Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path / "data",
        app_phase="REPAIR_ITEM3",
        asr_worker_count=ASR_WORKERS,
        translation_worker_count=TRANSLATION_WORKERS,
    )
    # Guard the guard: if the defaults ever change to match these values the
    # tests below would stop proving anything, so fail loudly instead.
    defaults = Settings.model_fields
    assert defaults["asr_worker_count"].default != ASR_WORKERS
    assert defaults["translation_worker_count"].default != TRANSLATION_WORKERS
    return settings


def test_production_app_creates_configured_worker_counts(tmp_path: Path) -> None:
    """`create_app` must hand the Settings worker counts to the orchestrator.

    Mutation that turns this red: drop either
    `asr_worker_count=`/`translation_worker_count=` kwarg from the
    `SessionOrchestrator(...)` call in `main.py`. The constructor then falls
    back to 2/2 and the assertions below see 2 instead of 3/4. No runtime is
    injected here, so the orchestrator is built by the production path.
    """
    settings = build_test_settings(tmp_path)
    database = Database(settings.database_path)
    app = create_app(settings=settings, database=database)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        assert runtime.asr_worker_count == ASR_WORKERS
        assert runtime.translation_worker_count == TRANSLATION_WORKERS
        # Placeholder providers (no key configured) still allow construction.
        response = client.get("/health")
        assert response.status_code == 200


def test_spawned_runtime_tasks_match_configured_worker_counts(tmp_path: Path) -> None:
    """`_spawn_workers` must create one live task per configured worker.

    The counts are passed from the same `Settings` object production reads,
    but explicitly, so this test isolates the spawn half of the path: it
    stays green if `main.py` regresses and red if `_spawn_workers` stops
    honouring the counts (for example by hardcoding a single worker or by
    reusing one worker id). `FakeListener` writes durable chunks on start so
    the session reaches RECORDING and the workers actually spawn.
    """
    settings = build_test_settings(tmp_path)
    database = Database(settings.database_path)

    runtime = SessionOrchestrator(
        settings,
        database,
        asr_provider=FakeASR(),
        translation_provider=FakeTranslation(),
        listener_factory=lambda root: FakeListener(database, root),
        auto_purge=False,
        asr_worker_count=settings.asr_worker_count,
        translation_worker_count=settings.translation_worker_count,
    )
    app = create_app(settings=settings, database=database, runtime=runtime)
    with TestClient(app) as client:
        client.post("/api/sessions", json={"course_name": "wiring"}).raise_for_status()
        try:
            names = sorted(task.get_name() for task in runtime._tasks)
            assert len(runtime._asr_workers) == ASR_WORKERS
            assert len(runtime._translation_workers) == TRANSLATION_WORKERS
            assert sum(1 for n in names if n.startswith("asr-loop-")) == ASR_WORKERS
            assert (
                sum(1 for n in names if n.startswith("translation-loop-"))
                == TRANSLATION_WORKERS
            )
            assert "segmenter-loop" in names
            assert "translation-sequencer-loop" in names
            # Distinct worker ids prove independent leaseholders, not N
            # references to one worker that would contend on the same lease.
            asr_ids = {worker.worker_id for worker in runtime._asr_workers}
            translation_ids = {
                worker.worker_id for worker in runtime._translation_workers
            }
            assert len(asr_ids) == ASR_WORKERS
            assert len(translation_ids) == TRANSLATION_WORKERS
        finally:
            runtime._stop.set()
            for task in runtime._tasks:
                task.cancel()
