from __future__ import annotations

import os
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from classroom_interpreter.audio.durable import checksum, sync_directory
from classroom_interpreter.audio.normalize import Normalizer
from classroom_interpreter.domain.models import SegmentState
from classroom_interpreter.storage import Database

NORMALIZED_RATE = 16_000
FRAME_SAMPLES = 160  # 10 ms frames at 16 kHz
SAMPLE_BYTES = 2
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_BYTES
FRAME_MS = FRAME_SAMPLES * 1000 // NORMALIZED_RATE  # 10
BYTES_PER_MS = NORMALIZED_RATE // 1000 * SAMPLE_BYTES  # 32


class CorruptCaptureChunk(RuntimeError):
    """A sealed capture chunk failed re-verification during segmentation."""


@dataclass(frozen=True, slots=True)
class SegmenterConfig:
    min_seconds: float = 2.0
    max_seconds: float = 30.0
    silence_milliseconds: int = 700
    overlap_milliseconds: int = 250
    threshold_rms: float = 300.0


@dataclass(slots=True)
class SegmenterState:
    """Exact resume state, checkpointed with the durable cursor so a restart
    re-derives at most the region after the last checkpoint and can never
    re-insert a conflicting sealed segment."""

    next_chunk_sequence: int = 0
    consumed_native_frames: int = 0
    consumed_normalized_frames: int = 0
    current_epoch: int = 0
    in_utterance: bool = False
    voiced_run: int = 0
    silence_run: int = 0
    utterance_voiced_frames: int = 0
    utterance_start_ms: int = 0
    current_overlap_ms: int = 0
    pending_pcm: bytearray = field(default_factory=bytearray)
    preroll_pcm: bytearray = field(default_factory=bytearray)
    carry_pcm: bytearray = field(default_factory=bytearray)
    discarded_utterances: int = 0


@dataclass(frozen=True, slots=True)
class SealedSegment:
    session_id: str
    sequence: int
    start_ms: int
    end_ms: int
    duration_ms: int
    overlap_ms: int
    audio_path: Path
    audio_sha256: str
    byte_size: int


class SegmentAgent:
    """Derives semantic ASR segments from committed capture chunks.

    PERSIST FIRST, PROCESS SECOND: each segment WAV is temp-written, flushed and
    fsynced, atomically renamed, directory-fsynced and checksummed before the
    segments row is inserted as CAPTURED in the same transaction that advances
    the durable cursor. A sealed segment is therefore never re-derived, so crash
    resume cannot produce a conflicting duplicate. The SoXR resampler stays warm
    across chunk boundaries within an epoch; after a restart it cold-starts at
    the resume point (documented sub-millisecond approximation, recorded in the
    Phase 3 report). Epoch/gap boundaries force-close the open utterance instead
    of splicing discontinuous audio.
    """

    def __init__(
        self,
        database: Database,
        session_store: Path,
        session_id: str,
        config: SegmenterConfig | None = None,
    ) -> None:
        self.database = database
        self.session_id = session_id
        self.config = config or SegmenterConfig()
        self.segments_dir = session_store / "segments"
        self.segments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._normalizer: Normalizer | None = None
        self._last_row: dict | None = None
        self._state = SegmenterState()

    # ------------------------------------------------------------------ state

    def _load_cursor(self) -> SegmenterState | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_cursors WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
        if row is None:
            return None
        return SegmenterState(
            next_chunk_sequence=row["next_chunk_sequence"],
            consumed_native_frames=row["consumed_native_frames"],
            consumed_normalized_frames=row["consumed_normalized_frames"],
            current_epoch=row["current_epoch"],
            in_utterance=bool(row["in_utterance"]),
            voiced_run=row["voiced_run"],
            silence_run=row["silence_run"],
            utterance_voiced_frames=row["utterance_voiced_frames"],
            utterance_start_ms=row["utterance_start_ms"],
            current_overlap_ms=row["current_overlap_ms"],
            pending_pcm=bytearray(row["pending_pcm"]),
            preroll_pcm=bytearray(row["preroll_pcm"]),
            carry_pcm=bytearray(row["carry_pcm"]),
            discarded_utterances=row["discarded_utterances"],
        )

    def _checkpoint(self, connection, state: SegmenterState) -> None:
        connection.execute(
            """
            INSERT INTO pipeline_cursors(
                session_id, next_chunk_sequence, consumed_native_frames,
                consumed_normalized_frames, current_epoch, in_utterance,
                voiced_run, silence_run, utterance_voiced_frames,
                utterance_start_ms, current_overlap_ms, pending_pcm,
                preroll_pcm, carry_pcm, discarded_utterances, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                next_chunk_sequence = excluded.next_chunk_sequence,
                consumed_native_frames = excluded.consumed_native_frames,
                consumed_normalized_frames = excluded.consumed_normalized_frames,
                current_epoch = excluded.current_epoch,
                in_utterance = excluded.in_utterance,
                voiced_run = excluded.voiced_run,
                silence_run = excluded.silence_run,
                utterance_voiced_frames = excluded.utterance_voiced_frames,
                utterance_start_ms = excluded.utterance_start_ms,
                current_overlap_ms = excluded.current_overlap_ms,
                pending_pcm = excluded.pending_pcm,
                preroll_pcm = excluded.preroll_pcm,
                carry_pcm = excluded.carry_pcm,
                discarded_utterances = excluded.discarded_utterances,
                updated_at = excluded.updated_at
            """,
            (
                self.session_id,
                state.next_chunk_sequence,
                state.consumed_native_frames,
                state.consumed_normalized_frames,
                state.current_epoch,
                int(state.in_utterance),
                state.voiced_run,
                state.silence_run,
                state.utterance_voiced_frames,
                state.utterance_start_ms,
                state.current_overlap_ms,
                bytes(state.pending_pcm),
                bytes(state.preroll_pcm),
                bytes(state.carry_pcm),
                state.discarded_utterances,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    # ------------------------------------------------------------------- main

    def process_available_chunks(self) -> tuple[SealedSegment, ...]:
        """Consume every committed capture chunk not yet fully consumed and
        return the segments sealed during this call."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, capture_epoch, start_ms, end_ms, path,
                       byte_size, sha256, frame_count, sample_rate, channels
                FROM capture_chunks WHERE session_id = ? ORDER BY sequence
                """,
                (self.session_id,),
            ).fetchall()
        chunks = {row["sequence"]: dict(row) for row in rows}
        saved = self._load_cursor()
        if saved is not None:
            self._state = saved
        state = self._state
        sealed: list[SealedSegment] = []
        while True:
            row = chunks.get(state.next_chunk_sequence)
            if row is None:
                break
            sealed.extend(self._consume_chunk(row))
        return tuple(sealed)

    def _handle_epoch_boundary(self, row: dict) -> None:
        """A new capture epoch (pause/resume, restart, device gap) ends the old
        audio timeline: flush the resampler tail, seal voiced content, drop
        silent tails, reset VAD state and the cross-epoch resampler."""
        self._flush_resampler()
        state = self._state
        if state.in_utterance and state.utterance_voiced_frames > 0:
            self._seal_segment()
        else:
            state.pending_pcm = bytearray()
            state.in_utterance = False
        state.preroll_pcm = bytearray()
        state.carry_pcm = bytearray()
        state.voiced_run = state.silence_run = state.utterance_voiced_frames = 0
        state.current_epoch = int(row["capture_epoch"])
        self._normalizer = None

    def _flush_resampler(self) -> None:
        """Emit the SoXR filter tail so sealed segments are sample-complete.
        Crash during a post-flush seal can drop only this bounded tail
        (tens of milliseconds); it is never silently treated as captured."""
        normalizer = self._normalizer
        row = self._last_row
        self._normalizer = None
        if normalizer is None or row is None:
            return
        stream = bytearray(normalizer.process(b"", last=True))
        offset = 0
        while len(stream) - offset >= FRAME_BYTES:
            frame = bytes(stream[offset : offset + FRAME_BYTES])
            offset += FRAME_BYTES
            self._process_frame(frame, row)
        if offset:
            del stream[:offset]
        if stream:
            state = self._state
            if state.in_utterance:
                state.pending_pcm.extend(stream)
            else:
                state.preroll_pcm.extend(stream)
                keep = _overlap_bytes(self.config.overlap_milliseconds)
                if len(state.preroll_pcm) > keep:
                    del state.preroll_pcm[: len(state.preroll_pcm) - keep]

    def _consume_chunk(self, row: dict) -> list[SealedSegment]:
        state = self._state
        at_chunk_start = state.consumed_native_frames == 0
        if at_chunk_start and state.current_epoch != int(row["capture_epoch"]):
            self._handle_epoch_boundary(row)
        self._last_row = row
        path = Path(row["path"])
        if path.is_symlink() or not path.is_file():
            raise CorruptCaptureChunk(f"chunk {row['sequence']} missing")
        if path.stat().st_size != row["byte_size"] or checksum(path) != row["sha256"]:
            raise CorruptCaptureChunk(f"chunk {row['sequence']} failed verification")
        channels = int(row["channels"])
        bytes_per_native_frame = SAMPLE_BYTES * channels
        if self._normalizer is None:
            self._normalizer = Normalizer(int(row["sample_rate"]), channels)
        normalizer = self._normalizer
        sealed: list[SealedSegment] = []
        stream = bytearray(state.carry_pcm)
        state.carry_pcm = bytearray()
        with wave.open(str(path), "rb") as wav:
            if state.consumed_native_frames:
                wav.setpos(state.consumed_native_frames)
            while True:
                block = wav.readframes(8192)
                if not block:
                    break
                state.consumed_native_frames += len(block) // bytes_per_native_frame
                stream.extend(normalizer.process(block))
                offset = 0
                while len(stream) - offset >= FRAME_BYTES:
                    frame = bytes(stream[offset : offset + FRAME_BYTES])
                    offset += FRAME_BYTES
                    segment = self._process_frame(frame, row)
                    if segment is not None:
                        sealed.append(segment)
                if offset:
                    del stream[:offset]
        state.carry_pcm = bytearray(stream)
        with self.database.transaction() as connection:
            state.next_chunk_sequence = int(row["sequence"]) + 1
            state.consumed_native_frames = 0
            state.consumed_normalized_frames = 0
            self._checkpoint(connection, state)
        return sealed

    def _process_frame(self, frame: bytes, row: dict) -> SealedSegment | None:
        state = self._state
        config = self.config
        start_ms = int(row["start_ms"]) + state.consumed_normalized_frames * FRAME_MS
        state.consumed_normalized_frames += 1
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples)))
        voiced = rms >= config.threshold_rms

        if not state.in_utterance:
            if voiced:
                pre_roll_ms = len(state.preroll_pcm) // BYTES_PER_MS
                state.in_utterance = True
                state.voiced_run = 1
                state.silence_run = 0
                state.utterance_voiced_frames = 1
                state.current_overlap_ms = pre_roll_ms
                state.utterance_start_ms = max(0, start_ms - pre_roll_ms)
                state.pending_pcm = bytearray(state.preroll_pcm) + bytearray(frame)
            else:
                state.silence_run += 1
                state.preroll_pcm.extend(frame)
                keep = _overlap_bytes(config.overlap_milliseconds)
                if len(state.preroll_pcm) > keep:
                    del state.preroll_pcm[: len(state.preroll_pcm) - keep]
            return None

        state.pending_pcm.extend(frame)
        if voiced:
            state.voiced_run += 1
            state.silence_run = 0
            state.utterance_voiced_frames += 1
        else:
            state.silence_run += 1
            state.voiced_run = 0

        duration_ms = _pcm_duration_ms(state.pending_pcm)
        voiced_ms = state.utterance_voiced_frames * FRAME_MS
        silence_close = (
            state.silence_run * FRAME_MS >= config.silence_milliseconds
        )
        max_close = duration_ms >= config.max_seconds * 1000
        if not (silence_close or max_close):
            return None

        if max_close or voiced_ms >= config.min_seconds * 1000:
            return self._seal_segment()
        # Sub-minimum utterance closed by silence: discarded to avoid a flood
        # of empty/noise ASR calls (F-032). The audio remains in the sealed
        # capture chunks; only the derived micro-segment is dropped.
        state.in_utterance = False
        tail_bytes = _overlap_bytes(config.overlap_milliseconds)
        state.preroll_pcm = bytearray(state.pending_pcm[-tail_bytes:]) if tail_bytes else bytearray()
        state.pending_pcm = bytearray()
        state.voiced_run = state.silence_run = state.utterance_voiced_frames = 0
        state.discarded_utterances += 1
        return None

    def seal_tail(self) -> tuple[SealedSegment, ...]:
        """Flush the open utterance (End Class / pause boundary). Voiced
        content is sealed even below the minimum duration; silent tails are
        discarded. The reset state is checkpointed durably."""
        self._flush_resampler()
        state = self._state
        if not state.in_utterance:
            return ()
        if state.utterance_voiced_frames > 0:
            return (self._seal_segment(),)
        state.in_utterance = False
        state.pending_pcm = bytearray()
        state.voiced_run = state.silence_run = state.utterance_voiced_frames = 0
        state.discarded_utterances += 1
        with self.database.transaction() as connection:
            self._checkpoint(connection, state)
        return ()

    def _seal_segment(self) -> SealedSegment:
        state = self._state
        pcm = bytes(state.pending_pcm)
        duration_ms = _pcm_duration_ms(pcm)
        if duration_ms <= 0:
            raise RuntimeError("refusing to seal an empty segment")
        segment_overlap_ms = state.current_overlap_ms
        start_ms = state.utterance_start_ms
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS next FROM segments WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            sequence = int(row["next"])
            final_path = self.segments_dir / f"seg_{sequence:06d}.wav"
            temporary_path = final_path.with_suffix(".wav.tmp")
            with temporary_path.open("xb") as handle:
                with wave.open(handle, "wb") as wav:
                    wav.setparams(
                        (1, SAMPLE_BYTES, NORMALIZED_RATE, 0, "NONE", "not compressed")
                    )
                    wav.writeframesraw(pcm)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, final_path)
            sync_directory(self.segments_dir)
            audio_sha256 = checksum(final_path)
            connection.execute(
                """
                INSERT INTO segments(
                    id, session_id, sequence, start_ms, end_ms, duration_ms,
                    overlap_ms, audio_path, audio_sha256, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    self.session_id,
                    sequence,
                    start_ms,
                    start_ms + duration_ms,
                    duration_ms,
                    segment_overlap_ms,
                    str(final_path),
                    audio_sha256,
                    SegmentState.CAPTURED.value,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            # Cursor and segment commit atomically: a sealed segment is never
            # re-derived, so crash resume cannot conflict with it.
            state.in_utterance = False
            state.voiced_run = state.silence_run = state.utterance_voiced_frames = 0
            overlap_bytes = _overlap_bytes(self.config.overlap_milliseconds)
            state.preroll_pcm = bytearray(pcm[-overlap_bytes:]) if overlap_bytes else bytearray()
            state.current_overlap_ms = min(
                self.config.overlap_milliseconds, _pcm_duration_ms(state.preroll_pcm)
            )
            state.pending_pcm = bytearray()
            self._checkpoint(connection, state)
        return SealedSegment(
            session_id=self.session_id,
            sequence=sequence,
            start_ms=start_ms,
            end_ms=start_ms + duration_ms,
            duration_ms=duration_ms,
            overlap_ms=segment_overlap_ms,
            audio_path=final_path,
            audio_sha256=audio_sha256,
            byte_size=final_path.stat().st_size,
        )


def _overlap_bytes(overlap_ms: int) -> int:
    return overlap_ms * BYTES_PER_MS


def _pcm_duration_ms(pcm: bytes) -> int:
    return len(pcm) // BYTES_PER_MS
