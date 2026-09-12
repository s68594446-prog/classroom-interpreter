from __future__ import annotations

import io
import os
import threading
import uuid
import wave
from functools import wraps
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from classroom_interpreter.audio.durable import checksum, sync_directory
from classroom_interpreter.audio.normalize import Normalizer
from classroom_interpreter.domain.models import SegmentState
from classroom_interpreter.storage import Database

NORMALIZED_RATE = 16_000
# Level tracking runs on 5-second blocks, so the floor and peak are always the
# minimum and maximum of samples actually observed in the last 5-10 seconds.
# That bound is the whole point: an exponential leak with no upper bound ran
# the threshold from 300 to 1743 over 54 seconds of real speech and started
# classifying the speaker as silence.
_LEVEL_BLOCK_FRAMES = 500
# Below this ratio between the window's loudest and quietest frame there is
# no observed distinction between speech and silence to place a threshold in.
_MIN_DYNAMIC_RANGE = 3.0
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
    # A soft target, and until now a dead setting: `segment_preferred_seconds`
    # was validated in Settings and never reached the segmenter, so D-043's
    # "12s -> 6s" change altered a number nothing read. A segment closed only
    # on a full silence gap or at the 30s cap. Past the preferred length the
    # segmenter now settles for a shorter pause, so a lecturer who does not
    # stop talking still gets cut at a breath rather than mid-word at 30s.
    preferred_seconds: float = 6.0
    preferred_silence_milliseconds: int = 300
    overlap_milliseconds: int = 250
    # The absolute FLOOR under the adaptive threshold, not the threshold
    # itself. A fixed absolute value cannot work across rooms, microphones and
    # macOS input gain: measured through a real microphone, a 700ms gap
    # qualified once every 36s, so segments ran to the 30s cap instead of
    # closing on silence.
    threshold_rms: float = 300.0
    # Speech is taken to start at this multiple of the running noise floor.
    # Swept against 800s of real captured audio: x4 with 200ms of onset
    # hysteresis gives p50 segment duration 6.2s and p90 9.2s with no segment
    # hitting the cap, against p50 27.2s and 16/36 at the cap for the fixed
    # threshold. Higher multiples start cutting inside speech.
    noise_floor_multiplier: float = 4.0
    # Only a guard against digital silence driving the threshold to zero, not
    # a calibration point. It must stay well below any real speech level: at 75
    # with a x4 multiplier the effective minimum was 300, which is above the
    # median frame of a quietly played recording and produced zero segments.
    noise_floor_min_rms: float = 30.0
    # A silence run is only cancelled by this much CONTINUOUS speech. A single
    # 20ms frame above threshold used to reset it to zero, so one keystroke,
    # cough or reverb tail in an otherwise quiet gap kept the segment open.
    voiced_onset_milliseconds: int = 200


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
    # Windowed level statistics. Checkpointed because a restart must re-derive
    # the post-checkpoint region with the same threshold, or it could seal
    # different segments than the first pass.
    noise_floor_rms: float = 0.0
    level_block_index: int = 0
    level_block_min: float = 0.0
    level_block_max: float = 0.0
    level_previous_min: float = 0.0
    level_previous_max: float = 0.0
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


def _serialized(method):
    """Serialise the segmenter's public entry points.

    Not defensive boilerplate: the failure being prevented is a segmentation
    fault in the resampler's C state, which cannot be caught or logged and
    destroys the recording in progress. A lock is a cheap price for turning a
    process-killing race into a wait.
    """

    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


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
        # `mode=` applies only when mkdir actually creates the directory, and
        # never to the parents it creates on the way. The session directory was
        # therefore left at 0755 while the segments directory beneath it was
        # 0700 -- the audio was protected by one level of luck.
        for directory in (self.segments_dir, session_store):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._normalizer: Normalizer | None = None
        self._last_row: dict | None = None
        self._state = SegmenterState()
        # The resampler underneath `Normalizer` is stateful C code and is not
        # thread-safe: two threads inside `process_available_chunks` at once
        # segfaults the interpreter rather than raising, taking the whole
        # recording down with it. In the running application the segmenter
        # loop and the finalization drain are kept apart by cancellation
        # ordering alone, which is a subtle thing to rely on for a failure mode
        # that cannot be caught, logged, or recovered from.
        self._lock = threading.RLock()

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
            noise_floor_rms=row["noise_floor_rms"],
            level_block_index=row["level_block_index"],
            level_block_min=row["level_block_min"],
            level_block_max=row["level_block_max"],
            level_previous_min=row["level_previous_min"],
            level_previous_max=row["level_previous_max"],
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
                preroll_pcm, carry_pcm, discarded_utterances,
                noise_floor_rms, level_block_index, level_block_min,
                level_block_max, level_previous_min, level_previous_max,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                noise_floor_rms = excluded.noise_floor_rms,
                level_block_index = excluded.level_block_index,
                level_block_min = excluded.level_block_min,
                level_block_max = excluded.level_block_max,
                level_previous_min = excluded.level_previous_min,
                level_previous_max = excluded.level_previous_max,
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
                state.noise_floor_rms,
                state.level_block_index,
                state.level_block_min,
                state.level_block_max,
                state.level_previous_min,
                state.level_previous_max,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    @_serialized
    def rebuild_unconfirmed_tail(self) -> dict[str, int]:
        """Discard open cursor state and re-derive after the DONE prefix.

        Persisted capture chunks remain authoritative. Only a contiguous DONE
        segment prefix is trusted across process restart; every later derived
        row is removed and VAD resumes from the first capture sample after the
        prefix's confirmed end boundary (D-035).
        """
        stale_paths: list[Path] = []
        with self.database.transaction() as connection:
            segments = connection.execute(
                "SELECT id, sequence, end_ms, audio_path, state FROM segments WHERE session_id = ? ORDER BY sequence",
                (self.session_id,),
            ).fetchall()
            keep_count = 0
            boundary_ms = 0
            for row in segments:
                if row["sequence"] != keep_count or row["state"] != SegmentState.DONE.value:
                    break
                keep_count += 1
                boundary_ms = int(row["end_ms"])
            stale = [row for row in segments if row["sequence"] >= keep_count]
            stale_paths = [Path(row["audio_path"]) for row in stale]
            for row in stale:
                connection.execute(
                    "DELETE FROM state_transition_audits WHERE entity_id = ?",
                    (row["id"],),
                )
            connection.execute(
                "DELETE FROM segments WHERE session_id = ? AND sequence >= ?",
                (self.session_id, keep_count),
            )
            connection.execute(
                """
                UPDATE sessions SET next_translation_sequence = ?, updated_at = ?,
                    version = version + 1 WHERE id = ?
                """,
                (keep_count, datetime.now(timezone.utc).isoformat(), self.session_id),
            )
            chunk = connection.execute(
                """
                SELECT sequence, capture_epoch, start_ms, end_ms, sample_rate,
                       frame_count
                FROM capture_chunks
                WHERE session_id = ? AND end_ms > ?
                ORDER BY sequence LIMIT 1
                """,
                (self.session_id, boundary_ms),
            ).fetchone()
            if chunk is None:
                tail = connection.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 AS next FROM capture_chunks WHERE session_id = ?",
                    (self.session_id,),
                ).fetchone()
                next_chunk = int(tail["next"])
                native_frames = normalized_frames = epoch = 0
            else:
                next_chunk = int(chunk["sequence"])
                offset_ms = max(0, boundary_ms - int(chunk["start_ms"]))
                native_frames = min(
                    int(chunk["frame_count"]),
                    round(offset_ms * int(chunk["sample_rate"]) / 1000),
                )
                normalized_frames = offset_ms // FRAME_MS
                epoch = int(chunk["capture_epoch"])
            rebuilt = SegmenterState(
                next_chunk_sequence=next_chunk,
                consumed_native_frames=native_frames,
                consumed_normalized_frames=normalized_frames,
                current_epoch=epoch,
            )
            self._checkpoint(connection, rebuilt)

        session_root = self.segments_dir.resolve()
        for path in stale_paths:
            if path.is_symlink():
                raise CorruptCaptureChunk("derived segment path is a symlink")
            if path.exists() and path.resolve().is_relative_to(session_root):
                path.unlink()
        self._state = rebuilt
        self._normalizer = None
        self._last_row = None
        return {
            "kept_done_segments": keep_count,
            "removed_unconfirmed_segments": len(stale_paths),
            "restart_boundary_ms": boundary_ms,
        }

    # ------------------------------------------------------------------- main

    @_serialized
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

    def _update_threshold(self, state: SegmenterState, rms: float) -> float:
        """Put the speech threshold between the room's floor and its speech.

        A fixed absolute RMS cannot work across rooms, microphones and playback
        volume. Two real recordings make that concrete: one had frame RMS p5=93
        and p50=1706, the other p5=77 and p50=393 -- the same threshold that
        segmented the first correctly produced ZERO segments on the second,
        because the whole signal sat near it.

        So the threshold is the geometric mean of a windowed floor and a
        windowed peak -- the midpoint in dB between silence and speech -- which
        depends on the ratio between them rather than on absolute level.

        Both statistics are the minimum and maximum of samples actually
        observed in the last 5-10 seconds, which is the property that matters
        most here. The previous version of this method used an exponential
        upward leak with no bound; over 54 seconds of continuous speech it
        drove the threshold from 300 to 1743, above the speaker's own level,
        and segmentation stopped dead after the first sentence. A statistic
        that is a min and a max of real samples cannot do that: it is bounded
        by the signal by construction.
        """
        config = self.config
        if state.level_block_index >= _LEVEL_BLOCK_FRAMES:
            state.level_previous_min = state.level_block_min
            state.level_previous_max = state.level_block_max
            state.level_block_min = 0.0
            state.level_block_max = 0.0
            state.level_block_index = 0
        state.level_block_index += 1
        state.level_block_min = (
            rms if state.level_block_min <= 0 else min(state.level_block_min, rms)
        )
        state.level_block_max = max(state.level_block_max, rms)

        candidates = [v for v in (state.level_block_min, state.level_previous_min) if v > 0]
        floor = max(min(candidates) if candidates else rms, config.noise_floor_min_rms)
        peak = max(state.level_block_max, state.level_previous_max)
        state.noise_floor_rms = floor
        if peak < config.threshold_rms:
            # Nothing in the last five to ten seconds was loud enough to be
            # speech at all: an empty room, a break, a lecturer who has stopped.
            #
            # Without this the geometric mean below is computed from a window
            # containing ONLY room noise, and the mean of that noise's own
            # minimum and maximum lands in the middle of the noise. It
            # manufactures a speech/silence split where there is no speech.
            # Measured on synthetic room tone at the levels of the real
            # recording (min 20, median 60, max 140): silent for the first five
            # seconds while the window still held speech, then 56% of frames
            # "voiced" for as long as the quiet lasted. The dynamic-range guard
            # below does not catch it -- noise between 20 and 140 has a range
            # of 7, comfortably over the 3 it looks for.
            #
            # What that cost: utterances open on nothing and run to the 30s
            # cap, each one a paid transcription of silence; the interim loop
            # sees a pending utterance forever and pays for an ASR call every
            # two seconds into an empty room; and the silence run never grows,
            # so auto-pause can never fire.
            #
            # `threshold_rms` is the absolute floor this has always been
            # documented as -- but it is compared against the window's PEAK,
            # not against each frame. That distinction is why a fixed threshold
            # failed before: on a quiet recording (frame p50 393) half the
            # individual speech frames fall under 300, while the maximum of any
            # five seconds of real speech is several times it.
            return config.threshold_rms
        if peak < floor * _MIN_DYNAMIC_RANGE:
            # The window is loud but shows no gap between quiet and loud: a
            # speaker who has not paused yet, or a session that began
            # mid-sentence. There is no evidence on which to place a boundary,
            # so treat the audio as speech. Over-capturing costs a segment that
            # runs to its maximum length; under-capturing loses the words
            # entirely, and that is the failure that actually happened.
            return floor
        return max((floor * peak) ** 0.5, config.noise_floor_min_rms)

    def _process_frame(self, frame: bytes, row: dict) -> SealedSegment | None:
        state = self._state
        config = self.config
        start_ms = int(row["start_ms"]) + state.consumed_normalized_frames * FRAME_MS
        state.consumed_normalized_frames += 1
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples)))
        voiced = rms >= self._update_threshold(state, rms)

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
            state.utterance_voiced_frames += 1
            # Only sustained speech cancels a silence run. Resetting on a
            # single 20ms frame meant one keystroke, cough or reverb tail in an
            # otherwise quiet gap kept the segment open all the way to the cap.
            if state.voiced_run * FRAME_MS >= config.voiced_onset_milliseconds:
                state.silence_run = 0
        else:
            state.silence_run += 1
            state.voiced_run = 0

        duration_ms = _pcm_duration_ms(state.pending_pcm)
        voiced_ms = state.utterance_voiced_frames * FRAME_MS
        # Past the preferred length, take the first real breath instead of
        # holding out for a full gap. Measured on an hour of real microphone
        # audio this moved segment duration from p50 27.2s / p90 30.0s (with
        # 16 of 36 segments hitting the cap) to p50 6.6s / p90 9.5s with none
        # at the cap.
        required_silence_ms = config.silence_milliseconds
        if duration_ms >= config.preferred_seconds * 1000:
            required_silence_ms = min(
                required_silence_ms, config.preferred_silence_milliseconds
            )
        silence_close = state.silence_run * FRAME_MS >= required_silence_ms
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

    @_serialized
    def pending_utterance_wav(self, *, min_ms: int = 1200) -> bytes | None:
        """In-memory WAV of the utterance currently being spoken, or None.

        This is the input for partial (interim) transcription: it lets the UI
        show English while the sentence is still being said, instead of waiting
        for the segment to close. Measured on the live endpoint, transcribing
        the first 1.5s of speech returned usable text in 0.8s -- a caption
        roughly 2.3s behind the speaker, against ~7s when waiting for the
        segment to seal.

        Deliberately in-memory and never written to disk: partial text is a
        display convenience, not durable state. It is not persisted, not
        sequenced, and never reaches the summary. The sealed segment remains
        the single factual source, so `PERSIST FIRST, PROCESS SECOND` and the
        English-as-source-of-truth contract are untouched.

        Returns None below `min_ms`, where there is too little audio for a
        transcript worth showing.
        """
        state = self._state
        if not state.in_utterance:
            return None
        pcm = bytes(state.pending_pcm)
        if _pcm_duration_ms(pcm) < min_ms:
            return None
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams(
                (1, SAMPLE_BYTES, NORMALIZED_RATE, 0, "NONE", "not compressed")
            )
            wav.writeframesraw(pcm)
        return buffer.getvalue()

    def input_level(self) -> dict:
        """What the segmenter is currently hearing, for display and diagnosis.

        An audio product that hears nothing looks exactly like an audio product
        in a quiet room, and this one has failed that way more than once. The
        numbers are levels, never content: an RMS peak, an RMS floor and the
        threshold between them.

        It also makes the one risk in the empty-room rule diagnosable in a
        glance. That rule silences a window whose peak is below
        `threshold_rms`, so a microphone with the input gain turned down would
        be heard as an empty room -- indistinguishable from an empty room, from
        the outside, unless the peak itself is on screen.
        """
        state = self._state
        peak = max(state.level_block_max, state.level_previous_max)
        return {
            "peak_rms": round(peak, 1),
            "floor_rms": round(state.noise_floor_rms, 1),
            "speech_floor_rms": round(self.config.threshold_rms, 1),
            "hearing_speech": bool(state.in_utterance),
        }

    def idle_silence_ms(self) -> int:
        """Continuous trailing silence, in ms, while no utterance is open.

        Returns 0 whenever an utterance is in progress: a pause *inside* a
        sentence is normal speech rhythm, not an idle classroom. `silence_run`
        is already checkpointed, so this survives a restart rather than
        resetting the quiet stretch to zero.
        """
        if self._state.in_utterance:
            return 0
        return self._state.silence_run * FRAME_MS


    @_serialized
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
            if temporary_path.is_symlink():
                raise CorruptCaptureChunk("segment temporary path is a symlink")
            if temporary_path.exists():
                temporary_path.unlink()
            with temporary_path.open("xb") as handle:
                with wave.open(handle, "wb") as wav:
                    wav.setparams(
                        (1, SAMPLE_BYTES, NORMALIZED_RATE, 0, "NONE", "not compressed")
                    )
                    wav.writeframesraw(pcm)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, final_path)
            # Recorded classroom speech. Created 0666 & ~umask == 0644 on a
            # default account; the enclosing 0700 directory is the only thing
            # that was hiding it, and a mode survives a copy or a backup where
            # a directory's does not.
            try:
                os.chmod(final_path, 0o600)
            except OSError:
                pass
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
