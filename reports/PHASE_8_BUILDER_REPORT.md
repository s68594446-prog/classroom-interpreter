# CLASSROOM INTERPRETER — PHASE 8 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — NOT ACCEPTED.** All new evidence is E1/E2 local
deterministic. Hardware rows remain NOT TESTED (Phase 2 gates).

## Scope

Automate the FAILURE_MODES.md matrix rows that can run deterministically in
process, close the discovered product gaps, and map every row F-001..F-040 to
its evidence or explicit NOT TESTED/manual status.

## Product gaps found and fixed while automating

- `SessionRecord` did not map `last_error_category` / `last_error_safe_message`
  → health/UI could not show provider error truthfulness. Fixed (model + row
  mapping).
- Drain on End Class spun without waiting for retry backoff, mis-labeling
  recovering segments. Now the drain waits on the durable retry schedule
  (bounded 120 s) before the terminal state decision.
- Backend restart reconciliation (F-013): `reconcile_on_startup()` moves
  RECORDING/PAUSED sessions to RECOVERY_REQUIRED (audited) so the recovery
  screen offers Resume/Delete instead of a silent zombie; wired into
  `create_app` lifespan.

## Files changed / added

- `backend/classroom_interpreter/runtime/pipeline.py` (reconcile + drain wait),
  `backend/classroom_interpreter/main.py` (startup reconciliation + log),
  `backend/classroom_interpreter/domain/models.py`,
  `backend/classroom_interpreter/storage/repository.py` (error fields).
- `tests/integration/test_reliability_matrix.py` (5 tests).

## Commands and exact results

```
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
→ 173 passed, 1 warning in 21.47s   (Phase 7: 168 passed)
```

## Failure matrix coverage map (F-001..F-040)

| ID | Evidence | Test / location |
|---|---|---|
| F-001/002 Wi-Fi loss 30 s / 5 min | E2 automated | `test_network_outage_recovers_with_zero_data_loss` (outage window, capture continues, drain to PURGED, retries>2) |
| F-003 ASR timeout | E2 | `tests/integration/test_asr_pipeline.py::test_retry_budget_exhaustion…` + retryable matrix (TIMEOUT) |
| F-004 translation timeout | E2 | `tests/integration/test_translation_pipeline.py::test_translation_budget_exhaustion…` |
| F-005 429 + Retry-After | E2 | `test_rate_limit_honors_retry_after` (ASR + translation) |
| F-006/F-007 500/503 | E2 | retryable matrix (SERVER) in ASR/translation suites |
| F-008 auth/balance terminal | E2 | `test_non_retryable_errors_fail_immediately`, `test_translation…` + `test_health_shows_red_actionable_on_auth_failure` (red PROVIDER_AUTH, exactly one attempt per segment, capture continues) |
| F-009 malformed ASR response | E1 | `test_parse_response_malformed_variants` + retry/recover test |
| F-010 malformed translation | E1 | `test_translation_output_rejects_*` |
| F-011 retries exhausted | E2 | budget exhaustion tests (ASR/TRANSLATION/summary chunk) |
| F-012 process crash while recording | E2+E3(partial) | lease-expiry reclaim tests; Phase 2 SIGKILL recovery tests (`tests/failure/test_audio_process.py`) |
| F-013 FastAPI restart | E2 | `test_backend_restart_reconciles_and_recovers` |
| F-014 browser refresh | E2 | WS replay/no-duplicate tests (`test_live_api`, `test_websocket_event_contract`) |
| F-015 permission denied/revoked | NOT TESTED (manual target-mac) | tests/hardware/README.md protocol |
| F-016 device disconnect | simulated only (Phase 2 fault tests); physical reconnect NOT TESTED | — |
| F-017 idle sleep assertion | prior-source evidence (pmset assertions, Phase 2); current-source rerun NOT TESTED | — |
| F-018 lid close/sleep wake | NOT TESTED (manual target-mac) | — |
| F-019 disk low | simulated (Phase 2 disk guard `LOW_DISK` fail closed); health STORAGE_LOW thresholds E2 | — |
| F-020 disk full/write failure | Phase 2 writer fault tests + `test_summary_write_failure_refuses_saved_then_recovers` | — |
| F-021 SQLite busy/locked | E2 | `test_sqlite_busy_never_loses_work` (external lock, bounded busy error, integrity_check ok, drain completes) |
| F-022 SQLite corruption | documented manual recovery only (no auto-delete); NOT TESTED | — |
| F-023/F-024 summary API fail / invalid | E2 | `test_summary_pipeline.py` invalid-output matrix |
| F-025 summary write failure | E2 | `test_summary_write_failure_refuses_saved_then_recovers` (read-only dir → RECOVERY_REQUIRED → resume → PURGED) |
| F-026 read-back/hash fail | E2 | `test_saved_summary_verification_detects_tampering` |
| F-027 saved but crash before status | E2 | `test_resume_reuses_completed_chunks` (SAVED short-circuit re-verification) |
| F-028/F-029 crash during purge / partial deletion | E2 | `test_partial_purge_resumes_to_completion` |
| F-030 traversal/symlink/bad manifest | E1/E2 | `test_manifest_rejects_symlink_inside_session_tree` + containment in `build_manifest` |
| F-031 1000+ segments | Phase 9 accelerated soak (next) | — |
| F-032 long silence / empty-call flood | E1 | `test_sub_minimum_utterance_is_discarded_not_sealed` |
| F-033 continuous long speech | E1 | `test_max_duration_forces_close_with_overlap` |
| F-034 high noise | config surface (vad_threshold_rms); real-noise quality NOT TESTED | — |
| F-035 provider slower than realtime | E2 | outage/slow-drain tests + `live_delay_ms` visibility |
| F-036 duplicate worker | E2 | lease reclaim + ALREADY_COMMITTED tests |
| F-037 out-of-order completion | E2 | `test_asr_completes_out_of_order_but_rows_stay_ordered` |
| F-038 WS disconnect/slow client | E2 | replay + reconnect tests; slow client drops events without blocking pipeline (bounded queue, fan-out tolerant) |
| F-039 memory pressure | E5 soak metrics (Phase 9); bounded buffers by design | — |
| F-040 API key in exception | E1 by construction | provider errors are static content-free strings; logging formatter emits fixed fields only (`tests/unit/test_logging.py`) |

## Known failures and unknowns (retained)

- F-015..F-018 (permission/device/sleep hardware matrix) remain the Phase 2
  open gates; cannot be automated in this environment.
- F-022 (SQLite corruption) has a documented manual procedure only.
- F-034 noise-quality requires real audio samples (E3/E4).

## Privacy and purge impact

No content in logs; all fault tests operate on synthetic fixtures. Purge
invariants re-verified by Phase 7 suite after the drain/reconciliation changes.

## Open gates

Phase 2 hardware ladder; real MiMo tests; F-031/F-039 via Phase 9 soak;
independent Reviewer/Validator/Oracle.

## Recommended next phase

Phase 9 — Accelerated three-hour-equivalent soak.
