# Phase 2 target-Mac hardware protocol (explicit opt-in)

No test under ordinary `pytest` opens a microphone. Hardware captures are launched
explicitly with the audio CLI, produce private WAV recovery chunks, and require
the macOS host's microphone consent. No content is printed or sent to a provider.

Run from the repository with the audio extra installed:

```
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio devices
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --seconds 300
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --seconds 1800
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --seconds 10800
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --seconds 30 --pause-at 5 --pause-seconds 3
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio synthetic --seconds 10800
```

Use a distinct private `--root` to isolate each experiment if needed. The default
root is outside Git under Application Support/Classroom Interpreter/audio-spike.
`report_RUN-ID.json` and `metrics_RUN-ID.jsonl` contain counters/timing only; `chunk_*.wav` is
sensitive. Do not commit, print, or upload audio. No automatic deletion is provided.

Capture runs install Ctrl+C/SIGTERM handlers. For forced-kill testing use a separate
synthetic session and PID, never another user's process. `recover --session ID`
verifies existing final chunks and adopts exact manifests left before DB commit.
Any unverified `.tmp` stays on disk and blocks automatic restart. Do not treat it
as valid or remove it blindly. Restart after an abnormal session is explicit;
device identity is checked on pause/resume, no fallback is performed.

Permission matrix still requires manual observation: first-run request, deny,
revocation, permission changed while running, then retry. Do not reset TCC or
modify microphone privacy settings automatically. Development-host permissions
(Codex/Terminal/Python responsibility chain) do not prove packaged-app permissions.
Device listing alone is not successful capture; callback frames are required.

Sleep protocol requires user coordination: verify caffeinate assertion via
`pmset -g assertions`, display sleep, then actual lid-close/manual sleep and wake.
Never claim recording during sleep. Stalls or ADC discontinuities enter explicit
recovery; no synthesized silence. The 100 ms timing tolerance is a diagnostic
threshold, not a predeclared production quality SLA. Intentional pause is separate.

Three-hour acceptance requires complete wall time, periodic RSS/CPU/FD/queue,
continuous verified metadata, readable WAVs, zero unexplained interruption,
graceful close, and an independent assessment of RSS curve after warmup. The
machine-generated `capture_test_pass` is a mechanics check, NOT Phase 2 acceptance.

Capture duration is measured from successful device start to stopping ingress,
summed across active epochs. Process wall time, deliberate pause, start and drain
latencies are separate. Startup readiness is not classroom audio. Fault durations
may be lower bounds; `unknown_gap_count` prevents unknown loss becoming 'zero gaps'.
The CPython callback uses non-waiting queue capacity acquisition (`put_nowait`),
but Python/PortAudio is not a hard realtime guarantee. Backend acceptance depends
on measured callback and queue stability, not the API's name.
