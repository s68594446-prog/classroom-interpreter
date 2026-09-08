from __future__ import annotations

import hashlib
import json
import os
import platform
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from classroom_interpreter.storage import Database

try:
    import psutil
    MONOTONIC_CLOCK_ID = f'{platform.node()}:{round(psutil.boot_time())}'
except (ImportError, OSError):  # Audio extra supplies psutil; explicit fallback is process-local.
    MONOTONIC_CLOCK_ID = f'{platform.node()}:process-{os.getpid()}'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_session_id(session_id: str) -> None:
    if not session_id or len(session_id) > 128 or any(
            c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in session_id):
        raise ValueError('INVALID_SESSION_ID')


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(131072), b''):
            digest.update(block)
    return digest.hexdigest()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    session_id: str
    sequence: int
    capture_epoch: int
    start_monotonic_ns: int
    end_monotonic_ns: int
    wall_clock_start: str
    wall_clock_end: str
    sample_rate: int
    channels: int
    sample_format: str
    frame_count: int
    path: str
    checksum: str
    byte_size: int
    status: str = 'AUDIO_CAPTURED'
    monotonic_clock_id: str | None = None


def commit_chunk(database: Database, chunk: Chunk) -> None:
    with database.transaction() as conn:
        existing = conn.execute('SELECT * FROM capture_chunks WHERE id = ?',
                                (chunk.chunk_id,)).fetchone()
        if existing:
            if (existing['sha256'] != chunk.checksum or existing['path'] != chunk.path
                    or existing['session_id'] != chunk.session_id or existing['sequence'] != chunk.sequence
                    or existing['capture_epoch'] != chunk.capture_epoch
                    or existing['frame_count'] != chunk.frame_count
                    or existing['start_monotonic_ns'] != chunk.start_monotonic_ns
                    or existing['end_monotonic_ns'] != chunk.end_monotonic_ns
                    or existing['monotonic_clock_id'] != chunk.monotonic_clock_id):
                raise ValueError('CHUNK_ID_CONFLICT')
            return
        previous = conn.execute('SELECT end_ms,end_monotonic_ns,monotonic_clock_id FROM capture_chunks WHERE session_id=? ORDER BY sequence DESC LIMIT 1',
                                (chunk.session_id,)).fetchone()
        # Preserve the existing session-relative millisecond contract. On reboot,
        # cross-boot gap is unknown and is represented by an explicit restart event.
        same_clock = (chunk.monotonic_clock_id is not None and previous is not None
                      and previous['end_monotonic_ns'] is not None
                      and previous['monotonic_clock_id'] == chunk.monotonic_clock_id)
        start_ms = (previous['end_ms'] + max(0, (chunk.start_monotonic_ns - previous['end_monotonic_ns']) // 1000000)
                    if same_clock else (previous['end_ms'] if previous is not None else 0))
        end_ms = start_ms + round(chunk.frame_count / chunk.sample_rate * 1000)
        conn.execute('''INSERT INTO capture_chunks (
            id,session_id,sequence,capture_epoch,start_ms,end_ms,path,byte_size,
            sha256,sealed_at,start_monotonic_ns,end_monotonic_ns,wall_clock_start,
            wall_clock_end,sample_rate,channels,sample_format,frame_count,status,
            monotonic_clock_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            chunk.chunk_id,chunk.session_id,chunk.sequence,chunk.capture_epoch,
            start_ms,end_ms,
            chunk.path,chunk.byte_size,chunk.checksum,utc_now(),
            chunk.start_monotonic_ns,chunk.end_monotonic_ns,chunk.wall_clock_start,
            chunk.wall_clock_end,chunk.sample_rate,chunk.channels,chunk.sample_format,
            chunk.frame_count,chunk.status,chunk.monotonic_clock_id))


def verify_chunk(chunk: Chunk, root: Path, session_id: str) -> Path:
    path = Path(chunk.path)
    expected = root / f'chunk_{chunk.sequence:06d}.wav'
    if (chunk.session_id != session_id or chunk.sequence < 0 or path != expected
            or path.is_symlink() or path.resolve().parent != root.resolve()
            or chunk.chunk_id != f'{session_id}:{chunk.sequence}'
            or chunk.status != 'AUDIO_CAPTURED' or chunk.sample_format != 'PCM_S16LE'
            or chunk.sample_rate <= 0 or chunk.channels not in (1, 2)
            or chunk.frame_count <= 0 or chunk.end_monotonic_ns <= chunk.start_monotonic_ns):
        raise ValueError('INVALID_CHUNK_MANIFEST')
    if abs(chunk.end_monotonic_ns - chunk.start_monotonic_ns - round(chunk.frame_count / chunk.sample_rate * 1e9)) > 1:
        raise ValueError('CHUNK_DURATION_MISMATCH')
    if path.stat().st_size != chunk.byte_size or checksum(path) != chunk.checksum:
        raise ValueError('CHUNK_CHECKSUM_MISMATCH')
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnframes(), wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (
                chunk.frame_count, chunk.sample_rate, chunk.channels, 2):
            raise ValueError('CHUNK_HEADER_MISMATCH')
        # A valid header alone does not prove the payload is complete.
        remaining = chunk.frame_count
        while remaining:
            wanted = min(remaining, 8192)
            if len(wav.readframes(wanted)) != wanted * chunk.channels * 2:
                raise ValueError('CHUNK_TRUNCATED')
            remaining -= wanted
    return path


def recover(database: Database, root: Path, session_id: str) -> dict:
    """Only verified finalized WAVs are adopted. Unknown/torn tails are retained."""
    validate_session_id(session_id)
    adopted = invalid = 0
    for sidecar in sorted(root.glob('chunk_*.json')):
        try:
            if sidecar.is_symlink():
                raise ValueError('SYMLINK_MANIFEST')
            if sidecar.stat().st_size > 16384:
                raise ValueError('MANIFEST_OVERSIZED')
            chunk = Chunk(**json.loads(sidecar.read_text()))
            verify_chunk(chunk, root, session_id)
            commit_chunk(database, chunk)
            adopted += 1
        except (ValueError, TypeError, KeyError, OSError, wave.Error):
            invalid += 1
    temporaries = sum(1 for _ in root.glob('*.tmp'))
    unmanifested = sum(1 for p in root.glob('chunk_*.wav') if not p.with_suffix('.json').exists())
    with database.connect() as conn:
        rows = conn.execute('SELECT * FROM capture_chunks WHERE session_id=? ORDER BY sequence',
                            (session_id,)).fetchall()
    bad_rows = 0
    for row in rows:
        try:
            sidecar = root / f'chunk_{row["sequence"]:06d}.json'
            chunk = Chunk(**json.loads(sidecar.read_text()))
            verify_chunk(chunk, root, session_id)
            if (row['sha256'] != chunk.checksum or row['path'] != chunk.path
                    or row['sequence'] != chunk.sequence or row['session_id'] != chunk.session_id
                    or row['frame_count'] != chunk.frame_count
                    or row['start_monotonic_ns'] != chunk.start_monotonic_ns
                    or row['end_monotonic_ns'] != chunk.end_monotonic_ns
                    or row['monotonic_clock_id'] != chunk.monotonic_clock_id):
                raise ValueError('DATABASE_CHECKSUM_MISMATCH')
        except (ValueError, TypeError, KeyError, OSError, wave.Error):
            bad_rows += 1
    sequences = [row['sequence'] for row in rows]
    continuous = sequences == list(range(len(sequences)))
    return dict(verified=adopted, invalid=invalid, temporary_files=temporaries,
                unmanifested_files=unmanifested, invalid_database_rows=bad_rows,
                sequence_continuous=continuous,
                clean=not (invalid or temporaries or unmanifested or bad_rows) and continuous)


class ChunkWriter:
    """Single consumer owns this writer. At most one chunk is resident on disk."""

    def __init__(self, database: Database, root: Path, session_id: str,
                 rate: int, channels: int, seconds: int = 20):
        if rate <= 0 or channels not in (1, 2) or not 10 <= seconds <= 30:
            raise ValueError('INVALID_CAPTURE_FORMAT')
        self.database, self.root, self.session_id = database, root.resolve(), session_id
        self.rate, self.channels, self.limit = rate, channels, rate * seconds
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with database.connect() as conn:
            self.sequence = conn.execute('SELECT coalesce(max(sequence), -1)+1 FROM capture_chunks WHERE session_id=?',
                                         (session_id,)).fetchone()[0]
        self.handle = self.wav = None
        self.frames = 0

    def _open(self, ns: int, wall: str, epoch: int) -> None:
        self.final = self.root / f'chunk_{self.sequence:06d}.wav'
        self.temporary = self.final.with_suffix('.wav.tmp')
        if self.final.exists() or self.final.with_suffix('.json').exists():
            raise ValueError('CHUNK_PATH_EXISTS')
        self.handle = self.temporary.open('xb')
        self.wav = wave.open(self.handle, 'wb')
        self.wav.setparams((self.channels, 2, self.rate, 0, 'NONE', 'not compressed'))
        self.started, self.wall, self.epoch = ns, wall, epoch
        self.frames = 0

    def append(self, pcm: bytes, ns: int, wall: str, epoch: int) -> list[Chunk]:
        width = self.channels * 2
        if not pcm or len(pcm) % width:
            raise ValueError('INVALID_PCM_LENGTH')
        offset = 0
        completed = []
        while offset < len(pcm) // width:
            if self.wav is None:
                delta = offset / self.rate
                self._open(ns + round(delta * 1e9),
                           (datetime.fromisoformat(wall) + timedelta(seconds=delta)).isoformat(), epoch)
            count = min(self.limit - self.frames, len(pcm) // width - offset)
            self.wav.writeframesraw(pcm[offset * width:(offset + count) * width])
            self.frames += count
            offset += count
            if self.frames == self.limit:
                completed.append(self.seal())
        return completed

    def seal(self) -> Chunk | None:
        if self.wav is None:
            return None
        self.wav.close()  # patch header before fsync; external handle remains open
        self.wav = None
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.handle = None
        duration = self.frames / self.rate
        chunk = Chunk(f'{self.session_id}:{self.sequence}', self.session_id, self.sequence,
                      self.epoch, self.started, self.started + round(duration * 1e9), self.wall,
                      (datetime.fromisoformat(self.wall) + timedelta(seconds=duration)).isoformat(),
                      self.rate, self.channels, 'PCM_S16LE', self.frames, str(self.final),
                      checksum(self.temporary), self.temporary.stat().st_size,
                      monotonic_clock_id=MONOTONIC_CLOCK_ID)
        # Manifest-before-rename permits exact verification after a DB-commit crash.
        atomic_json(self.final.with_suffix('.json'), asdict(chunk))
        os.replace(self.temporary, self.final)
        sync_directory(self.root)
        verify_chunk(chunk, self.root, self.session_id)
        commit_chunk(self.database, chunk)
        self.sequence += 1
        self.frames = 0
        return chunk

    def abandon(self) -> None:
        """Release handles on failure; leave uncommitted tail for inspection, not replay."""
        try:
            if self.wav:
                self.wav.close()
        finally:
            self.wav = None
            if self.handle:
                self.handle.close()
                self.handle = None
