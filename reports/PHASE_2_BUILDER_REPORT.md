# CLASSROOM INTERPRETER — PHASE 2 BUILDER REPORT (Overnight build)

Date: 2026-09-08
Builder: Zcode (Builder + Repair Agent role per overnight authorization)
Status: **IMPLEMENTED — NOT ACCEPTED.** Implementation ≠ acceptance. Independent
Reviewer/Validator/Oracle gates remain open.

## Scope

Phase 2 closeout for the overnight build: read-only takeover audit of the existing
Phase 2 audio spike, current-source deterministic regression, compileall, offline
lock check, and a fresh current-source accelerated 10,800-second synthetic
equivalent run. No audio code changes were made in this batch (source hashes below
are identical to the handoff — the Phase 2 implementation was already in place).

## Takeover audit results

- `git status --short` / `git status --branch --short`: `## No commits yet on main`;
  all project files untracked. No destructive reset/checkout/clean performed.
- Source hashes at takeover (identical to ZCODE_HANDOFF.md §H.7 — no drift):

```
audio/__init__.py       3095b72f3ae75a256c48011c4935db03182a4d094a4cbd92b7d6a1afd1c7ce2a
audio/__main__.py       00e5d8dc81a97934e06eb31d8e33816e08be8bd2cc87315ef3545cfdde7aa96b
audio/durable.py        8a333fe6f48bba111a5a23642200822d616136409c853300dd76d894165668a5
audio/listener.py       03142dbbcefa7aa32a2156c3276cb23388dc1b5c7eee27fcd4e98371edcbdf18
audio/normalize.py      bc1af74b8cd70a55b9adc1c01b72d7e6e3066ab4e66394776cf34b7b2071ce60
storage/database.py     1ad63b3126b1361fef96382f38811747dde6b56b267ca020c66f2e46ce098a4f
migrations/001_initial.sql fabf7fc4eccb8dd9f8c809217fff809d7b35297d0d658bf30a89f2583197e6c4
migrations/002_capture.sql 5644413ec9886c317d228a0c8f685c7cb48d8acf63cdf4cdf5ceb1bb4dfb9a97
migrations/003_capture_clock.sql 643f0c0687ddbb722c4f78b01f09206c69aac71f34038343cae818e0d0639886
```

- Existing private evidence roots under `/private/tmp/classroom-phase2-*` were
  listed (metadata only); nothing was played, printed, uploaded, or deleted.

## Files changed in this batch

- `reports/PHASE_2_BUILDER_REPORT.md` (this file; new `reports/` directory).

No source changes. Prior-source hardware evidence (30-min, pause, signal runs in
ZCODE_HANDOFF.md §H) remains prior-source evidence for the hashes listed above.

## Commands and exact results

1. `PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest`
   → **63 passed, 1 warning in 3.64s** (warning: upstream Starlette/AnyIO
   BlockingPortal deprecation; not a project failure).
2. `PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m compileall -q backend`
   → exit 0.
3. `UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv lock --check --offline`
   → `Resolved 32 packages in 22ms`, exit 0.
4. `PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -c "import sounddevice, soxr, numpy, psutil, fastapi"`
   → `AUDIO_DEPS_OK`.

## Accelerated current-source test (E5-class, synthetic)

Command:

```
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python \
  -m classroom_interpreter.audio synthetic \
  --root /private/tmp/classroom-interpreter-overnight/phase2-accelerated-current \
  --seconds 10800
```

Exit code 0. Results:

| Metric | Value |
|---|---|
| session_id | c365f2b9-f5f3-4eaa-8cb1-37e2933f05da |
| requested / expected / captured audio | 10,800.000 s / 10,800.000 s / 10,800.000 s |
| duration deficit | 0.0 s |
| native frames received | 518,400,000 |
| normalized frames | 172,800,000 |
| recovery chunks | 540, sequence continuous |
| callback overruns | 0 |
| gaps / unknown gaps | 0 / 0 |
| queue high watermark / capacity | 64 / 128 |
| initial / final / peak RSS | 47.20 / 24.17 / 47.20 MiB |
| metric samples | 18 |
| wall-clock elapsed | 16.86 s |
| exceptions | none |
| recovery verification | verified 540, invalid 0, temporary 0, unmanifested 0, invalid rows 0, sequence continuous, clean=true |
| capture_test_pass | true |

Note: the run's `rss_slope_mb_per_hour` (-4,600) is computed from 18 samples over
17 wall-clock seconds and is statistically meaningless; it is recorded for
completeness, not as a stability claim. A short accelerated pass is not a
wall-clock stability claim.

Artifact: `/private/tmp/classroom-interpreter-overnight/phase2-accelerated-current/c365f2b9-f5f3-4eaa-8cb1-37e2933f05da/report_ea2a56a0-f1f6-426e-97c9-8bdcfd518085.json`
(content-free metrics only). Disk footprint of the new root: 992 MiB; 19 GiB free
remained on the data volume.

## Evidence classification

- Deterministic regression, compileall, lock check: **local deterministic (E1)** —
  PASS on current source.
- Accelerated 10,800 s synthetic: **synthetic (E5-class capture-only)** — PASS on
  current source. Does not substitute for wall-clock hardware soak.
- Real 5 min / 30 min / 3 h target-Mac capture: **NOT TESTED** —
  `HARDWARE_CAPTURE_ALLOWED=false`; the user did not provide the exact overnight
  authorization sentence for real microphone recording.
- Permission matrix, device loss/reconnect, sleep/wake: **NOT TESTED** (require
  user cooperation; `MANUAL_TCC_CHANGE_ALLOWED=false`,
  `MANUAL_SLEEP_DEVICE_ACTION_ALLOWED=false`).
- D-024 ADR: remains **candidate, not accepted**.

## Known failures and unknowns

- All blockers listed in ZCODE_HANDOFF.md §I remain open: current-source real
  5-min/30-min/3-h runs, permission matrix, device loss, sleep/wake, Python
  finalization slow-exit repro, independent Reviewer/Validator/Oracle gates.
- Historical failures retained (not hidden): first 30 s pause FAIL (expected-duration
  metric), `stream.stop()` CoreAudio deadlock (now abort→drain→seal), `.venv`
  packaging/Documents-folder slowness (`PYTHONPATH=backend` workaround in use).
- Phase 2 status per acceptance checklist remains **FAIL (evidence incomplete)**.

## Privacy and purge impact

No provider calls, no purge implementation, no classroom content in logs. New
evidence root contains synthetic tone audio only; prior real-recording roots were
not touched. Invariants PERSIST FIRST / NO VALID SUMMARY = NO PURGE unaffected.

## Open gates (unchanged)

Real hardware ladder (5m/30m/3h), macOS permission/device/sleep matrix,
independent Reviewer/Validator/Oracle sign-off, D-024 acceptance.

## Recommended next phase

Phase 3 — Segmentation + MiMo ASR integration (implementation only; real MiMo
remains NOT TESTED without key authorization). Proceeding under the overnight
continuous-build authorization; Phase 2 acceptance still pending independent gates.
