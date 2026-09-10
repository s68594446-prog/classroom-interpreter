from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from classroom_interpreter.api import router
from classroom_interpreter.config import Settings, get_settings
from classroom_interpreter.logging import MetadataLogEvent, configure_logging, log_metadata
from classroom_interpreter.providers.mimo import (
    MimoASRProvider,
    MimoSummaryProvider,
    MimoTranslationProvider,
)
from classroom_interpreter.runtime.pipeline import SessionOrchestrator, bind_database
from classroom_interpreter.storage import Database


def _build_providers(settings: Settings):
    """Real MiMo adapters only when a key is configured. No silent mock: a
    missing key leaves providers unset and Start Class is refused with an
    actionable readiness error."""
    if not settings.mimo_api_key:
        return None, None, None
    return (
        MimoASRProvider(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
            model=settings.mimo_asr_model,
            language=settings.mimo_asr_language,
        ),
        MimoTranslationProvider(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
            model=settings.mimo_translation_model,
        ),
        MimoSummaryProvider(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
            model=settings.mimo_summary_model,
        ),
    )


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    runtime: SessionOrchestrator | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_database = database or Database(resolved_settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        migration_version = resolved_database.migrate()
        app.state.settings = resolved_settings
        app.state.database = resolved_database
        if runtime is None:
            asr_provider, translation_provider, summary_provider = _build_providers(
                resolved_settings
            )
            app.state.runtime = SessionOrchestrator(
                resolved_settings,
                resolved_database,
                asr_provider=asr_provider,
                translation_provider=translation_provider,
                summary_provider=summary_provider,
                # Configured concurrency must reach the orchestrator; the
                # constructor defaults happen to match the Settings defaults,
                # so omitting these silently pinned production to 2/2 (D-034).
                asr_worker_count=resolved_settings.asr_worker_count,
                translation_worker_count=resolved_settings.translation_worker_count,
            )
        else:
            app.state.runtime = runtime
        # Restart reconciliation: orphaned capture sessions become visible
        # recovery choices instead of silent zombies (F-013).
        moved = app.state.runtime.reconcile_on_startup()
        log_metadata(
            logging.getLogger("classroom_interpreter"),
            MetadataLogEvent(
                agent="application",
                event="startup",
                status="ready",
                schema_version=migration_version,
            ),
        )
        if moved:
            log_metadata(
                logging.getLogger("classroom_interpreter"),
                MetadataLogEvent(
                    agent="recovery",
                    event="restart_reconciled",
                    status=f"reconciled_{moved}",
                ),
            )
        health_stop = asyncio.Event()
        health_task = asyncio.create_task(
            app.state.runtime.health_monitor.run(health_stop),
            name="health-monitor",
        )
        try:
            yield
        finally:
            health_stop.set()
            await health_task

    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    if runtime is not None:
        app.state.runtime = runtime
    app.include_router(router)
    bind_database(resolved_database)

    @app.get("/api/app-info")
    def app_info() -> dict[str, str]:
        return {
            "name": resolved_settings.app_name,
            "phase": resolved_settings.app_phase,
            "status": "runtime",
        }

    dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "classroom_interpreter.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
