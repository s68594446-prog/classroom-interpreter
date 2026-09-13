from __future__ import annotations

import asyncio
import fcntl
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from functools import wraps
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import sounddevice as sd

from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.storage import Database, SessionRepository
from .durable import ChunkWriter, recover, utc_now, validate_session_id
from .normalize import Normalizer


@dataclass(frozen=True)
class Buffer:
    pcm: bytes
    frames: int
    monotonic_ns: int
    wall: str
    epoch: int


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.controller:
            return method(self, *args, **kwargs)
    return call


class AudioListener:
    """Synchronous capture core with the async AudioListenerAgent public adapter.

    One controller thread owns lifecycle; one consumer owns writer and resampler.
    Callback only copies a bounded block, timestamps, and tries nonblocking enqueue.
    Fatal faults require explicit stop/recovery; no silent device switching/retry.
    """

    def __init__(self, database: Database, root: Path, *, device=None,
                 rate: int | None = None, channels: int = 1, queue_blocks: int = 128,
                 chunk_seconds: float = 2, synthetic: bool = False,
                 min_free_bytes: int = 256 * 1024 * 1024,
                 gap_tolerance_seconds: float = 5.0, max_tolerated_gaps: int = 60):
        if queue_blocks < 1 or channels not in (1, 2):
            raise ValueError('INVALID_BUFFER_CONFIGURATION')
        self.database, self.repository = database, SessionRepository(database)
        self.controller = threading.RLock()
        self.root = root.resolve()
        self.device, self.rate, self.channels = device, rate, channels
        self.synthetic, self.chunk_seconds = synthetic, chunk_seconds
        self.min_free_bytes = min_free_bytes
        self.gap_tolerance_ns = int(gap_tolerance_seconds * 1e9)
        self.max_tolerated_gaps = max_tolerated_gaps
        self.buffers = queue.Queue(maxsize=queue_blocks)
        self.finished = threading.Event()
        self.accepting = False
        self.stream = self.worker = self.assertion = self.lock_handle = None
        self.fault_detail = None
        self.owns_capture = False
        self.writer = None
        self.session_id = None
        self.state = 'NEW'
        self.fault = None
        self.epoch = 0
        self.received = self.committed = self.chunks = self.overruns = self.high_water = 0
        self.normalized_frames = 0
        self.write_latency = self.resample_latency = 0.0
        self.last_frame = self.last_commit = None
        self.gaps = self.gap_ns = self.largest_gap_ns = 0
        self.unknown_gaps = 0
        self.previous_end = None
        self.pause_started = None
        self.pause_total_ns = 0
        self.active_started_ns = None
        self.active_total_ns = 0
        self.startup_total_ns = 0
        self.drain_total_ns = 0
        self.process = psutil.Process()
        self.process.cpu_percent()
        self.cpu_sample_time = time.monotonic()
        self.cpu_percent = 0.0
        self.device_info = {'name': 'synthetic' if synthetic else 'not_opened'}

    def _event(self, kind: str, reason: str, start: int, end: int) -> None:
        with self.database.transaction() as conn:
            conn.execute('''INSERT INTO capture_events(session_id,kind,reason,
                start_monotonic_ns,end_monotonic_ns,created_at) VALUES (?,?,?,?,?,?)''',
                (self.session_id, kind, reason, start, max(start, end), utc_now()))

    def _transition(self, target: SessionStatus) -> None:
        current = self.repository.get_session(self.session_id).status
        if current != target:
            self.repository.transition_session(self.session_id, target,
                reason_code=self.fault or 'AUDIO_LIFECYCLE', actor='audio_listener')

    def _open_stream(self) -> None:
        if self.synthetic:
            return
        selected = sd.query_devices(self.device, 'input')
        if selected['max_input_channels'] < self.channels:
            raise RuntimeError('DEVICE_CHANNELS_UNAVAILABLE')
        if self.device_info['name'] != 'not_opened' and selected['name'] != self.device_info['name']:
            raise RuntimeError('DEVICE_IDENTITY_CHANGED')
        self.device_info = dict(selected)
        self.device = selected['index']
        native_rate = int(selected['default_samplerate'])
        if self.rate is not None and self.rate != native_rate:
            raise ValueError('NATIVE_RATE_CHANGED_OR_OVERRIDDEN')
        self.rate = native_rate
        sd.check_input_settings(device=self.device, channels=self.channels,
                                dtype='int16', samplerate=self.rate)
        # Fixed 20 ms blocks bound per-callback allocation as well as queue length.
        self.stream = sd.RawInputStream(device=self.device, channels=self.channels,
            samplerate=self.rate, dtype='int16', blocksize=round(self.rate * .02),
            callback=self._callback)

    @serialized
    def begin(self, session_id: str) -> None:
        if self.state != 'NEW':
            raise RuntimeError('LISTENER_ALREADY_STARTED')
        validate_session_id(session_id)
        self.session_id = session_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_root = self.root / session_id
        if self.session_root.is_symlink():
            raise ValueError('SYMLINK_SESSION_ROOT')
        self.session_root.mkdir(mode=0o700, exist_ok=True)
        self.lock_handle = (self.root / 'capture.lock').open('a')
        try:
            fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owns_capture = True
        except OSError:
            self.lock_handle.close()
            self.lock_handle = None
            raise RuntimeError('CAPTURE_ALREADY_OWNED') from None
        self.started_ns = time.monotonic_ns()
        self.started_at = utc_now()
        try:
            try:
                old = self.repository.get_session(session_id)
            except KeyError:
                old = self.repository.create_session('Audio capture spike', app_version='phase2', session_id=session_id)
            if old.status not in {SessionStatus.NEW, SessionStatus.RECORDING, SessionStatus.PAUSED, SessionStatus.RECOVERY_REQUIRED}:
                raise RuntimeError('SESSION_NOT_RESTARTABLE')
            if old.status is SessionStatus.NEW:
                self._transition(SessionStatus.RECORDING)
            if old.status in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
                self._transition(SessionStatus.RECOVERY_REQUIRED)
            recovery = recover(self.database, self.session_root, session_id)
            if not recovery['clean']:
                raise RuntimeError('UNVERIFIED_RECOVERY_TAIL')
            with self.database.connect() as conn:
                previous = conn.execute('SELECT max(capture_epoch),max(end_monotonic_ns) FROM capture_chunks WHERE session_id=?', (session_id,)).fetchone()
            self.epoch = (previous[0] + 1) if previous[0] is not None else 0
            if old.status != SessionStatus.NEW:
                self._event('restart', 'EXPLICIT_RECOVERY', self.started_ns, self.started_ns)
                # Cross-reboot monotonic clocks cannot establish gap duration.
                self._event('audio_gap', 'RESTART_DURATION_UNKNOWN', self.started_ns, self.started_ns)
                self.gaps += 1
                self.unknown_gaps += 1
            if old.status is not SessionStatus.NEW:
                self._transition(SessionStatus.RECORDING)
            self._open_stream()
            self.rate = self.rate or 48000
            self.writer = ChunkWriter(self.database, self.session_root, session_id,
                                      self.rate, self.channels, self.chunk_seconds)
            self._start_epoch()
        except Exception:
            self.fault = self.fault or 'CAPTURE_START_FAILED'
            self.close()
            raise

    def _start_epoch(self) -> None:
        opening_ns = time.monotonic_ns()
        self.finished.clear()
        self.previous_end = None
        self.accepting = True
        self.state = 'RECORDING'
        self.epoch_started = self.last_arrival = time.monotonic_ns()
        self.wall_anchor = utc_now()
        self.worker = threading.Thread(target=self._consume, name='capture-writer', daemon=True)
        self.worker.start()
        if self.stream:
            self.stream.start()
        self.active_started_ns = time.monotonic_ns()
        self.startup_total_ns += self.active_started_ns - opening_ns
        if not self.synthetic and sys.platform == 'darwin':
            self.assertion = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _callback(self, data, frames, timing, status) -> None:
        if not self.accepting:
            raise sd.CallbackAbort
        now = time.monotonic_ns()
        self.last_arrival = now
        if status:
            self.overruns += 1
            # An input overflow means PortAudio discarded samples because this
            # process was briefly behind -- a GC pause, a disk write, another
            # application taking the CPU. On a laptop running a three-hour
            # class that is an ordinary event, not a broken microphone.
            #
            # It used to end the recording. That is the same mistake already
            # corrected one layer down for ADC discontinuities, left standing
            # here: the samples are gone either way, and stopping capture does
            # not recover them -- it only destroys the rest of the class to
            # preserve the continuity of the part already written. The lost
            # samples surface as a timestamp gap in the consumer, which seals
            # the chunk, writes an `audio_gap` event and counts it, so the
            # record still states plainly that audio is missing.
            #
            # A stream that keeps overflowing is a different thing: past the
            # same budget the gap tolerance uses, the device really is failing
            # and the fault stands. Any other status flag is fatal as before.
            if not (status.input_overflow and self.overruns <= self.max_tolerated_gaps):
                self.fault = 'CALLBACK_STATUS'
                self.fault_detail = f'overruns={self.overruns}'
                self.accepting = False
                raise sd.CallbackAbort
        if frames > round(self.rate * .02) or len(data) != frames * self.channels * 2:
            self.fault = 'CALLBACK_BUFFER_INVALID'
            self.accepting = False
            raise sd.CallbackAbort
        ns = now + round((timing.inputBufferAdcTime - timing.currentTime) * 1e9)
        # Wall-clock mapping computed by consumer from an epoch anchor.
        if not self.submit(bytes(data), frames, ns):
            raise sd.CallbackAbort

    def submit(self, pcm: bytes, frames: int, ns: int) -> bool:
        """Nonblocking capture ingress, also used by deterministic fake backend."""
        if not self.accepting:
            return False
        self.received += frames
        self.last_frame = ns
        try:
            self.buffers.put_nowait(Buffer(pcm, frames, ns, self.wall_anchor, self.epoch))
            self.high_water = max(self.high_water, self.buffers.qsize())
            return True
        except queue.Full:
            self.fault = 'QUEUE_OVERFLOW'
            self.accepting = False
            return False

    def _account(self, chunk) -> None:
        if chunk:
            self.committed += chunk.frame_count
            self.chunks += 1
            self.last_commit = time.monotonic_ns()

    def _consume(self) -> None:
        try:
            normalizer = Normalizer(self.rate, self.channels)
            def committed(chunk):
                if chunk is None:
                    return
                self._account(chunk)
                before = time.perf_counter()
                with wave.open(chunk.path, 'rb') as wav:
                    while pcm := wav.readframes(4096):
                        self.normalized_frames += len(normalizer.process(pcm)) // 2
                self.resample_latency = (time.perf_counter() - before) * 1000
            while not self.finished.is_set() or not self.buffers.empty():
                try:
                    frame = self.buffers.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if shutil.disk_usage(self.root).free < self.min_free_bytes:
                        raise OSError('LOW_DISK')
                    if self.previous_end is not None:
                        delta = frame.monotonic_ns - self.previous_end
                        if abs(delta) > 100_000_000:
                            committed(self.writer.seal())
                            self._event('audio_gap', 'ADC_DISCONTINUITY', min(self.previous_end, frame.monotonic_ns),
                                        max(self.previous_end, frame.monotonic_ns))
                            self.gaps += 1
                            self.gap_ns += abs(delta)
                            self.largest_gap_ns = max(self.largest_gap_ns, abs(delta))
                            # The gap is now sealed, timestamped and counted, so
                            # the record states plainly that audio is missing --
                            # nothing is fabricated by carrying on.
                            #
                            # Ending capture here did not protect that record;
                            # it destroyed the REST of the class to preserve the
                            # continuity of the part already written. Measured:
                            # a single discontinuity 23.5 minutes into a
                            # 29-minute run stopped recording for good, and
                            # silently. On a laptop under load a 100ms
                            # scheduling hiccup is an ordinary event, not a
                            # broken device.
                            #
                            # A genuinely large gap, or a stream that keeps
                            # gapping, is a different thing and still fatal.
                            if (abs(delta) > self.gap_tolerance_ns
                                    or self.gaps > self.max_tolerated_gaps):
                                raise RuntimeError('AUDIO_CONTINUITY_LOST')
                    wall = (datetime.fromisoformat(frame.wall) + timedelta(
                        seconds=(frame.monotonic_ns - self.epoch_started) / 1e9)).isoformat()
                    before = time.perf_counter()
                    completed = self.writer.append(frame.pcm, frame.monotonic_ns, wall, frame.epoch)
                    self.write_latency = (time.perf_counter() - before) * 1000
                    for chunk in completed:
                        committed(chunk)
                    self.previous_end = frame.monotonic_ns + round(frame.frames / self.rate * 1e9)
                finally:
                    self.buffers.task_done()
            committed(self.writer.seal())
            self.normalized_frames += len(normalizer.process(b'', last=True)) // 2
        except Exception as exc:
            # Keep the exception type. The reason code alone says a write or a
            # resample failed and nothing about which, so a capture that stops
            # mid-class leaves no way to tell a full disk from a lost device
            # from a bug. The type name is content-free.
            self.fault = self.fault or 'WRITER_OR_NORMALIZER_FAILED'
            self.fault_detail = self.fault_detail or type(exc).__name__
            self.accepting = False
            self.writer.abandon()
            while True:
                try:
                    self.buffers.get_nowait()
                    self.buffers.task_done()
                except queue.Empty:
                    break

    @serialized
    def check(self) -> bool:
        if self.state == 'RECORDING' and not self.synthetic and not self.fault:
            if self.stream is None or not self.stream.active or time.monotonic_ns() - self.last_arrival > 2_000_000_000:
                self.fault = 'DEVICE_STOPPED_OR_STALLED'
        if self.fault:
            self.close()
            return False
        return True

    def _drain(self) -> None:
        draining_ns = time.monotonic_ns()
        if self.active_started_ns is not None:
            self.active_total_ns += draining_ns - self.active_started_ns
            self.active_started_ns = None
        self.accepting = False
        try:
            if self.stream:
                try:
                    # Input has no playback tail. Abort stops future callbacks;
                    # queued frames are still drained and durably sealed below.
                    # A real SIGINT probe showed Pa_StopStream can deadlock in
                    # CoreAudio AudioOutputUnitStop, so it is not used here.
                    self.stream.abort()
                except Exception:
                    self.fault = self.fault or 'DEVICE_ABORT_FAILED'
                try:
                    self.stream.close()
                except Exception:
                    self.fault = self.fault or 'DEVICE_CLOSE_FAILED'
                self.stream = None
        finally:
            self.finished.set()
            if self.worker:
                self.worker.join(timeout=15)
                if self.worker.is_alive():
                    self.fault = 'WRITER_SHUTDOWN_TIMEOUT'
                    # Do not release ownership while a writer still owns resources.
                    raise RuntimeError('WRITER_SHUTDOWN_TIMEOUT')
                self.worker = None
            if self.assertion:
                self.assertion.terminate()
                self.assertion.wait(timeout=3)
                self.assertion = None
            self.drain_total_ns += time.monotonic_ns() - draining_ns

    @serialized
    def pause_capture(self) -> None:
        if self.state != 'RECORDING' or self.fault:
            raise RuntimeError('NOT_RECORDING')
        self._drain()
        if self.fault:
            self.close()
            return
        self.pause_started = time.monotonic_ns()
        self._event('pause', 'PAUSE_BEGIN', self.pause_started, self.pause_started)
        self._transition(SessionStatus.PAUSED)
        self.state = 'PAUSED'

    @serialized
    def resume_capture(self) -> None:
        if self.state != 'PAUSED':
            raise RuntimeError('NOT_PAUSED')
        try:
            self._open_stream()
            now = time.monotonic_ns()
            self._event('pause', 'PAUSE_END', self.pause_started, now)
            self.pause_total_ns += now - self.pause_started
            self.pause_started = None
            self.epoch += 1
            self._transition(SessionStatus.RECORDING)
            self._start_epoch()
        except Exception:
            self.fault = 'RESUME_FAILED'
            self.close()
            raise

    @serialized
    def close(self) -> None:
        if self.state in {'STOPPED', 'RECOVERY_REQUIRED'}:
            return
        if not self.owns_capture:
            return
        self._drain()
        self.ended_ns = time.monotonic_ns()
        self.ended_at = utc_now()
        if self.pause_started:
            self._event('pause', 'STOP_DURING_PAUSE', self.pause_started, self.ended_ns)
            self.pause_total_ns += self.ended_ns - self.pause_started
            self.pause_started = None
        try:
            if self.session_id:
                current = self.repository.get_session(self.session_id).status
                if self.fault:
                    if not self.gaps:
                        # Fault ends trustworthy continuity. A zero lower bound is
                        # explicitly UNKNOWN, never evidence of zero missing audio.
                        gap_start = min(self.previous_end or self.ended_ns, self.ended_ns)
                        delta = self.ended_ns - gap_start
                        self._event('audio_gap', 'FAULT_INTERVAL_LOWER_BOUND', gap_start, self.ended_ns)
                        self.gaps += 1
                        self.unknown_gaps += 1
                        self.gap_ns += delta
                        self.largest_gap_ns = max(self.largest_gap_ns, delta)
                    self._event('fault', self.fault, self.ended_ns, self.ended_ns)
                    if current in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
                        self._transition(SessionStatus.RECOVERY_REQUIRED)
                elif current in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
                    self._transition(SessionStatus.FINALIZING)
        finally:
            self.state = 'RECOVERY_REQUIRED' if self.fault else 'STOPPED'
            if self.lock_handle:
                self.lock_handle.close()
                self.lock_handle = None
            self.owns_capture = False

    def snapshot(self) -> dict:
        if time.monotonic() - self.cpu_sample_time >= 1:
            self.cpu_percent = self.process.cpu_percent()
            self.cpu_sample_time = time.monotonic()
        end = getattr(self, 'ended_ns', time.monotonic_ns())
        elapsed = (end - getattr(self, 'started_ns', end)) / 1e9
        pause = self.pause_total_ns + (end - self.pause_started if self.pause_started else 0)
        active_ns = self.active_total_ns + (end - self.active_started_ns if self.active_started_ns else 0)
        return dict(capture_active=self.state == 'RECORDING' and self.accepting and not self.fault,
            state=self.state, reason_code=self.fault, device_name=self.device_info['name'],
            device_sample_rate=self.rate, internal_sample_rate=16000, channels=self.channels,
            frames_received_total=self.received, audio_seconds_captured=self.committed / (self.rate or 1),
            recovery_chunks_written=self.chunks, callback_overrun_count=self.overruns,
            queue_depth=self.buffers.qsize(), queue_high_watermark=self.high_water,
            queue_capacity=self.buffers.maxsize, write_latency_ms=self.write_latency,
            resample_latency_ms=self.resample_latency, last_audio_frame_at=self.last_frame,
            last_chunk_committed_at=self.last_commit, memory_rss_mb=self.process.memory_info().rss / 1048576,
            cpu_percent=self.cpu_percent, open_file_count=len(self.process.open_files()),
            open_fd_count=self.process.num_fds(), disk_free_bytes=shutil.disk_usage(self.root).free,
            wall_clock_duration=elapsed, expected_audio_duration=active_ns / 1e9,
            intentional_pause_seconds=pause / 1e9,
            startup_seconds=self.startup_total_ns / 1e9, shutdown_drain_seconds=self.drain_total_ns / 1e9,
            gap_count=self.gaps, total_gap_seconds=self.gap_ns / 1e9,
            unknown_gap_count=self.unknown_gaps, gap_duration_is_lower_bound=bool(self.unknown_gaps),
            largest_gap_seconds=self.largest_gap_ns / 1e9, normalized_frames=self.normalized_frames,
            start_time=getattr(self, 'started_at', None), end_time=getattr(self, 'ended_at', None),
            timing_tolerance_ms=100)

    async def start(self, session_id: str) -> None:
        task = asyncio.create_task(asyncio.to_thread(self.begin, session_id))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation cannot strand a newly opened microphone in a worker.
            try:
                await task
            finally:
                await asyncio.to_thread(self.close)
            raise

    async def pause(self, session_id: str) -> None:
        self._assert_session(session_id)
        await asyncio.to_thread(self.pause_capture)

    async def resume(self, session_id: str) -> None:
        self._assert_session(session_id)
        await asyncio.to_thread(self.resume_capture)

    async def stop(self, session_id: str) -> None:
        self._assert_session(session_id)
        await asyncio.to_thread(self.close)

    def _assert_session(self, session_id: str) -> None:
        if session_id != self.session_id:
            raise ValueError('SESSION_MISMATCH')
