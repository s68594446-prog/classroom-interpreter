"""Shared Phase 3 fixtures: deterministic synthetic capture chunks.

Only generated PCM enters tests; no real classroom audio.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from classroom_interpreter.audio.durable import ChunkWriter
from classroom_interpreter.storage import Database, SessionRepository

RATE = 48_000  # exact 3:1 ratio to the 16 kHz internal rate (deterministic)
TONE_HZ = 440.0
TONE_AMPLITUDE = 4000  # RMS ~2828, comfortably above the default VAD threshold


def tone_pcm(seconds: float, rate: int = RATE) -> bytes:
    count = round(seconds * rate)
    phase = np.arange(count) / rate
    return (np.sin(2 * np.pi * TONE_HZ * phase) * TONE_AMPLITUDE).astype("<i2").tobytes()


def silence_pcm(seconds: float, rate: int = RATE) -> bytes:
    return b"\x00\x00" * round(seconds * rate)


def make_session(database: Database, course_name: str = "Phase 3 test") -> str:
    repository = SessionRepository(database)
    record = repository.create_session(course_name, app_version="test")
    return record.id


def write_chunks(
    database: Database,
    chunk_dir: Path,
    session_id: str,
    blocks: list[bytes],
    *,
    epoch: int = 0,
    chunk_seconds: int = 20,
    start_ns: int = 1_000_000_000,
) -> int:
    """Persist deterministic audio blocks as fully durable capture chunks and
    return the number of sealed chunks (mirrors the Phase 2 durability path)."""
    writer = build_chunk_writer(database, chunk_dir, session_id, chunk_seconds)
    wall = "2026-09-08T00:00:00+00:00"
    sealed = 0
    ns = start_ns
    for block in blocks:
        completed = writer.append(block, ns, wall, epoch)
        sealed += len(completed)
        ns += round(len(block) // 2 / RATE * 1_000_000_000)
    if writer.seal() is not None:
        sealed += 1
    return sealed


def build_chunk_writer(
    database: Database, chunk_dir: Path, session_id: str, chunk_seconds: int = 20
) -> ChunkWriter:
    return ChunkWriter(database, chunk_dir, session_id, RATE, 1, chunk_seconds)


def envelope_blocks(envelope: list[tuple[str, float]]) -> list[bytes]:
    """Split a speech/silence envelope into ~1 s PCM blocks."""
    blocks: list[bytes] = []
    for kind, seconds in envelope:
        remaining = seconds
        while remaining > 0:
            step = min(1.0, remaining)
            blocks.append(tone_pcm(step) if kind == "speech" else silence_pcm(step))
            remaining -= step
    return blocks
