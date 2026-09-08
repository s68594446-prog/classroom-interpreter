# CLASSROOM INTERPRETER — PHASE 4 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — NOT ACCEPTED.** Real MiMo evidence: NOT TESTED (no key
authorization; `REAL_MIMO_ALLOWED=false`). Deterministic evidence below.

## Scope

English → Simplified Chinese translation per the overnight authorization:
per-session ordered commit against the durable cursor, bounded context
(count/time/token), versioned prompt contract, MiMo translation adapter,
deterministic mock error matrix, ordered/idempotent/hash-bound commits.

## Files changed / added

- `backend/classroom_interpreter/agents/context.py` — `ContextAgent` +
  `estimate_tokens` (CJK-aware, deterministic). Reads ONLY committed DONE
  predecessors; window bounded by `context_max_segments` / `context_max_seconds`
  / `context_max_tokens` (D-029).
- `backend/classroom_interpreter/agents/translation.py` — `TranslationAgent`:
  per-session head-of-line claim (D-022), lease/reclaim/retry/backoff semantics
  mirroring the ASR worker, provider call outside DB transactions, commit via
  `commit_translation_result` (ordering + `english_text_sha256` + prior-completeness
  enforced by the repository). `SourceHashMismatch` → segment FAILED (integrity
  violation, not retried).
- `backend/classroom_interpreter/providers/mimo.py` — `MimoTranslationProvider`,
  `TRANSLATION_SYSTEM_PROMPT` (versioned, implements ARCHITECTURE §13 contract
  verbatim: faithful, no summarize/answer, preserve terminology/numbers/formulae/
  names, context-only-for-reference, output-only-the-translation),
  `render_translation_user_content` (structural `[CONTEXT]` / `[CURRENT SEGMENT]`
  separation), `validate_translation_output` (rejects empty + deterministic
  prompt-wrapper markers).
- `backend/classroom_interpreter/storage/repository.py` —
  `claim_next_translation_segment` (claims the ASR_DONE segment at each
  session's `next_translation_sequence`, plus elapsed TRANSLATION_RETRY rows and
  expired leases; retry-budget rows walk state-machine-legal steps to
  TRANSLATION_FAILED), `finish_translation_attempt`, `mark_translation_retry`,
  `mark_translation_failed`.
- `tests/integration/test_translation_pipeline.py` (22 tests).

## Commands and exact results

```
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
→ 133 passed, 1 warning in 5.18s   (Phase 3: 111 passed)
```

## Key behaviors implemented and verified

- Ordered commit: only the segment at `next_translation_sequence` is claimed;
  commits strictly advance the cursor; out-of-order commits raise
  `OrderingConflict`; prior-incomplete guard enforced in SQL.
- Context: only DONE predecessors; count/time/token bounds each truncate
  (fixtures for all three); estimated tokens deterministic.
- Ordering blockage: a terminal translation failure (e.g. AUTH) blocks that
  session's later commits (head-of-line) while earlier DONE history stays
  intact — later segments remain ASR_DONE, session carries `last_error_category`;
  recovery retry path exists (TRANSLATION_FAILED → TRANSLATION_PENDING).
- Error matrix: TIMEOUT/SERVER/RATE_LIMIT(with Retry-After)/MALFORMED retried
  within budget; AUTH/BALANCE/CONTENT_FILTER fail immediately (one attempt);
  budget exhaustion → TRANSLATION_FAILED + session error.
- Idempotency: duplicate commit → ALREADY_COMMITTED; expired lease reclaimed by
  another worker → exactly one result row; source-hash mismatch → FAILED, zero
  rows written.
- Prompt/payload contract: system+user message shape verified against a fake
  transport; api-key header present; no network in tests.

## Evidence classification

- All tests: **E1/E2 local deterministic (scripted/fake providers)**.
- Real MiMo translation short test: **NOT TESTED** (no authorized key).
- Semantic translation quality: NOT TESTED by design (non-automatable);
  requires the real MiMo sample run and reviewed fixtures.

## Known failures and unknowns (retained)

- Prompt-wrapper detection is a deterministic structural check only; "model
  answered instead of translating" cannot be caught without real calls —
  deferred to the real MiMo confidence run.
- Token estimator is a documented heuristic, not the provider's tokenizer;
  real usage data may require recalibration (allowed: bounds are config).
- Phase 2 hardware gates and Phase 3 real-MiMo gates remain open (unchanged).

## Privacy and purge impact

No key/content in logs; prompts carry only bounded committed classroom text at
runtime (by design, sent to MiMo) — tests use synthetic text only. Purge
invariants untouched.

## Open gates

Real MiMo short tests (ASR+translation); Phase 2 hardware ladder; independent
Reviewer/Validator/Oracle.

## Recommended next phase

Phase 5 — Live UI (implemented under the overnight authorization).
