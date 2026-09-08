"""Accelerated three-hour-equivalent soak (E5, TEST_PLAN).

Runs the REAL runtime agents (SegmentAgent, TranscriptionAgent, TranslationAgent,
SummaryAgent, CleanupAgent) against a synthetic 9,000 s lecture with seeded
provider errors (500 / 429+Retry-After / timeout / 503), latency spikes, a
mid-soak restart (durable cursor reload), and interleaved writes that build
consumer backlog. Unattended; produces a content-free report JSON.

Usage:
  PYTHONPATH=backend python scripts/soak_accelerated.py \
    --root /private/tmp/classroom-interpreter-overnight/phase9-soak
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psutil  # noqa: E402

from classroom_interpreter.agents.cleanup import CleanupAgent  # noqa: E402
from classroom_interpreter.agents.segmentation import (  # noqa: E402
    SegmentAgent,
    SegmenterConfig,
)
from classroom_interpreter.agents.summary import SummaryAgent  # noqa: E402
from classroom_interpreter.agents.transcription import TranscriptionAgent  # noqa: E402
from classroom_interpreter.agents.translation import TranslationAgent  # noqa: E402
from classroom_interpreter.audio.durable import ChunkWriter  # noqa: E402
from classroom_interpreter.domain.models import (  # noqa: E402
    ProviderError,
    ProviderErrorCategory,
    TranscriptResult,
    TranslationResult,
    utc_now,
)
from classroom_interpreter.storage import Database  # noqa: E402
from classroom_interpreter.storage.repository import SessionRepository  # noqa: E402

RATE = 48_000
SEED = 20260908


def tone(seconds: float) -> bytes:
    count = round(seconds * RATE)
    out = bytearray(count * 2)
    for i in range(count):
        value = int(4000 * math.sin(2 * math.pi * 440 * (i / RATE)))
        out[2 * i] = value & 0xFF
        out[2 * i + 1] = (value >> 8) & 0xFF
    return bytes(out)


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * round(seconds * RATE)


class SoakASR:
    """Seeded error/latency matrix mock. Content-free outputs."""

    name = "soak-mock"
    model = "soak-asr-v1"

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.calls = 0
        self.errors = {"SERVER": 0, "RATE_LIMIT": 0, "TIMEOUT": 0}

    async def transcribe(self, segment, *, language: str = "en"):
        self.calls += 1
        roll = self.rng.random()
        await asyncio.sleep(self.rng.uniform(0.001, 0.006))
        if roll < 0.02:
            self.errors["SERVER"] += 1
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.SERVER, retryable=True,
                              http_status=500, safe_message="soak injected 500"))
        if roll < 0.035:
            self.errors["RATE_LIMIT"] += 1
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.RATE_LIMIT, retryable=True,
                              http_status=429, retry_after_ms=5,
                              safe_message="soak injected 429"))
        if roll < 0.045:
            self.errors["TIMEOUT"] += 1
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.TIMEOUT, retryable=True,
                              safe_message="soak injected timeout"))
        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=(
                f"Segment {segment.sequence}: the VaR at 1.96 sigma is 2.5%, "
                f"Black-Scholes delta 0.63, expiry 2027-06-30."
            ),
            provider=self.name,
            model=self.model,
            latency_ms=1,
            audio_seconds=segment.duration_ms / 1000,
            created_at=utc_now(),
        )


class SoakTranslation:
    name = "soak-mock"
    model = "soak-tr-v1"

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.calls = 0
        self.errors = {"SERVER": 0, "TIMEOUT": 0}

    async def translate(self, request):
        self.calls += 1
        roll = self.rng.random()
        await asyncio.sleep(self.rng.uniform(0.001, 0.005))
        if roll < 0.015:
            self.errors["SERVER"] += 1
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.SERVER, retryable=True,
                              http_status=503, safe_message="soak injected 503"))
        if roll < 0.025:
            self.errors["TIMEOUT"] += 1
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.TIMEOUT, retryable=True,
                              safe_message="soak injected timeout"))
        return TranslationResult(
            segment_id=request.segment_id,
            chinese_text=f"片段 {request.sequence}：1.96 个标准差下 VaR 为 2.5%。",
            english_text_sha256=hashlib.sha256(
                request.current_english_text.encode()
            ).hexdigest(),
            provider=self.name,
            model=self.model,
            latency_ms=1,
            created_at=utc_now(),
        )


class SoakSummary:
    name = "soak-mock"
    model = "soak-summary-v1"

    async def summarize_chunk(self, *, chunk_index: int, transcript_material: str) -> str:
        await asyncio.sleep(0.002)
        return f"片段 {chunk_index + 1} 的中文要点：涵盖 VaR、Black-Scholes 与到期日 2027-06-30。"

    async def synthesize(self, *, course_name, date, duration_text, chunk_summaries):
        from classroom_interpreter.providers.summary_contract import REQUIRED_SECTIONS

        sections = "\n\n".join(
            f"## {i + 1}. {title}\n- 覆盖要点：VaR 2.5%、Black-Scholes、到期日 2027-06-30。"
            for i, title in enumerate(REQUIRED_SECTIONS)
        )
        return (
            f"# 课程名称：{course_name}\n\n日期：{date}\n\n"
            f"课程时长：{duration_text}\n\n{sections}"
        )


from classroom_interpreter.providers.errors import ProviderFailure  # noqa: E402


class SoakMetrics:
    def __init__(self, path: Path):
        self.process = psutil.Process()
        self.path = path
        self.samples = 0
        self.peak_rss = 0.0
        self.initial_rss = self.process.memory_info().rss / 1048576
        self.handle = path.open("x")

    def sample(self, **extra):
        rss = self.process.memory_info().rss / 1048576
        self.peak_rss = max(self.peak_rss, rss)
        record = {
            "ts": round(time.monotonic(), 3),
            "rss_mb": round(rss, 2),
            "open_fds": self.process.num_fds(),
            **extra,
        }
        self.handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.handle.flush()
        self.samples += 1

    def close(self):
        self.handle.close()


def pending_count(database: Database, session_id: str) -> int:
    with database.connect() as connection:
        return connection.execute(
            """
            SELECT COUNT(*) AS n FROM segments WHERE session_id = ? AND state IN (
              'ASR_PENDING','ASR_PROCESSING','ASR_RETRY',
              'TRANSLATION_PENDING','TRANSLATION_PROCESSING','TRANSLATION_RETRY'
            )
            """,
            (session_id,),
        ).fetchone()["n"]


def verify_db(database: Database, session_id: str, expected_segments: int) -> dict:
    with database.connect() as connection:
        seg_rows = connection.execute(
            "SELECT sequence, state FROM segments WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        sequences = [r["sequence"] for r in seg_rows]
        states = {r["state"] for r in seg_rows}
        tr_sequences = [
            r["segment_sequence"]
            for r in connection.execute(
                """
                SELECT t.segment_sequence FROM translation_results t
                WHERE t.session_id = ? ORDER BY t.segment_sequence
                """,
                (session_id,),
            ).fetchall()
        ]
        dup_asr = connection.execute(
            """SELECT COUNT(*) AS n FROM (
                 SELECT segment_id FROM asr_results GROUP BY segment_id HAVING COUNT(*) > 1)"""
        ).fetchone()["n"]
        dup_tr = connection.execute(
            """SELECT COUNT(*) AS n FROM (
                 SELECT segment_id FROM translation_results GROUP BY segment_id HAVING COUNT(*) > 1)"""
        ).fetchone()["n"]
    return {
        "segments_total": len(sequences),
        "sequences_contiguous": sequences == list(range(len(sequences))),
        "all_done": bool(sequences) and states == {"DONE"},
        "expected_segments_match": len(sequences) == expected_segments,
        "translation_ordered": tr_sequences == list(range(len(tr_sequences))),
        "duplicate_asr_commits": dup_asr,
        "duplicate_translation_commits": dup_tr,
    }


def verify_post(database: Database, session_id: str, summary_path: Path,
                receipt: dict) -> dict:
    with database.connect() as connection:
        metadata = connection.execute(
            "SELECT * FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        session_gone = connection.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["n"] == 0
    summary_intact = summary_path.is_file()
    return {
        "purge_outcome": receipt.get("outcome"),
        "summary_saved": summary_intact,
        "summary_sha256_match": metadata is not None and summary_intact
        and metadata["summary_hash"] == hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "session_row_removed": session_gone,
    }


async def run(root: Path, lecture_seconds: int) -> dict:
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    rng = random.Random(SEED)
    rng_asr = random.Random(SEED + 1)
    rng_tr = random.Random(SEED + 2)

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = Database(root / "runtime.sqlite3")
    database.migrate()
    repository = SessionRepository(database)
    session_id = str(uuid.uuid4())
    repository.create_session("Soak Accelerated", app_version="soak", session_id=session_id)
    from classroom_interpreter.domain.models import SessionStatus

    repository.transition_session(
        session_id, SessionStatus.RECORDING, reason_code="SOAK_START", actor="soak"
    )

    session_store = root / "sessions" / session_id
    chunk_dir = session_store / "capture" / session_id
    summaries_root = root / "summaries"

    metrics = SoakMetrics(root / "metrics.jsonl")
    fd_baseline = metrics.process.num_fds()

    # 5 s envelope: 4 s speech + 1 s silence -> exactly one segment per 5 s.
    cycle = 5
    expected_segments = lecture_seconds // cycle
    block_speech = tone(4.0)
    block_silence = silence(1.0)
    cycles_per_chunk = 4  # 4 x 5 s = 20 s chunk
    n_chunks = lecture_seconds // (cycle * cycles_per_chunk)

    soak_asr = SoakASR(rng_asr)
    soak_translation = SoakTranslation(rng_tr)

    def build_agents():
        segmenter = SegmentAgent(
            database, session_store, session_id,
            SegmenterConfig(min_seconds=2.0, max_seconds=30.0,
                            silence_milliseconds=700, overlap_milliseconds=250,
                            threshold_rms=300.0),
        )
        asr = TranscriptionAgent(
            database, soak_asr, worker_id="soak-asr",
            max_attempts=8, backoff_base_seconds=0.001, backoff_cap_seconds=0.02,
            jitter_fraction=0.0,
        )
        translation = TranslationAgent(
            database, soak_translation, worker_id="soak-tr",
            max_attempts=8, backoff_base_seconds=0.001, backoff_cap_seconds=0.02,
            jitter_fraction=0.0,
        )
        return segmenter, asr, translation

    segmenter, asr, translation = build_agents()
    restarts = 0
    written_chunks = 0
    max_backlog = 0

    async def consumer_cycle(max_steps=8):
        nonlocal max_backlog
        steps = 0
        while steps < max_steps:
            progressed = await asr.step()
            progressed = await translation.step() or progressed
            if not progressed:
                break
            steps += 1
        max_backlog = max(max_backlog, steps)

    for index in range(n_chunks):
        writer = ChunkWriter(database, chunk_dir, session_id, RATE, 1, 20)
        base_ns = 1_000_000_000 * index * 20
        wall = "2026-09-08T00:00:00+00:00"
        ns = base_ns
        for _ in range(cycles_per_chunk):
            writer.append(block_speech, ns, wall, 0)
            ns += int(4.0 * RATE) * 10**9 // RATE
            writer.append(block_silence, ns, wall, 0)
            ns += int(1.0 * RATE) * 10**9 // RATE
        written_chunks += 1
        if index % 5 == 0:
            await consumer_cycle()
        if index % 10 == 0:
            metrics.sample(chunks=written_chunks, pending=pending_count(database, session_id))
        if index == n_chunks * 4 // 10:
            # Simulated backend restart: durable cursor reload (F-013).
            segmenter, asr, translation = build_agents()
            restarts += 1

    # Drain everything (long-speech windows, retry backoff, restart reloads).
    drain_deadline = time.monotonic() + 900
    while time.monotonic() < drain_deadline:
        segmenter.process_available_chunks()
        if await asr.step():
            continue
        if await translation.step():
            continue
        segmenter.seal_tail()
        if pending_count(database, session_id) == 0:
            if segmenter.process_available_chunks() == () and not pending_count(database, session_id):
                segmenter.seal_tail()
                break
        await asyncio.sleep(0.01)

    from classroom_interpreter.domain.models import SessionStatus

    repository.transition_session(
        session_id, SessionStatus.FINALIZING, reason_code="SOAK_END", actor="soak"
    )
    repository.transition_session(
        session_id, SessionStatus.SUMMARIZING, reason_code="SOAK_DRAIN", actor="soak"
    )
    summary_agent = SummaryAgent(
        database, SoakSummary(), summaries_root=summaries_root,
        session_store_root=root / "sessions", chunk_token_budget=3000,
    )
    summary_path = await summary_agent.summarize_async(session_id)

    cleanup = CleanupAgent(database, summaries_root=summaries_root,
                           session_store_root=root / "sessions")
    pre = verify_db(database, session_id, expected_segments)
    receipt = cleanup.purge(session_id)
    post = verify_post(database, session_id, summary_path, receipt)

    elapsed = time.monotonic() - started
    metrics.sample(final=True)
    final_rss = metrics.process.memory_info().rss / 1048576
    fds_final = metrics.process.num_fds()
    metrics.close()

    return {
        "evidence": "accelerated_synthetic",
        "seed": SEED,
        "lecture_seconds": lecture_seconds,
        "expected_segments": expected_segments,
        "chunks_written": written_chunks,
        "restarts": restarts,
        "max_consumer_backlog": max_backlog,
        "asr_calls": soak_asr.calls,
        "asr_errors": soak_asr.errors,
        "translation_calls": soak_translation.calls,
        "translation_errors": soak_translation.errors,
        "elapsed_wall_seconds": round(elapsed, 2),
        "initial_rss_mb": round(metrics.initial_rss, 2),
        "peak_rss_mb": round(metrics.peak_rss, 2),
        "final_rss_mb": round(final_rss, 2),
        "fds_baseline": fd_baseline,
        "fds_final": fds_final,
        "metric_samples": metrics.samples,
        "db_bytes_after_purge": (root / "runtime.sqlite3").stat().st_size,
        "started_wall": started_wall,
        **pre,
        **post,
        "pass": (
            pre["sequences_contiguous"]
            and pre["all_done"]
            and pre["expected_segments_match"]
            and pre["translation_ordered"]
            and pre["duplicate_asr_commits"] == 0
            and pre["duplicate_translation_commits"] == 0
            and post["purge_outcome"] == "PURGED"
            and post["summary_saved"]
            and post["summary_sha256_match"]
            and post["session_row_removed"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=9000)
    args = parser.parse_args()
    report = asyncio.run(run(args.root, args.seconds))
    report_path = args.root / "soak_report.json"
    with report_path.open("w") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
