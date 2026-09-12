import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from classroom_interpreter.audio.durable import recover
from classroom_interpreter.audio.listener import AudioListener
from classroom_interpreter.storage import Database


# The child submits 1000 blocks of 320 frames at 16kHz = exactly 20 seconds.
# Capture chunks default to 2 seconds (they used to be hard-coded to 20, which
# put ~10s of latency in front of every caption), so that audio now seals as
# ten chunks rather than one. Multi-chunk recovery is the realistic case, so
# the test exercises it rather than pinning the old single-chunk arithmetic.
CHUNK_SECONDS = 2
SUBMITTED_SECONDS = 20
EXPECTED_CHUNKS = SUBMITTED_SECONDS // CHUNK_SECONDS


CHILD = '''
import sys,time
from pathlib import Path
from classroom_interpreter.storage import Database
from classroom_interpreter.audio.listener import AudioListener
root=Path(sys.argv[1])
db=Database(root/'capture.sqlite3'); db.migrate()
a=AudioListener(db,root,rate=16000,synthetic=True,chunk_seconds=2)
a.begin('crash')
origin=time.monotonic_ns()
for i in range(1000):
    while a.buffers.qsize()>30: time.sleep(.001)
    a.submit(b'\\0\\0'*320,320,origin+i*20000000)
a.buffers.join()
print('SEALED',flush=True)
if sys.argv[2]=='tail':
    a.submit(b'\\0\\0'*320,320,origin+20000000000)
    a.buffers.join()
print('READY',flush=True)
time.sleep(30)
'''


@pytest.mark.parametrize('tail', [False, True])
def test_sigkill_previous_chunks_recoverable(database, tmp_path, tail):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'backend'))
    child = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path), 'tail' if tail else 'sealed'],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # communicate timeout avoids indefinite test wait if child startup fails.
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), 'child did not finalize first chunk'
        assert child.stdout.readline().strip() == 'SEALED'
        # second line may already reside in Python's buffered reader.
        assert child.stdout.readline().strip() == 'READY'
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
    db = Database(tmp_path / 'capture.sqlite3')
    result = recover(db, tmp_path / 'crash', 'crash')
    assert result['verified'] == EXPECTED_CHUNKS
    assert result['invalid_database_rows'] == 0
    assert result['temporary_files'] == int(tail)
    restarted = AudioListener(db, tmp_path, synthetic=True, rate=16000)
    if tail:
        with pytest.raises(RuntimeError, match='UNVERIFIED_RECOVERY_TAIL'):
            restarted.begin('crash')
    else:
        restarted.begin('crash')
        restarted.submit(b'\0\0'*320, 320, time.monotonic_ns())
        restarted.close()
        with db.connect() as conn:
            sequences = [r[0] for r in conn.execute(
                'SELECT sequence FROM capture_chunks ORDER BY sequence')]
            # The property is continuity, not a specific count: the restarted
            # listener must resume numbering after the recovered chunks with
            # no gap and no duplicate, whatever the chunk length happens to be.
            assert sequences == list(range(EXPECTED_CHUNKS + 1))


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
def test_cli_signal_finalizes_tail(tmp_path, signum):
    # Accelerated producer still running long enough to interrupt after startup.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'backend'))
    process = subprocess.Popen([sys.executable, '-m', 'classroom_interpreter.audio', 'synthetic',
                               '--root', str(tmp_path), '--session', 'signal-test', '--seconds', '10800'],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10)
        assert json.loads(process.stdout.readline())['event'] == 'started'
        time.sleep(.1)
        process.send_signal(signum)
        out, err = process.communicate(timeout=15)
        result = json.loads(out.strip())
        assert result['interrupted'] is True
        assert result['reason_code'] is None
        assert result['state'] == 'STOPPED'
        assert result['recovery_verification']['clean']
        assert result['audio_seconds_captured'] > 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_cli_lock_rejection_neither_scans_nor_mutates_session(database, tmp_path):
    listener = AudioListener(database, tmp_path, synthetic=True)
    listener.begin('owned')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'backend'))
    process = subprocess.run([sys.executable, '-m', 'classroom_interpreter.audio', 'synthetic',
                              '--root', str(tmp_path), '--session', 'owned', '--seconds', '1'],
                             env=env, capture_output=True, text=True, timeout=10)
    result = json.loads(process.stdout.strip())
    assert process.returncode == 1
    assert result['recovery_verification']['scan_skipped'] == 'CAPTURE_OWNERSHIP_NOT_ACQUIRED'
    assert listener.repository.get_session('owned').status.value == 'RECORDING'
    assert not list((tmp_path / 'owned').glob('report_*.json'))
    listener.close()
