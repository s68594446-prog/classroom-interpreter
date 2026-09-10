"""Concurrent-pipeline accelerated soak (BLOCKING ISSUE 10 rewrite).

Why this file exists instead of edits to `soak_accelerated.py`
--------------------------------------------------------------
The independent review (2026-09-08) invalidated the Phase 9 soak evidence.
Reading the old script confirms every finding:

  * `build_agents()` constructed a `SegmentAgent`, but the production-phase
    `consumer_cycle()` only stepped ASR and translation. Segmentation did not
    begin until the post-production drain, so no restart could ever land while
    the segmenter was working through a backlog.
  * No `TranslationSequencer` was constructed or stepped at all, so the
    durable-staging / ordered-release architecture introduced by migration 005
    was never exercised.
  * "Restart" rebound three Python variables in the same process and the same
    event loop. No task was cancelled, no database handle was reopened, and
    `rebuild_unconfirmed_tail()` was never called.
  * `max_backlog` counted consumer loop iterations, not any durable queue
    depth, so the reported backlog number described the harness, not the system.

This harness runs the REAL production orchestration: `SessionOrchestrator.
_spawn_workers()` creates the same segmenter, ASR, translation and sequencer
loops that `create_app` runs, and the final drain is the production
`_finalize()` path. A separate producer coroutine writes durable capture
chunks concurrently, so the segmenter consumes a live backlog rather than a
finished recording.

The restart is a real one: worker tasks are stopped and cancelled and awaited,
the database handle is reopened, a new orchestrator is constructed, boundary
recovery runs, and only then are new workers spawned -- all while the producer
still has chunks left to write.

Evidence honesty
----------------
`lecture_seconds` is SYNTHETIC AUDIO DURATION, not elapsed time. A 9000 s run
is "9000 seconds of equivalent audio", never "a 2.5 hour run"; the report
carries both numbers under separate keys. This harness exercises no
microphone, no real MiMo endpoint and no real classroom content: real-hardware
and real-provider acceptance remain NOT TESTED.

Usage:
  PYTHONPATH=backend python scripts/soak_pipeline.py \
      --root /private/tmp/classroom-soak/<fresh-dir> --seconds 9000
  PYTHONPATH=backend python scripts/soak_pipeline.py \
      --root /private/tmp/classroom-soak/<fresh-dir> --seconds 300 --smoke
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from classroom_interpreter.agents.cleanup import CleanupAgent  # noqa: E402
from classroom_interpreter.audio.durable import ChunkWriter  # noqa: E402
from classroom_interpreter.config import Settings  # noqa: E402
from classroom_interpreter.domain.models import SessionStatus  # noqa: E402
from classroom_interpreter.runtime.pipeline import SessionOrchestrator  # noqa: E402
from classroom_interpreter.storage import Database  # noqa: E402
from classroom_interpreter.storage.repository import SessionRepository  # noqa: E402

from soak_accelerated import (  # noqa: E402
    RATE,
    SEED,
    SoakASR,
    SoakMetrics,
    SoakSummary,
    SoakTranslation,
    pending_count,
    silence,
    tone,
    verify_db,
    verify_post,
)

CYCLE_SECONDS = 5  # 4 s speech + 1 s silence -> exactly one segment per cycle
CYCLES_PER_CHUNK = 4  # 4 x 5 s = one 20 s capture chunk
BURST_CHUNKS = 4  # chunks written back-to-back just before the restart
WORKER_TASK_PREFIXES = (
    "segmenter-loop",
    "asr-loop-",
    "translation-loop-",
    "translation-sequencer-loop",
)


# --------------------------------------------------------------- providers


class LaggingASR(SoakASR):
    """Seeded provider matrix plus a deliberate lag.

    The lag is what lets the producer outrun the consumers so a genuine
    durable backlog exists at the restart point. Without it the pipeline
    keeps up and the restart would land on an idle system, which is the
    hole the independent review found in the old soak.
    """

    def __init__(self, rng: random.Random, latency_seconds: float):
        super().__init__(rng)
        self.latency_seconds = latency_seconds

    async def transcribe(self, segment, *, language: str = "en"):
        await asyncio.sleep(self.latency_seconds)
        return await super().transcribe(segment, language=language)


class LaggingTranslation(SoakTranslation):
    def __init__(self, rng: random.Random, latency_seconds: float):
        super().__init__(rng)
        self.latency_seconds = latency_seconds

    async def translate(self, request):
        await asyncio.sleep(self.latency_seconds)
        return await super().translate(request)


# ---------------------------------------------------------------- probes


def durable_backlog(database: Database, session_id: str) -> dict:
    """Durable queue depths read from SQLite -- never harness loop counters.

    Every number here survives a process restart, which is the point: the
    old script's `max_backlog` counted consumer iterations and proved nothing
    about the system's state.
    """
    with database.connect() as connection:
        committed_chunks = connection.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 AS n FROM capture_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        cursor_row = connection.execute(
            "SELECT next_chunk_sequence FROM pipeline_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        segments = connection.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
        staged = connection.execute(
            "SELECT COUNT(*) AS n FROM translation_staged_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        published = connection.execute(
            "SELECT COUNT(*) AS n FROM translation_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
    cursor = int(cursor_row["next_chunk_sequence"]) if cursor_row else 0
    return {
        "committed_chunks": int(committed_chunks),
        "segmenter_cursor_chunk": cursor,
        "unconsumed_chunks": max(0, int(committed_chunks) - cursor),
        "segments": int(segments),
        "pending_segments": pending_count(database, session_id),
        "staged_translations": int(staged),
        "published_translations": int(published),
    }


def sqlite_integrity(database: Database) -> str:
    with database.connect() as connection:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]


def verify_hashes_and_residue(database: Database, session_id: str) -> dict:
    """Source-hash / context-hash validity and staging residue."""
    with database.connect() as connection:
        # `asr_results` stores the English text, not its digest, so the bind
        # between a translation and the exact ASR text it was derived from is
        # recomputed here rather than compared column-to-column.
        pairs = connection.execute(
            """
            SELECT a.english_text AS english_text,
                   t.english_text_sha256 AS claimed_sha256
            FROM translation_results t
            JOIN asr_results a ON a.segment_id = t.segment_id
            WHERE t.session_id = ?
            """,
            (session_id,),
        ).fetchall()
        bad_source_hash = sum(
            1
            for row in pairs
            if hashlib.sha256(row["english_text"].encode("utf-8")).hexdigest()
            != row["claimed_sha256"]
        )
        translations_checked = len(pairs)
        missing_context = connection.execute(
            """
            SELECT COUNT(*) AS n FROM segments s
            WHERE s.session_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM translation_context_snapshots c
                WHERE c.session_id = s.session_id
                  AND c.segment_sequence = s.sequence
              )
            """,
            (session_id,),
        ).fetchone()["n"]
        staged_residue = connection.execute(
            "SELECT COUNT(*) AS n FROM translation_staged_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        bad_context_hash = connection.execute(
            """
            SELECT COUNT(*) AS n FROM translation_context_snapshots
            WHERE session_id = ?
              AND (context_sha256 IS NULL OR LENGTH(context_sha256) != 64)
            """,
            (session_id,),
        ).fetchone()["n"]
    return {
        "translations_source_hash_checked": int(translations_checked),
        "translation_source_hash_mismatches": int(bad_source_hash),
        "segments_without_context_snapshot": int(missing_context),
        "staged_translation_residue": int(staged_residue),
        "invalid_context_hashes": int(bad_context_hash),
    }


# ------------------------------------------------------------------- run


def make_settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        classroom_interpreter_data_dir=root,
        app_phase="SOAK_PIPELINE",
        asr_worker_count=3,
        translation_worker_count=3,
        segment_min_seconds=2.0,
        segment_max_seconds=30.0,
        segment_silence_milliseconds=700,
        segment_overlap_milliseconds=250,
        vad_threshold_rms=300.0,
        asr_backoff_base_seconds=0.001,
        asr_backoff_cap_seconds=0.02,
        translation_backoff_base_seconds=0.001,
        translation_backoff_cap_seconds=0.02,
    )


def make_runtime(settings, database, asr, translation, summary) -> SessionOrchestrator:
    return SessionOrchestrator(
        settings,
        database,
        asr_provider=asr,
        translation_provider=translation,
        summary_provider=summary,
        auto_purge=False,
        asr_worker_count=settings.asr_worker_count,
        translation_worker_count=settings.translation_worker_count,
    )


async def stop_workers(runtime: SessionOrchestrator) -> int:
    """A real stop, through the production path.

    `stop_workers_for_finalization` is the same method `end_session` uses: it
    signals, cancels and awaits every worker task, then releases the leases
    that workers cancelled mid-provider-call died holding. Using it here keeps
    the soak on production code instead of a harness-local imitation.
    """
    return await runtime.stop_workers_for_finalization()


async def run(
    root: Path, lecture_seconds: int, *, smoke: bool, producer_interval: float
) -> dict:
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"refusing to reuse a non-empty soak root: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)

    settings = make_settings(root)
    database_path = settings.database_path
    database = Database(database_path)
    migration_version = database.migrate()
    repository = SessionRepository(database)

    session_id = str(uuid.uuid4())
    repository.create_session(
        "Soak Pipeline", app_version="soak-pipeline", session_id=session_id
    )
    repository.transition_session(
        session_id, SessionStatus.RECORDING, reason_code="SOAK_START", actor="soak"
    )

    sessions_root = settings.classroom_interpreter_data_dir / "sessions"
    summaries_root = settings.classroom_interpreter_data_dir / "summaries"
    chunk_dir = sessions_root / session_id / "capture" / session_id

    metrics = SoakMetrics(root / "metrics.jsonl")
    fd_baseline = metrics.process.num_fds()

    expected_segments = lecture_seconds // CYCLE_SECONDS
    n_chunks = lecture_seconds // (CYCLE_SECONDS * CYCLES_PER_CHUNK)
    if n_chunks < 5:
        raise SystemExit("need at least 5 chunks for a meaningful restart point")

    block_speech = tone(4.0)
    block_silence = silence(1.0)

    latency = 0.02 if smoke else 0.012
    asr_provider = LaggingASR(random.Random(SEED + 1), latency)
    translation_provider = LaggingTranslation(random.Random(SEED + 2), latency)
    summary_provider = SoakSummary()

    runtime = make_runtime(
        settings, database, asr_provider, translation_provider, summary_provider
    )
    runtime.active_session = session_id
    runtime._spawn_workers()

    restart_at = n_chunks * 4 // 10
    written_chunks = 0
    observations: list[dict] = []
    high_water = {
        "unconsumed_chunks": 0,
        "pending_segments": 0,
        "staged_translations": 0,
    }
    segmenter_progress_during_production = False
    pre_restart: dict | None = None
    restart_evidence: dict | None = None
    post_restart: dict | None = None
    restarts = 0
    leases_released_at_restart = 0

    async def write_chunk(index: int, db: Database) -> None:
        writer = ChunkWriter(db, chunk_dir, session_id, RATE, 1, 20)
        ns = 1_000_000_000 * index * 20
        wall = "2026-09-10T00:00:00+00:00"
        for _ in range(CYCLES_PER_CHUNK):
            writer.append(block_speech, ns, wall, 0)
            ns += 4 * 10**9
            writer.append(block_silence, ns, wall, 0)
            ns += 1 * 10**9

    for index in range(n_chunks):
        await write_chunk(index, database)
        written_chunks += 1
        # Pace the producer so the concurrently running production loops get
        # real time on the event loop: the pipeline must be actively draining
        # a backlog at the restart point, not idle and not hopelessly behind.
        # The chunks immediately before the restart are written as a burst, so
        # the restart lands with earlier segments already DONE *and* a real
        # unconsumed backlog behind them -- the two conditions that make the
        # boundary rebuild meaningful (it must keep the DONE prefix and
        # re-derive the rest). A steady rate alone gives one or the other
        # depending on timing luck, which is how the old soak proved nothing.
        # The restart chunk itself is part of the burst: pausing after writing
        # it would let a single segmenter tick (which drains every available
        # chunk in one call) clear the backlog before it is measured.
        bursting = restart_at - BURST_CHUNKS <= index <= restart_at
        await asyncio.sleep(0.0 if bursting else producer_interval)

        snapshot = durable_backlog(database, session_id)
        for key in high_water:
            high_water[key] = max(high_water[key], snapshot[key])
        if snapshot["segmenter_cursor_chunk"] > 0 or snapshot["segments"] > 0:
            segmenter_progress_during_production = True

        if index % 10 == 0:
            metrics.sample(phase="produce", chunks=written_chunks, **snapshot)
            observations.append({"chunk_index": index, **snapshot})

        if index == restart_at:
            # ---- Requirement 4: assert a real, durable backlog exists here.
            pre_restart = durable_backlog(database, session_id)
            pre_restart["producer_finished"] = False
            pre_restart["chunks_remaining"] = n_chunks - written_chunks
            failures = []
            if pre_restart["committed_chunks"] <= 0:
                failures.append("no committed chunks")
            if pre_restart["segmenter_cursor_chunk"] <= 0:
                failures.append("segmenter cursor never advanced")
            if pre_restart["segments"] <= 0:
                failures.append("no segments produced during production")
            if pre_restart["chunks_remaining"] <= 0:
                failures.append("producer already finished")
            quantified = (
                pre_restart["unconsumed_chunks"]
                + pre_restart["pending_segments"]
                + pre_restart["staged_translations"]
            )
            if quantified <= 0:
                failures.append("no durable backlog at the restart point")
            if not segmenter_progress_during_production:
                failures.append("segmenter never executed during production")
            if pre_restart["published_translations"] <= 0:
                # Without a completed prefix the rebuild has nothing to retain,
                # so the restart would not exercise DONE-prefix preservation.
                failures.append("no segment reached DONE before the restart")
            pre_restart["assertion_failures"] = failures
            if failures:
                metrics.close()
                raise SystemExit(
                    "restart preconditions not met (the soak would prove "
                    f"nothing): {failures}"
                )

            # ---- Requirement 6: a real restart, not a variable rebind.
            stopped_runtime = runtime
            leases_released_at_restart = await stop_workers(runtime)

            # Prove the stop actually happened. Without this the harness
            # cannot tell a real restart from rebinding three variables while
            # the original workers keep running -- exactly the hole the
            # independent review found in the old soak.
            still_running = sorted(
                task.get_name()
                for task in asyncio.all_tasks()
                if not task.done()
                and task.get_name().startswith(WORKER_TASK_PREFIXES)
            )
            unfinished = [
                task.get_name()
                for task in stopped_runtime._tasks
                if not task.done()
            ]
            if still_running or unfinished:
                metrics.close()
                raise SystemExit(
                    "restart did not stop the previous workers, so the run "
                    f"would prove nothing: live={still_running} "
                    f"unawaited={unfinished}"
                )

            database = Database(database_path)  # reopened handle
            database.migrate()  # migration idempotency across restart
            runtime = make_runtime(
                settings,
                database,
                asr_provider,
                translation_provider,
                summary_provider,
            )
            runtime.active_session = session_id
            runtime._build_workers(session_id)
            # ---- Requirement 7: boundary rebuild on recovery.
            restart_evidence = runtime._segmenter.rebuild_unconfirmed_tail()
            runtime._spawn_workers()
            restarts += 1
            post_restart = durable_backlog(database, session_id)
            metrics.sample(phase="restart", **post_restart)

            # Prove the boundary rebuild actually ran and did what it claims.
            # A skipped rebuild leaves the pre-restart segments in place, so
            # tying the retained count to the observed row count is what
            # separates real recovery evidence from a number that was merely
            # reported.
            rebuild_failures = []
            required_keys = {
                "kept_done_segments",
                "removed_unconfirmed_segments",
                "restart_boundary_ms",
            }
            if not required_keys.issubset(restart_evidence):
                rebuild_failures.append(
                    f"rebuild evidence missing keys: {restart_evidence}"
                )
            else:
                kept = restart_evidence["kept_done_segments"]
                removed = restart_evidence["removed_unconfirmed_segments"]
                if post_restart["segments"] != kept:
                    rebuild_failures.append(
                        f"rows after rebuild ({post_restart['segments']}) do not "
                        f"match the retained DONE prefix ({kept}): the "
                        "unconfirmed tail was not discarded"
                    )
                if kept + removed != pre_restart["segments"]:
                    rebuild_failures.append(
                        f"kept {kept} + removed {removed} != "
                        f"{pre_restart['segments']} segments before restart"
                    )
                if kept <= 0:
                    rebuild_failures.append(
                        "no DONE prefix retained, so retention was not exercised"
                    )
                if removed <= 0:
                    rebuild_failures.append(
                        "no unconfirmed tail re-derived, so the rebuild was a no-op"
                    )
            if rebuild_failures:
                metrics.close()
                raise SystemExit(f"boundary rebuild not proven: {rebuild_failures}")

    producer_finished = time.monotonic()

    # ---- Requirement 10: drain through the production finalization path.
    leases_released_at_stop = await stop_workers(runtime)
    repository.transition_session(
        session_id, SessionStatus.FINALIZING, reason_code="SOAK_END", actor="soak"
    )
    await runtime._finalize(session_id)

    record = repository.get_session(session_id)
    summary_path = Path(record.summary_path) if record.summary_path else None
    if record.status is not SessionStatus.SUMMARY_SAVED or summary_path is None:
        metrics.sample(phase="failed", status=str(record.status))
        metrics.close()
        return {
            "pass": False,
            "failure": "summary was not saved; purge gate correctly stayed closed",
            "session_status": str(record.status),
            "finalization_error": runtime.last_finalization_error,
            "backlog_at_failure": durable_backlog(database, session_id),
        }

    pre_purge = verify_db(database, session_id, expected_segments)
    hashes = verify_hashes_and_residue(database, session_id)
    integrity = sqlite_integrity(database)

    cleanup = CleanupAgent(
        database,
        summaries_root=summaries_root,
        session_store_root=sessions_root,
    )
    receipt = cleanup.purge(session_id)
    post = verify_post(database, session_id, summary_path, receipt)

    elapsed = time.monotonic() - started
    metrics.sample(phase="final")
    final_rss = metrics.process.memory_info().rss / 1048576
    fds_final = metrics.process.num_fds()
    metrics.close()

    passed = (
        pre_purge["sequences_contiguous"]
        and pre_purge["all_done"]
        and pre_purge["expected_segments_match"]
        and pre_purge["translation_ordered"]
        and pre_purge["duplicate_asr_commits"] == 0
        and pre_purge["duplicate_translation_commits"] == 0
        and hashes["translation_source_hash_mismatches"] == 0
        and hashes["staged_translation_residue"] == 0
        and hashes["invalid_context_hashes"] == 0
        and integrity == "ok"
        and restarts == 1
        and segmenter_progress_during_production
        and post["purge_outcome"] == "PURGED"
        and post["summary_saved"]
        and post["summary_sha256_match"]
        and post["session_row_removed"]
    )

    return {
        "harness": "soak_pipeline",
        "evidence_class": "accelerated_synthetic_audio",
        "evidence_caveat": (
            "synthetic audio duration, not wall-clock runtime; no microphone, "
            "no real MiMo provider, no real classroom content"
        ),
        "seed": SEED,
        "smoke": smoke,
        "producer_interval_seconds": producer_interval,
        "burst_chunks_before_restart": BURST_CHUNKS,
        "synthetic_audio_seconds": lecture_seconds,
        "wall_clock_seconds": round(elapsed, 2),
        "production_phase_wall_seconds": round(producer_finished - started, 2),
        "migration_version": migration_version,
        "asr_worker_count": settings.asr_worker_count,
        "translation_worker_count": settings.translation_worker_count,
        "expected_segments": expected_segments,
        "chunks_written": written_chunks,
        "restarts": restarts,
        "stale_leases_released_at_restart": leases_released_at_restart,
        "stale_leases_released_at_stop": leases_released_at_stop,
        "segmenter_ran_during_production": segmenter_progress_during_production,
        "pre_restart": pre_restart,
        "restart_rebuild_evidence": restart_evidence,
        "post_restart": post_restart,
        "durable_backlog_high_water": high_water,
        "observations": observations,
        "asr_calls": asr_provider.calls,
        "asr_injected_faults": asr_provider.errors,
        "translation_calls": translation_provider.calls,
        "translation_injected_faults": translation_provider.errors,
        "sqlite_integrity": integrity,
        "initial_rss_mb": round(metrics.initial_rss, 2),
        "peak_rss_mb": round(metrics.peak_rss, 2),
        "final_rss_mb": round(final_rss, 2),
        "fds_baseline": fd_baseline,
        "fds_final": fds_final,
        "metric_samples": metrics.samples,
        "db_bytes_after_purge": database_path.stat().st_size,
        "started_wall": started_wall,
        "finished_wall": datetime.now(timezone.utc).isoformat(),
        **pre_purge,
        **hashes,
        **post,
        "pass": passed,
    }


def environment() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except Exception:
        commit = "unknown"
    return {
        "commit": commit,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "command": " ".join(sys.argv),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=9000)
    parser.add_argument(
        "--producer-interval",
        type=float,
        default=0.4,
        help="seconds between capture chunks; paces producer against consumers",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="short harness-correctness run; still asserts every invariant",
    )
    args = parser.parse_args()

    report = asyncio.run(
        run(
            args.root,
            args.seconds,
            smoke=args.smoke,
            producer_interval=args.producer_interval,
        )
    )
    report["environment"] = environment()

    report_path = args.root / "soak_pipeline_report.json"
    with report_path.open("w") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report["report_sha256"] = digest
    print(json.dumps(report, indent=1, sort_keys=True))
    print(f"\nreport: {report_path}\nreport_sha256: {digest}", file=sys.stderr)
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
