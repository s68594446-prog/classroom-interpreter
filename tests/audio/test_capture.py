import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from classroom_interpreter.audio.durable import Chunk, ChunkWriter, checksum, recover, verify_chunk
from classroom_interpreter.audio.listener import AudioListener
from classroom_interpreter.audio.normalize import Normalizer, normalized_chunks


def writer_for(database, repository, tmp_path, *, rate=16000, channels=1, seconds=20):
    repository.create_session('synthetic', app_version='test', session_id='audio-test')
    # 20s explicitly: these tests assert rollover arithmetic (320000 frames per
    # chunk), which is about the writer's boundary behaviour and not about what
    # the product should default to. The default is now 2s for latency.
    return ChunkWriter(database, tmp_path / 'audio-test', 'audio-test', rate, channels, seconds)


def test_rollover_metadata_and_replay(database, repository, tmp_path):
    writer = writer_for(database, repository, tmp_path)
    chunks = writer.append(b'\x01\x00' * (16000 * 41), 1000000000,
                           '2026-09-06T00:00:00+00:00', 0)
    chunks.append(writer.seal())
    assert [c.frame_count for c in chunks] == [320000, 320000, 16000]
    assert [Path(c.path).name for c in chunks] == ['chunk_000000.wav', 'chunk_000001.wav', 'chunk_000002.wav']
    assert chunks[0].end_monotonic_ns == chunks[1].start_monotonic_ns
    assert chunks[0].wall_clock_end == chunks[1].wall_clock_start
    assert all(c.status == 'AUDIO_CAPTURED' for c in chunks)
    with database.connect() as conn:
        assert [tuple(r) for r in conn.execute('SELECT start_ms,end_ms FROM capture_chunks ORDER BY sequence')] == [(0, 20000), (20000, 40000), (40000, 41000)]
    for c in chunks:
        verify_chunk(c, writer.root, 'audio-test')
        assert c.checksum == hashlib.sha256(Path(c.path).read_bytes()).hexdigest()
    assert recover(database, writer.root, 'audio-test')['clean']
    normalized = list(normalized_chunks(iter(chunks)))
    assert sum(len(f.pcm) // 2 for f in normalized) == 41 * 16000
    assert all(f.channels == 1 and f.sample_rate == 16000 for f in normalized)


@pytest.mark.parametrize('rate', [44100, 48000])
def test_resampling_deterministic_and_streaming(rate):
    t = np.arange(rate) / rate
    source = (np.sin(2*np.pi*440*t) * 8000).astype('<i2')
    mono = Normalizer(rate, 1)
    whole = mono.process(source.tobytes(), last=True)
    stream = Normalizer(rate, 1)
    parts = b''.join(stream.process(p.tobytes()) for p in np.array_split(source, 137))
    parts += stream.process(b'', last=True)
    assert whole == parts
    assert len(whole) == 16000 * 2
    stereo = np.column_stack([source, -source]).astype('<i2')
    cancelled = Normalizer(rate, 2).process(stereo.tobytes(), last=True)
    assert np.count_nonzero(np.frombuffer(cancelled, dtype='<i2')) == 0


def test_no_database_visibility_before_seal(database, repository, tmp_path):
    writer = writer_for(database, repository, tmp_path)
    writer.append(b'\0\0' * 100, 1000000000, '2026-09-06T00:00:00+00:00', 0)
    with database.connect() as conn:
        assert conn.execute('SELECT count(*) FROM capture_chunks').fetchone()[0] == 0
    assert not list(writer.root.glob('*.wav'))
    writer.abandon()
    result = recover(database, writer.root, 'audio-test')
    assert not result['clean'] and result['temporary_files'] == 1


def test_recover_rename_before_commit(database, repository, tmp_path, monkeypatch):
    import classroom_interpreter.audio.durable as durable
    writer = writer_for(database, repository, tmp_path)
    writer.append(b'\0\0' * 100, 1000000000, '2026-09-06T00:00:00+00:00', 0)
    original = durable.commit_chunk
    monkeypatch.setattr(durable, 'commit_chunk', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        writer.seal()
    monkeypatch.setattr(durable, 'commit_chunk', original)
    assert recover(database, writer.root, 'audio-test')['clean']
    assert recover(database, writer.root, 'audio-test')['verified'] == 1
    restarted = ChunkWriter(database, writer.root, 'audio-test', 16000, 1, 20)
    assert restarted.sequence == 1


def test_corrupt_and_foreign_manifest_rejected(database, repository, tmp_path):
    writer = writer_for(database, repository, tmp_path)
    writer.append(b'\0\0' * 100, 1000000000, '2026-09-06T00:00:00+00:00', 0)
    chunk = writer.seal()
    with pytest.raises(ValueError):
        verify_chunk(replace(chunk, session_id='foreign'), writer.root, 'audio-test')
    Path(chunk.path).write_bytes(b'not wav')
    result = recover(database, writer.root, 'audio-test')
    assert not result['clean'] and result['invalid_database_rows'] == 1


def test_database_context_releases_connection(database):
    with database.connect() as conn:
        conn.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def feed(listener, seconds=.1, origin=None):
    count = round(listener.rate * seconds)
    assert listener.submit(b'\0\0' * count, count, origin or time.monotonic_ns())


def test_pause_resume_epochs_and_no_silence(database, tmp_path):
    listener = AudioListener(database, tmp_path, rate=16000, synthetic=True)
    listener.begin('pause-test')
    feed(listener)
    listener.pause_capture()
    before = listener.committed
    assert not listener.submit(b'\0\0', 1, time.monotonic_ns())
    listener.resume_capture()
    feed(listener)
    listener.close()
    assert listener.state == 'STOPPED'
    assert listener.committed == before * 2 == 3200
    with database.connect() as conn:
        assert [r[0] for r in conn.execute('SELECT capture_epoch FROM capture_chunks ORDER BY sequence')] == [0, 1]
        assert conn.execute("SELECT count(*) FROM capture_events WHERE kind='pause'").fetchone()[0] == 2


def test_gap_is_explicit_and_fail_closed(database, tmp_path):
    listener = AudioListener(database, tmp_path, rate=16000, synthetic=True)
    listener.begin('gap-test')
    origin = time.monotonic_ns()
    feed(listener, origin=origin)
    feed(listener, origin=origin + 2_000_000_000)
    listener.close()
    assert listener.state == 'RECOVERY_REQUIRED'
    assert listener.gaps == 1
    assert listener.gap_ns == 1_900_000_000
    assert listener.committed == 1600


def test_session_ownership_and_async_interface(database, tmp_path):
    import asyncio
    first = AudioListener(database, tmp_path, synthetic=True)
    second = AudioListener(database, tmp_path, synthetic=True)
    asyncio.run(first.start('one'))
    with pytest.raises(RuntimeError, match='OWNED'):
        second.begin('two')
    with pytest.raises(ValueError, match='MISMATCH'):
        asyncio.run(first.stop('two'))
    asyncio.run(first.stop('one'))


def test_wall_clock_independent_ordering(database, repository, tmp_path):
    writer = writer_for(database, repository, tmp_path)
    first = writer.append(b'\0\0' * 320000, 1_000_000_000, '2026-09-06T00:00:00+00:00', 0)[0]
    second = writer.append(b'\0\0' * 320000, 21_000_000_000, '2020-01-01T00:00:00+00:00', 0)[0]
    assert second.start_monotonic_ns == first.end_monotonic_ns
    assert second.sequence == first.sequence + 1


def test_different_monotonic_clock_does_not_fabricate_restart_gap(database, repository, tmp_path):
    writer = writer_for(database, repository, tmp_path)
    first = writer.append(b'\0\0' * 320000, 1_000_000_000,
                          '2026-09-06T00:00:00+00:00', 0)[0]
    from dataclasses import replace
    foreign = replace(first, chunk_id='audio-test:1', sequence=1,
                      start_monotonic_ns=9_000_000_000_000,
                      end_monotonic_ns=9_020_000_000_000,
                      path=str(writer.root / 'chunk_000001.wav'),
                      monotonic_clock_id='different-boot')
    import shutil
    shutil.copyfile(first.path, foreign.path)
    object.__setattr__(foreign, 'checksum', checksum(Path(foreign.path)))
    from classroom_interpreter.audio.durable import atomic_json, commit_chunk
    atomic_json(Path(foreign.path).with_suffix('.json'), __import__('dataclasses').asdict(foreign))
    commit_chunk(database, foreign)
    with database.connect() as conn:
        assert tuple(conn.execute('SELECT start_ms,end_ms FROM capture_chunks WHERE sequence=1').fetchone()) == (20000, 40000)


def test_legacy_manifest_without_clock_remains_recoverable(database, repository, tmp_path):
    writer = writer_for(database, repository, tmp_path)
    writer.append(b'\0\0' * 16000, 1_000_000_000,
                  '2026-09-06T00:00:00+00:00', 0)
    chunk = writer.seal()
    sidecar = Path(chunk.path).with_suffix('.json')
    value = json.loads(sidecar.read_text())
    value.pop('monotonic_clock_id')
    sidecar.write_text(json.dumps(value))
    with database.transaction() as conn:
        conn.execute('UPDATE capture_chunks SET monotonic_clock_id=NULL WHERE id=?',
                     (chunk.chunk_id,))
    assert recover(database, writer.root, 'audio-test')['clean']
