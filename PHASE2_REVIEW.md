# CLASSROOM INTERPRETER — PHASE 2 REVIEW

Date: 2026-09-06. This is an interim evidence report, not an accepted phase or a Git commit.

## Status

FAIL — acceptance gates are incomplete. User explicitly deferred the real three-hour
recording and requested short tests first. Do not enter Phase 3.

## Selected Audio Backend

Candidate: sounddevice 0.5.6 / PortAudio 19.7.0-devel / CoreAudio, Python 3.13.13,
macOS 15.7.4 arm64, built-in MacBook Air microphone. SoXR 1.1.0 HQ normalization,
NumPy 2.5.2, psutil 7.2.2. This is **not a production-accepted adapter**.

## Why

The native Python route enumerates the target microphone and delivers actual frames.
Rolling durable PCM and deterministic resampling work in current short tests. There
is no evidence yet requiring Swift, but insufficient evidence to accept Python for
production. PyAudio's shared PortAudio base is not presumed to fix unseen failures.

## Microphone Permission

- Sandboxed enumeration returned zero devices; explicitly approved host enumeration
  returned CoreAudio devices and default input index 2, MacBook Air microphone.
- Three-second permission/callback probe received 131,584 frames, zero status callbacks.
- Host microphone access is currently granted enough for capture. First-run prompt,
  denial, revoke-while-running and permission-change behavior were **not tested**.
- No TCC settings were reset or changed. Development-host success is not packaged-app
  permission acceptance; Codex/Terminal/Python attribution remains to be manually checked.

## Capture Format

Native PCM signed 16-bit little-endian, mono, lossless WAV; no lossy encoding.

## Device Sample Rate

44,100 Hz verified on this built-in device. Synthetic stress: 48,000 Hz.

## Internal Sample Rate

16,000 Hz mono signed PCM16, streaming SoXR HQ. Input interface is pull-based
`normalized_chunks`, only reading committed chunks. No semantic segmentation yet.

## Recovery Chunk Duration

20 seconds, final partial chunk sealed on pause/stop. Config limited to 10–30 seconds.

## Durability Boundary

AUDIO_CAPTURED requires finalized WAV header, file flush/fsync, checksum, durable
sidecar, atomic WAV rename and directory fsync, read-back validation, then SQLite WAL
metadata commit with synchronous FULL. Pending memory and `.tmp` files are not valid
captured audio. No power-loss/SSD-controller guarantee beyond these OS primitives is claimed.
Sidecars preserve the rename/DB crash window; only exact validated files are adopted.

## Hardware Tests

- 30-second pause/resume: PASS on the second run; 26.36 seconds of audio over
  26.386 seconds active expected capture, three chunks, zero gap/overrun, all readable.
- First 30-second run: retained FAIL. Its expected-duration metric incorrectly
  included device startup; fixed by separating active intervals, startup and drain.
- 5 min: IN PROGRESS at report creation; update below after completion.
- 30 min: NOT RUN; remains an acceptance blocker.
- 3 hour: DEFERRED by user, not run and not substituted by simulation.

## 3-Hour Metrics

Real wall clock duration, captured audio duration, initial/final/peak RSS, queue high
watermark, callback overruns, gap count/total/largest, chunk count and errors:
**NOT MEASURED — real three-hour test deferred.**

## Accelerated Test

Initial three-hour equivalent: 10,800.000 seconds, 518,400,000 native frames,
172,800,000 normalized frames, 540 readable verified chunks, continuous sequences,
zero callback overruns, zero gap, zero exceptions. Elapsed 17.818 seconds; queue
high watermark 64/128 (synthetic pacing), initial RSS 49.969 MiB, final 25.016 MiB,
peak 51.297 MiB. A short accelerated RSS slope is not a long-runtime stability claim.
Final-source rerun is recorded below when complete.

## Pause / Resume

Deterministic and real short test passed. Pause drains/seals, closes microphone,
persists an intentional interval; resume opens the same device identity and a new
epoch. No silence is fabricated. Public async lifecycle uses serialized ownership;
cancelled start closes any microphone it opened.

## Device Loss

Simulated unavailable/stop failure, queue pressure and writer exceptions fail closed.
Device inactivity or two-second callback stall is monitored by the CLI controller.
No silent device switching. Physical device loss/reconnect remains NOT TESTED.
Fault/restart missing duration can be unknown: counters expose lower-bound status.

## Sleep / Wake

Verified `pmset -g assertions` during the short run: caffeinate's
PreventUserIdleSystemSleep assertion on behalf of capture PID 52896, plus the
CoreAudio input assertion. Display sleep, actual lid close, manual sleep and wake
were NOT TESTED; require user coordination. No claim of recording during sleep.

## Crash Recovery

Deterministic rename-before-DB adoption, corruption rejection, previous-final-chunk
replay after real subprocess SIGKILL, orphan retention/restart blocking, SIGINT and
SIGTERM tail sealing all passed. Unknown `.tmp` files remain preserved and explicitly
block automated restart; there is no automatic tail deletion or content purge.

## Tests Run

Repository working directory: `/Users/shh/Documents/ChatGPT/STUDY`.

```
UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv sync --extra dev --extra audio
PYTHONPATH=backend UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv run --extra dev --extra audio pytest
UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv lock --check --offline
PYTHONPATH=backend UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv run --extra audio python -m compileall -q backend
PYTHONPATH=backend UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv run --extra audio python -m classroom_interpreter.audio synthetic --root /private/tmp/classroom-phase2-final-synthetic --seconds 10800
PYTHONPATH=backend UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv run --extra audio python -m classroom_interpreter.audio record --root /private/tmp/classroom-phase2-hardware --seconds 30 --pause-at 5 --pause-seconds 3
PYTHONPATH=backend UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv run --extra audio python -m classroom_interpreter.audio record --root /private/tmp/classroom-phase2-hardware --seconds 300
```

Current automated result: 55 passed, 1 warning (upstream Starlette uses deprecated
AnyIO BlockingPortal alias). Compileall passed. Offline lock check: 32 packages,
passed. Targeted key/private-key-pattern scan: zero matching files. Runtime/secret
ignore probes passed. Full Git status: repository files remain untracked; there is
no accepted baseline commit and no Git commit was created. `git diff --check` is
limited because untracked files are not covered; code was directly inspected/tested.

Evidence classes: local deterministic, synthetic subprocess, target macOS short
capture. No simulated-provider/real MiMo evidence in Phase 2. Frontend unchanged;
no new frontend acceptance claim.

## ADR

D-024 in DECISIONS.md is candidate / NOT ACCEPTED. Final choice awaits complete
hardware, permissions, reconnect, sleep and independent review gates.

## Architecture Deviations

- Durable downstream availability is chunk-granular (up to 20 seconds), not claimed
  low-latency realtime ASR. Low-latency persistence design requires a later decision.
- Phase 2 adds migration 002, not a rewrite of Phase 1 migration 001. Added capture
  metadata/events and explicit SQLite connection closure to prevent handle retention.
- CLI owns the active capture instance. Web `/health/capture` only reports an explicitly
  injected instance; the default Phase 1 web shell does not control the separate CLI.

## Known Issues

- Editable installed-module resolution failed on this Mac; explicit PYTHONPATH works.
  Hidden `.pth` flags were observed. Packaging remains unresolved, not masked as PASS.
- Python queue `put_nowait` avoids waiting for space; CPython/PortAudio does not promise
  hard realtime scheduling. Callback stability needs the deferred real long run.
- Independent Reviewer returned lifecycle/cancellation/gap findings; regression repairs
  were implemented. Its final re-review was interrupted by agent usage limits.
- The author ran validation; an independent Validator/Oracle final sign-off is absent.
- Fault classification is intentionally content-free but several writer/backend failures
  share a broad reason code; detailed payload/error text is never logged.

## Blocking Issues

Real 30-minute and three-hour tests, permission-change/device reconnect/sleep-wake
matrix, final independent Reviewer/Validator/Oracle gates, and final adapter ADR.
No PASS or production-readiness claim while any gate remains open.

## Files Changed

Phase 2 scope: pyproject.toml, uv.lock, .gitignore, AGENTS.md, DECISIONS.md,
ARCHITECTURE.md, TEST_PLAN.md, README.md, this report; migration 002;
audio/{__init__,__main__,durable,listener,normalize}.py; storage/database.py;
api/health.py; tests/conftest.py and integration/test_schema.py; new audio/failure
tests and hardware protocol. Phase 1 code/assets are preserved; no frontend redesign.

## Privacy and Purge Invariants

No network/provider call, no audio/text in ordinary logs, no purge implementation.
Actual recordings remain in private temporary test roots outside Git, with private
permissions; they have NOT been deleted or uploaded. Content-free metric/report JSON
is alongside them. PERSIST FIRST and NO VALID SUMMARY = NO PURGE remain intact.

## Recommended Next Phase

Remain in Phase 2 to finish deferred gates. Only after approval:
Phase 3 — Segmentation + Real MiMo ASR Integration. Do not start it now.
