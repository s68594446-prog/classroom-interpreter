# CLASSROOM INTERPRETER — PHASE 6 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — NOT ACCEPTED.** Real MiMo summary evidence: NOT TESTED
(no key authorization). All evidence below is E1/E2 local deterministic.

## Scope

Final Summary per the overnight authorization: hierarchical/resumable
chunking, English-source-of-truth prompts, strict ten-section structural
validation, atomic write protocol, crash-safe resume, orchestrator integration
(End Class → drain → summary → SUMMARY_SAVED), provider port + MiMo adapter +
deterministic mocks with an invalid-output matrix.

## Files changed / added

- `backend/classroom_interpreter/providers/summary_contract.py` (new):
  versioned chunk + synthesis prompts (English factual source, 不确定 marks,
  no invented facts, exact template), `REQUIRED_SECTIONS` (the exact ten from
  PRODUCT.md), `validate_summary_document` (header fields 课程名称/日期/课程时长 +
  exactly ten titled sections in order, each non-empty), `validate_chunk_notes`.
- `backend/classroom_interpreter/providers/mimo.py`: `MimoSummaryProvider`
  (chat endpoint, injectable post seam, timeout/network/status classification).
- `backend/classroom_interpreter/providers/mocks.py`: `MockSummaryProvider`
  (deterministic valid-document generator; per-chunk/synthesis overrides for
  the invalid-output matrix; records calls).
- `backend/classroom_interpreter/providers/base.py`: `SummaryProvider` protocol.
- `backend/classroom_interpreter/agents/summary.py` (new): `SummaryAgent` —
  `verify_drained` (contiguous + DONE gate), token-budget chunking persisted
  once in `summary_chunks` (stable resume boundaries), per-chunk notes with
  bounded retries, synthesis, validation BEFORE any write, atomic protocol
  (temp → fsync → rename → dir fsync → read-back → re-validation → SHA256),
  `SUMMARY_SAVED` transition + summary columns set atomically afterwards,
  `_verify_saved_summary` (path/symlink/hash re-check on every resume).
- `backend/classroom_interpreter/runtime/pipeline.py`: `_run_summary` after
  drain; any summary failure → session RECOVERY_REQUIRED (gate stays closed);
  missing summary provider is an explicit CONFIGURATION error.
- `backend/classroom_interpreter/main.py`: builds the real `MimoSummaryProvider`
  when a key is configured.
- `tests/unit/test_summary_contract.py` (10), `tests/integration/test_summary_pipeline.py`
  (9), `tests/integration/test_live_api.py` (end-to-end now drives to
  SUMMARY_SAVED via `MockSummaryProvider`).

## Commands and exact results

```
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
→ 159 passed, 1 warning in 7.14s   (Phase 5: 140 passed)
```

## Key behaviors implemented and verified

- End Class full journey: capture chunks → segmentation → ASR → ordered
  translation → drain → hierarchical summary → `SUMMARY_SAVED` with a saved,
  hash-verified Markdown file outside the session tree (single test drives the
  whole chain through the REST API with the backend-owned fake capture).
- Validation-first: missing/mis-titled/out-of-order/empty/truncated sections
  and missing header fields are all rejected; a rejected synthesis writes NO
  file and never reaches SUMMARY_SAVED (test asserts zero `*.md` in the
  summary store, `summary_path` NULL, status RECOVERY_REQUIRED).
- Resume: chunk boundaries and completed notes are reused; a re-run on a SAVED
  session performs zero provider calls and re-verifies file+hash. Tampering
  with the saved file is detected (hash mismatch → SummaryValidationError).
- Atomic write: no `.tmp` residue; read-back re-validated; hash recorded in
  `sessions.summary_hash`.
- Chunk failure with a non-retryable provider error → job FAILED → session
  RECOVERY_REQUIRED (no fake-complete summary).

## Evidence classification

- E1/E2 local deterministic only. Real MiMo summary short test: NOT TESTED.
- Prompt quality (faithfulness, no invention) is not automatable: deferred to
  the real MiMo confidence run + reviewed fixtures.

## Known failures and unknowns (retained)

- Token-budget chunking uses the deterministic estimator, not the provider
  tokenizer; recalibration is config-only.
- Chunk retry loop has no inter-attempt delay (bounded count, not time);
  acceptable for V1, revisited in Phase 8.
- Summary material is English-only by design (D-019: prevents ASR→translation
  error compounding); Chinese organization aid not yet sent — documented
  choice, revisit with real samples.

## Privacy and purge impact

The summary is written only to the summary store outside `sessions/`; no
content in logs; test documents are synthetic. Purge invariants strengthened
(the SAVED gate now has full evidence machinery).

## Open gates

Real MiMo tests; Phase 2 hardware ladder; independent Reviewer/Validator/Oracle.

## Recommended next phase

Phase 7 — Data Purge (implemented under the overnight authorization).
