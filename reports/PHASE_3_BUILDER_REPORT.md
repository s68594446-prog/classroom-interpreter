# CLASSROOM INTERPRETER — PHASE 3 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — NOT ACCEPTED.** Real MiMo evidence: NOT TESTED (no key
authorization; `REAL_MIMO_ALLOWED=false`). Deterministic evidence below.

## Scope

Segmentation + MiMo ASR per the overnight build authorization: local
deterministic VAD segmentation over committed capture chunks, durable per-session
segment materialization, SQLite lease/claim/retry/restart ASR workers, the MiMo
ASR adapter with a 10 MB payload gate, typed error classification, deterministic
mock error matrix, and request-payload contract tests.

## Decisions recorded first (per AGENTS.md)

D-025 … D-031 were written into `DECISIONS.md` BEFORE implementation. The
material Phase 3 choices: D-025 (in-process asyncio workers, SQLite rows are the
only queue truth), D-026 (segmenter consumes committed chunks via a durable
`pipeline_cursors` cursor; deterministic energy/RMS VAD; no ML VAD in V1),
D-027 (MiMo documented protocol + payload gate + typed classification; no
fallback).

## Files changed / added

- `backend/migrations/004_pipeline.sql` — `pipeline_cursors` (durable consumer
  position + exact VAD resume state), `summary_chunks` + `purge_journal`
  (schema preparation for Phases 6/7, unused until then).
- `backend/classroom_interpreter/domain/models.py` — `ProviderErrorCategory`,
  `ProviderError` (content-free).
- `backend/classroom_interpreter/providers/errors.py` — `ProviderFailure`.
- `backend/classroom_interpreter/providers/mimo.py` — `MimoASRProvider` +
  pure `build_asr_payload` / `parse_asr_response` / `classify_http_status`;
  injectable `post` seam so tests never dial out; missing key is a hard
  configuration error (never a silent mock).
- `backend/classroom_interpreter/providers/mocks.py` — `ScriptedASRProvider`
  (seeded error matrix; awaitable script outcomes; records every call).
- `backend/classroom_interpreter/agents/segmentation.py` — `SegmentAgent`
  (energy VAD, pre-roll, silence/max-duration close with overlap, epoch
  force-seal, atomic segment WAV + CAPTURED row + cursor checkpoint in ONE
  transaction, resampler flush at epoch/seal boundaries).
- `backend/classroom_interpreter/agents/transcription.py` — `TranscriptionAgent`
  (promote CAPTURED→ASR_PENDING, lease claim incl. expired-lease reclaim,
  provider call strictly outside DB transactions, idempotent commit, bounded
  backoff with jitter + Retry-After, budget exhaustion → ASR_FAILED + session
  error).
- `backend/classroom_interpreter/storage/repository.py` —
  `promote_captured_segments`, `claim_next_asr_segment`, `finish_asr_attempt`,
  `mark_asr_retry`, `mark_asr_failed`, `update_session_error`; lease cleared on
  ASR commit.
- `backend/classroom_interpreter/config.py` — VAD/ASR/context knobs
  (`vad_threshold_rms`, ASR lease/attempts/backoff/timeout, context token cap,
  translation + summary knobs for later phases).
- `tests/helpers_phase3.py`, `tests/unit/test_segmentation.py` (10),
  `tests/unit/test_mimo_provider.py` (25), `tests/integration/test_asr_pipeline.py` (13).
- `tests/conftest.py`, `tests/integration/test_schema.py` — migration version
  assertions 3→4 (schema grew; no acceptance criterion weakened).

## Commands and exact results

```
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
→ 111 passed, 1 warning in 5.17s   (was 63 passed at Phase 2 baseline; +48 tests)
```

First full-suite run after the 992 MB accelerated capture run took 166 s due to
disk cache pressure; it was re-run and returned 5.17 s. Both runs passed.

## Key behaviors implemented and verified

- Segmentation: silence-close, min-duration discard (F-032 flood guard),
  max-duration force close with configured overlap (F-033), utterance spanning
  chunk boundaries stays one segment, epoch/gap boundaries force-seal and never
  splice discontinuous audio, sub-minimum utterances counted as discarded.
- Crash recovery: cursor (chunk position + VAD counters + pending/overlap/carry
  buffers) checkpoints atomically with each seal and at chunk boundaries;
  restart reproduces the same segment set (± one 10 ms frame) and can never
  duplicate or conflict a sealed segment (UNIQUE(session, sequence) + atomic
  cursor). SoXR resampler tail flushed at epoch/seal; post-crash resampler
  cold-start is a documented sub-millisecond approximation.
- Durability: segment WAV temp → fsync → atomic rename → directory fsync →
  sha256 → CAPTURED row + cursor in one transaction (PERSIST FIRST, PROCESS
  SECOND). Corrupt/missing chunk/segment files fail closed, no provider call.
- ASR queue: durable leases, expired-lease reclaim by another worker,
  at-least-once with exactly one committed result (ALREADY_COMMITTED on
  duplicate), retryable errors bounded backoff + Retry-After honor, budget
  exhaustion → ASR_FAILED + session.last_error, AUTH/BALANCE/CONTENT_FILTER
  never auto-retried, out-of-order ASR completion allowed (translation ordering
  comes in Phase 4).
- MiMo payload contract: `POST /v1/chat/completions`, single `input_audio`
  data-URL item, `asr_options.language=en`, `stream:false`, result surface
  `choices[0].message.content`, verified against official docs 2026-09-08; local
  10 MB encoded gate fails closed before any network call.

## Evidence classification

- All above tests: **E1/E2 local deterministic (synthetic PCM, scripted
  providers)**. Segments derive from generated 440 Hz tone/silence envelopes;
  no real classroom audio anywhere.
- Real MiMo short ASR test: **NOT TESTED** — no authorized key configured; no
  key was requested from the user in chat.
- Target-macOS end-to-end (mic → segmentation → ASR): **NOT TESTED** (hardware
  capture still unauthorized).

## Known failures and unknowns (retained, not hidden)

- Provider error-code table beyond HTTP status classes is not documented
  publicly; unknown provider codes map to UNKNOWN (bounded retry) — must be
  re-validated in the real MiMo short run.
- Provider call timeout uses asyncio.wait_for; httpx2 is only imported in the
  default post seam and was never exercised against the live endpoint.
- Cold-resume audio content may differ sub-millisecond from an uninterrupted
  run (resampler warmup); timing stays deterministic; no duplicate-sequence
  conflict is possible by construction.
- Phase 2's open hardware gates remain open (unchanged).

## Privacy and purge impact

No key, audio, transcript, or classroom text in logs/reports. Scripted mocks
only. Segment/chunk fixtures live in pytest temp dirs and are auto-removed.
Migration 004 tables carry no content by default. Purge invariants untouched
(no purge behavior implemented yet).

## Open gates

Real MiMo short test; hardware ladder + permission/device/sleep matrix (Phase 2
gates); independent Reviewer/Validator/Oracle; D-024/D-027 live validation.

## Recommended next phase

Phase 4 — Translation (implemented under the overnight authorization).
