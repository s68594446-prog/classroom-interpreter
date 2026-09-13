import threading
import time

import pytest

from classroom_interpreter.audio.listener import AudioListener


def test_slow_writer_bounded_overflow(database, tmp_path, monkeypatch):
    from classroom_interpreter.audio.durable import ChunkWriter
    original = ChunkWriter.append
    entered, release = threading.Event(), threading.Event()
    def slow(self, *args):
        entered.set()
        release.wait(3)
        return original(self, *args)
    monkeypatch.setattr(ChunkWriter, 'append', slow)
    listener = AudioListener(database, tmp_path, rate=16000, synthetic=True, queue_blocks=1)
    listener.begin('pressure')
    origin = time.monotonic_ns()
    assert listener.submit(b'\0\0'*320, 320, origin)
    assert entered.wait(2)
    assert listener.submit(b'\0\0'*320, 320, origin+20_000_000)
    assert not listener.submit(b'\0\0'*320, 320, origin+40_000_000)
    release.set()
    listener.close()
    assert listener.fault == 'QUEUE_OVERFLOW'
    assert listener.high_water == 1
    assert listener.gaps == 1
    assert listener.state == 'RECOVERY_REQUIRED'


@pytest.mark.parametrize('stage', ['append', 'seal', 'normalizer'])
def test_consumer_backend_exception_is_visible(database, tmp_path, monkeypatch, stage):
    import classroom_interpreter.audio.listener as module
    from classroom_interpreter.audio.durable import ChunkWriter
    def broken(*args, **kwargs):
        raise OSError('synthetic failure')
    if stage == 'normalizer':
        monkeypatch.setattr(module, 'Normalizer', broken)
    else:
        monkeypatch.setattr(ChunkWriter, stage, broken)
    listener = AudioListener(database, tmp_path, rate=16000, synthetic=True)
    listener.begin('broken-'+stage)
    listener.submit(b'\0\0'*320, 320, time.monotonic_ns())
    listener.close()
    assert listener.fault == 'WRITER_OR_NORMALIZER_FAILED'
    assert listener.state == 'RECOVERY_REQUIRED'
    assert listener.chunks == 0
    assert listener.worker is None


def test_low_disk_stops_without_valid_chunk(database, tmp_path):
    listener = AudioListener(database, tmp_path, synthetic=True, min_free_bytes=10**20)
    listener.begin('low-disk')
    listener.submit(b'\0\0'*320, 320, time.monotonic_ns())
    listener.close()
    assert listener.fault
    assert listener.committed == 0


def test_device_stop_failure_still_closes_and_records(database, tmp_path):
    class BrokenDevice:
        closed = False
        def abort(self):
            raise RuntimeError('unplugged')
        def close(self):
            self.closed = True
    listener = AudioListener(database, tmp_path, synthetic=True)
    listener.begin('device-stop')
    device = listener.stream = BrokenDevice()
    listener.close()
    assert device.closed
    assert listener.fault == 'DEVICE_ABORT_FAILED'
    assert listener.state == 'RECOVERY_REQUIRED'
    assert listener.lock_handle is None


def test_device_unavailable_is_not_silence(database, tmp_path, monkeypatch):
    listener = AudioListener(database, tmp_path)
    def unavailable(*a, **kw):
        raise RuntimeError('unavailable')
    monkeypatch.setattr('sounddevice.query_devices', unavailable)
    with pytest.raises(RuntimeError):
        listener.begin('unavailable')
    assert listener.fault == 'CAPTURE_START_FAILED'
    assert listener.received == 0
    assert listener.repository.get_session('unavailable').status.value == 'RECOVERY_REQUIRED'


def test_cancelled_async_start_releases_microphone_owner(database, tmp_path, monkeypatch):
    import asyncio
    listener = AudioListener(database, tmp_path, synthetic=True)
    original = listener.begin
    def delayed(session_id):
        time.sleep(.05)
        original(session_id)
    monkeypatch.setattr(listener, 'begin', delayed)
    async def scenario():
        task = asyncio.create_task(listener.start('cancelled'))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert listener.state == 'STOPPED'
    assert listener.lock_handle is None


def test_parallel_pause_stop_serialized(database, tmp_path):
    listener = AudioListener(database, tmp_path, synthetic=True)
    listener.begin('parallel')
    errors = []
    def pause():
        try:
            listener.pause_capture()
        except RuntimeError:
            pass  # Stop won lifecycle race: pause is rejected, no second writer.
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=pause)
    thread.start()
    listener.close()
    thread.join()
    assert not errors
    assert listener.state == 'STOPPED'
    assert listener.worker is None


def test_rejected_owner_cannot_mutate_active_session(database, tmp_path):
    first = AudioListener(database, tmp_path, synthetic=True)
    second = AudioListener(database, tmp_path, synthetic=True)
    first.begin('same')
    with pytest.raises(RuntimeError, match='OWNED'):
        second.begin('same')
    second.close()
    assert first.repository.get_session('same').status.value == 'RECORDING'
    assert first.accepting and not second.owns_capture
    first.close()


def test_check_is_serialized_with_pause(database, tmp_path):
    listener = AudioListener(database, tmp_path, synthetic=True)
    listener.begin('serialized-check')
    listener.pause_capture()
    assert listener.check()
    listener.close()


class _Status:
    """A PortAudio callback status, the shape sounddevice hands the callback."""

    def __init__(self, *, input_overflow=False, input_underflow=False):
        self.input_overflow = input_overflow
        self.input_underflow = input_underflow

    def __bool__(self):
        return self.input_overflow or self.input_underflow


class _Timing:
    inputBufferAdcTime = 0.0
    currentTime = 0.0


def _driveable(database, tmp_path):
    listener = AudioListener(database, tmp_path, rate=16000, synthetic=True)
    listener.begin('overflow')
    return listener


def test_an_input_overflow_does_not_end_the_class(database, tmp_path):
    """PortAudio discards samples whenever this process is briefly behind -- a
    GC pause, a disk write, another application taking the CPU. On a laptop
    running a three-hour class that is an ordinary event.

    It used to set a fatal fault and abort the stream, which is the same
    mistake already corrected one layer down for ADC discontinuities: the
    samples are gone either way, and stopping capture does not recover them. It
    only destroys the rest of the class to preserve the continuity of the part
    already written. The gap is still sealed, timestamped and counted by the
    consumer, so the record does not pretend the audio was there.

    Mutation that turns this red: fault on any status, as before.
    """
    listener = _driveable(database, tmp_path)
    try:
        listener._callback(b"\0\0" * 320, 320, _Timing(), _Status(input_overflow=True))

        assert listener.fault is None, f"a routine overflow killed capture: {listener.fault}"
        assert listener.accepting is True, "capture stopped accepting audio"
        assert listener.overruns == 1, "the overflow was not counted"
    finally:
        listener.close()


def test_a_stream_that_keeps_overflowing_is_still_a_fault(database, tmp_path):
    """Tolerance is not blindness. Past the same budget the gap tolerance uses,
    the device really is failing.

    Mutation that turns this red: drop the `self.overruns <=
    self.max_tolerated_gaps` half of the condition.
    """
    listener = _driveable(database, tmp_path)
    listener.max_tolerated_gaps = 3
    try:
        with pytest.raises(Exception):
            for _ in range(5):
                listener._callback(
                    b"\0\0" * 320, 320, _Timing(), _Status(input_overflow=True)
                )

        assert listener.fault == 'CALLBACK_STATUS'
        assert listener.accepting is False
    finally:
        listener.close()


def test_a_status_that_is_not_an_overflow_is_still_fatal(database, tmp_path):
    """Only the discarded-samples case is survivable; anything else is a device
    telling us something we do not understand.

    Mutation that turns this red: tolerate any status, not only
    `input_overflow`.
    """
    listener = _driveable(database, tmp_path)
    try:
        with pytest.raises(Exception):
            listener._callback(
                b"\0\0" * 320, 320, _Timing(), _Status(input_underflow=True)
            )

        assert listener.fault == 'CALLBACK_STATUS'
    finally:
        listener.close()
