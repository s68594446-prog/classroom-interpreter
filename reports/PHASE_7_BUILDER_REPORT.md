# CLASSROOM INTERPRETER — PHASE 7 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — NOT ACCEPTED.** All evidence is E1/E2 local
deterministic with synthetic fixtures only. No real recordings or user data
were touched (`REAL_DATA_PURGE_ALLOWED=false` honored: the purge machinery
operates only on self-created pytest temp fixtures).

## Scope

Data purge per the overnight authorization: `NO VALID SUMMARY = NO PURGE` hard
gate, exact validated manifest, path containment, idempotent journaled steps,
allowlisted metadata ledger, retained-summary verification, unfinished-session
deletion, and the API/UI wiring for the automatic post-save purge.

## Files changed / added

- `backend/classroom_interpreter/agents/cleanup.py` (new): `CleanupAgent` —
  - `build_manifest`: canonicalized, symlink-rejected, strictly session-tree
    contained file list; refuses the app root and any path inside the summary
    store.
  - `_revalidate_summary`: independent gate re-reads, re-hashes and
    re-validates the retained summary before and after deletion; refusals are
    `PurgeRefused` (fail closed).
  - `purge`: SUMMARY_SAVED → PURGING (audited) → journaled per-file deletion
    (missing file = ABSENT, never an error) → empty-dir removal bottom-up →
    residue check → summary integrity check → one transaction inserting the
    allowlisted `purged_session_metadata` row and deleting the temporary
    session row + session-linked audits. Resume after partial completion:
    a missing session dir with status PURGING skips straight to row
    finalization; duplicate purge after completion is a no-op
    (`ALREADY_PURGED`).
  - `delete_unfinished`: explicit destructive path for unfinished sessions
    (records no summary ledger row; refuses sessions claiming a saved
    summary).
- `backend/classroom_interpreter/runtime/pipeline.py`: `auto_purge` (default
  on) runs `_auto_purge` after verified `SUMMARY_SAVED`; a refused/failed
  purge leaves the session RECOVERY_REQUIRED with the saved summary intact
  (never deleted, never blocked from user access). `delete_session` passthrough.
- `backend/classroom_interpreter/api/sessions.py`: `DELETE /api/recovery/{id}`
  now performs the explicit unfinished-session deletion (409 on refusal).
- `frontend/src/App.tsx`: Delete button enabled with an action-time
  `window.confirm` warning; recovery list refreshes after deletion.
- `tests/integration/test_purge.py` (10 tests); `auto_purge=False` seam added
  to `make_client`/`run_flow_to_summary` so Phase 5/6 tests can observe the
  pre-purge `SUMMARY_SAVED` state (default-on in production composition).

## Commands and exact results

```
cd frontend && npm run typecheck && npm run build   → exit 0 (dist rebuilt)
PYTHONPATH=backend .../python -m pytest
→ 168 passed, 1 warning in 7.98s   (Phase 6: 159 passed)
```

## Failure matrix covered (tests)

- Wrong state (RECORDING with no summary claim) → `NO_VALID_SUMMARY` refusal,
  nothing deleted.
- Missing summary file → refusal, all content retained.
- Tampered summary (hash mismatch) → refusal, all content retained.
- Symlink inside session tree → manifest refuses.
- Partial purge (crash simulation: 2 files deleted, status PURGING) → re-run
  completes; no residue; metadata written exactly once.
- Duplicate purge after completion → `ALREADY_PURGED` no-op.
- Retained summary byte-identical before/after purge.
- Post-purge residue: session dir absent; all content tables zero rows
  (capture_chunks, segments, asr_results, translation_results,
  processing_attempts, summary_jobs, summary_chunks, websocket_events,
  pipeline_cursors); `purged_session_metadata` has exactly the 11 allowlisted
  columns; purge journal targets are paths/steps only (no `.md`, no content).
- Unfinished-session deletion: files + rows removed, no metadata row;
  SUMMARY_SAVED session → delete refused (409, purge path applies).

## Evidence classification

E1/E2 local deterministic, synthetic fixtures only. No APFS/SSD forensic
erase is implemented or claimed (logical deletion only, per D-015). Real-data
purge: NOT TESTED and not performed.

## Known failures and unknowns (retained)

- Backup/sync of the app-data root is outside application control (documented
  in DATA_LIFECYCLE; Time Machine exclusion deferred to packaging).
- `state_transition_audits` for segments are removed by entity_id in the final
  transaction; a purge that crashes before finalization leaves audits in place
  until resume completes (they are content-free).
- Auto-purge runs inside the finalize task; a very large session purge could
  delay the final session.state event by the deletion duration — acceptable
  for V1, revisited if the soak shows user-visible lag.

## Privacy and purge impact

This phase implements the purge invariant machinery itself. `PERSIST FIRST`
untouched; `NO VALID SUMMARY = NO PURGE` now enforced by state machine +
independent revalidation + residue/retention checks. Test fixtures are
synthetic only.

## Open gates

Phase 2 hardware ladder; real MiMo tests; independent Reviewer/Validator/Oracle.

## Recommended next phase

Phase 8 — Reliability failure matrix.
