from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
import signal
import time
import uuid
from pathlib import Path

from classroom_interpreter.storage import Database
from .durable import atomic_json, recover, validate_session_id
from .listener import AudioListener


def main() -> int:
    parser = argparse.ArgumentParser(description='Phase 2 audio-only spike; no providers or purge')
    parser.add_argument('command', choices=['devices', 'record', 'synthetic', 'recover'])
    parser.add_argument('--root', type=Path, default=Path.home() / 'Library/Application Support/Classroom Interpreter/audio-spike')
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--session', default=None)
    parser.add_argument('--device', type=int)
    parser.add_argument('--rate', type=int, default=48000, help='Synthetic only; hardware uses native rate')
    parser.add_argument('--pause-at', type=float)
    parser.add_argument('--pause-seconds', type=float, default=2)
    args = parser.parse_args()
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in Path(__file__).parent.glob('*.py')}
    if args.command == 'devices':
        import sounddevice as sd
        print(json.dumps(dict(hostapis=sd.query_hostapis(), devices=list(sd.query_devices()),
                              default=list(sd.default.device)), ensure_ascii=False))
        return 0
    if args.seconds <= 0 or args.rate <= 0:
        parser.error('seconds and rate must be positive')
    os.umask(0o077)
    args.root = args.root.resolve()
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = Database(args.root / 'capture.sqlite3')
    db.migrate()
    session = args.session or str(uuid.uuid4())
    validate_session_id(session)
    if args.command == 'recover':
        if not args.session:
            parser.error('recover requires --session')
        with (args.root / 'capture.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = recover(db, args.root / session, session)
        print(json.dumps(result))
        return 0 if result['clean'] else 1
    listener = AudioListener(db, args.root, device=args.device,
                             rate=args.rate if args.command == 'synthetic' else None,
                             synthetic=args.command == 'synthetic')
    stop = False
    def request_stop(signum, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    samples = count = sx = sy = sxx = sxy = 0
    initial = final = peak = 0
    errors = []
    started_ok = False
    started = time.monotonic()
    run_id = str(uuid.uuid4())
    report = args.root / session / f'report_{run_id}.json'
    try:
        listener.begin(session)
        started_ok = True
        capture_started = time.monotonic()
        print(json.dumps(dict(event='started', session_id=session, pid=os.getpid(),
                              evidence='synthetic' if listener.synthetic else 'target_macos',
                              device=listener.device_info['name'], sample_rate=listener.rate)), flush=True)
        with (args.root / session / f'metrics_{run_id}.jsonl').open('x') as metrics:
            def sample():
                nonlocal samples, count, sx, sy, sxx, sxy, initial, final, peak
                data = listener.snapshot()
                x = time.monotonic() - started
                y = data['memory_rss_mb']
                if not count:
                    initial = y
                final, peak = y, max(peak, y)
                count += 1
                sx += x
                sy += y
                sxx += x*x
                sxy += x*y
                metrics.write(json.dumps(data) + '\n')
                metrics.flush()
                samples += 1
            sample()
            last_sample = time.monotonic()
            if listener.synthetic:
                # Only the fake source paces on queue depth; hardware NEVER waits.
                import numpy as np
                block_frames = round(listener.rate * .02)
                phase = np.arange(block_frames) / listener.rate
                pcm = (np.sin(2 * np.pi * 440 * phase) * 4000).astype('<i2').tobytes()
                emitted, target = 0, round(args.seconds * listener.rate)
                origin = time.monotonic_ns()
                while emitted < target and not stop and listener.check():
                    if listener.buffers.qsize() >= listener.buffers.maxsize // 2:
                        time.sleep(.001)
                        continue
                    n = min(block_frames, target - emitted)
                    if not listener.submit(pcm[:n*2], n, origin + round(emitted/listener.rate*1e9)):
                        break
                    emitted += n
                    if time.monotonic() - last_sample >= 1:
                        sample()
                        last_sample = time.monotonic()
            else:
                paused = False
                while not stop and time.monotonic() - capture_started < args.seconds:
                    if not listener.check():
                        break
                    if args.pause_at is not None and not paused and time.monotonic() - capture_started >= args.pause_at:
                        listener.pause_capture()
                        pause_end = time.monotonic() + args.pause_seconds
                        while time.monotonic() < pause_end and not stop:
                            time.sleep(.05)
                        if not stop:
                            listener.resume_capture()
                        paused = True
                    if time.monotonic() - last_sample >= 5:
                        sample()
                        last_sample = time.monotonic()
                    time.sleep(.05)
            listener.close()
            sample()
    except Exception as exc:
        # Never print exception messages from audio libraries (could include content/path).
        errors.append(type(exc).__name__)
        try:
            listener.close()
        except Exception as close_exc:
            errors.append(type(close_exc).__name__)
    result = listener.snapshot()
    result.update(session_id=session, evidence='synthetic' if listener.synthetic else 'target_macos',
                  source_sha256=source_hashes,
                  requested_duration=args.seconds, interrupted=stop, exceptions=errors,
                  initial_rss_mb=initial, final_rss_mb=final, peak_rss_mb=peak,
                  rss_slope_mb_per_hour=(3600*(count*sxy-sx*sy)/(count*sxx-sx*sx)
                                         if count*sxx > sx*sx else None), metric_samples=samples)
    verification = (recover(db, args.root / session, session) if started_ok else {
        'verified': 0, 'invalid': 0, 'temporary_files': 0,
        'unmanifested_files': 0, 'invalid_database_rows': 0,
        'sequence_continuous': False, 'clean': False,
        'scan_skipped': 'CAPTURE_OWNERSHIP_NOT_ACQUIRED'})
    result['recovery_verification'] = verification
    if listener.synthetic:
        result['expected_audio_duration'] = args.seconds
    result['duration_deficit_seconds'] = result['expected_audio_duration'] - result['audio_seconds_captured']
    result['capture_test_pass'] = bool(not errors and not listener.fault and not stop
        and result['audio_seconds_captured'] > 0 and verification['clean']
        and result['callback_overrun_count'] == 0 and result['gap_count'] == 0
        and abs(result['duration_deficit_seconds']) < 1)
    if started_ok:
        atomic_json(report, result)
    print(json.dumps(result), flush=True)
    return 0 if result['capture_test_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
