# Classroom Interpreter — Independent Reviewer / Gatekeeper Report

Date: 2026-09-08  
Reviewed snapshot: `ee72af2d218c88a254c9e7a685216ec65026e054` (`pre-review snapshot`)  
Role: independent review only; no production code or tests were changed  
Final verdict: **FAIL**

The candidate is materially more complete than the prior Phase 2 repository and its
deterministic suite is green. It is not ready for acceptance. The six requested
architecture checks found three unimplemented requirements, two partial implementations,
and one requirement currently avoided by omission rather than implemented. Additional
summary crash-consistency and destructive-delete findings are blocking.

## COMMIT SNAPSHOT

**Completed.**

```text
HEAD: ee72af2d218c88a254c9e7a685216ec65026e054
Subject: pre-review snapshot
Initial review worktree: clean
```

The prior mutation work was fully restored before this review. The only worktree change
created by this review is this report.

## Independent validation run

Backend:

```text
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
173 passed, 1 warning in 21.91s
```

Warning: upstream Starlette TestClient / AnyIO `BlockingPortal` deprecation.

The repository `.venv` stalled while importing pytest and was interrupted; the previously
isolated venv completed normally. This independently reproduces the documented local
`.venv`/Documents-folder problem.

Frontend:

```text
cd frontend
npm run typecheck
npm run build
```

Both passed. Vite built 16 modules in 132 ms.

Selected critical suite:

```text
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest \
  tests/integration/test_purge.py \
  tests/integration/test_summary_pipeline.py \
  tests/unit/test_segmentation.py \
  tests/integration/test_translation_pipeline.py \
  tests/integration/test_live_api.py -q
```

Exit code 0. A green result does not resolve the test-quality findings below.

## TEST QUALITY

### 1. `test_translations_commit_in_strict_sequence_order`

Verdict: **not credible for the requested concurrency property.**

The test drives the existing head-of-line implementation and then checks that rows are
`0..3` and the cursor is 4. It never starts multiple translation requests out of order,
never places completed results in a reorder buffer, and never asks the commit layer to
hold sequence 1 while sequence 0 is slow. It therefore proves the current serial design,
not “parallel work plus ordered release.” The supplied mutation result—removing the
ordered guard while 22 tests still passed—is consistent with direct code inspection.

Evidence: `tests/integration/test_translation_pipeline.py:259-279`,
`backend/classroom_interpreter/storage/repository.py:703-713`.

### 2. `test_source_hash_mismatch_fails_closed`

Verdict: **credible for its narrow contract.**

It submits a result bound to the wrong English hash and checks the terminal failure path.
The commit code independently calculates and compares the durable ASR English hash. The
supplied mutation result also showed that removing this gate makes the test fail.

Evidence: `tests/integration/test_translation_pipeline.py:451`,
`backend/classroom_interpreter/storage/repository.py:1000-1055`.

### 3. `test_summary_write_is_atomic_no_temp_residue`

Verdict: **not credible as an atomic/crash test.**

It observes only a successful run: one `.md`, no `.tmp`. It does not inject failure after
temp write, after fsync, after rename, after directory fsync, after read-back, after the
session state transition, or before metadata commit. It cannot detect removal of the
read-back verification; the supplied M3 mutation result confirms this gap.

Evidence: `tests/integration/test_summary_pipeline.py:193-198`,
`backend/classroom_interpreter/agents/summary.py:317-345`.

### 4. `test_crash_resume_checkpoint_reproduces_same_segments`

Verdict: **misnamed and insufficient.**

All capture chunks are inserted before the loop. The first call to
`process_available_chunks()` consumes every available chunk because the implementation
loops until no next sequence remains. Later “restart” iterations are no-ops. The test does
not crash or restart between chunks and does not validate rebuilding from the last fully
confirmed segment boundary.

Evidence: `tests/unit/test_segmentation.py:130-163`,
`backend/classroom_interpreter/agents/segmentation.py:179-202`.

### 5. `test_websocket_replay_is_deterministic_and_ordered`

Verdict: **credible only for quiescent replay; insufficient for reconnect correctness.**

It finalizes all events before connecting, drains them, disconnects, then reconnects when
no producer is publishing. It never publishes in the replay→subscribe gap. An independent
runtime probe published exactly in that gap; the client received `system.pong` next and
never received the persisted `session.state` event.

Probe result:

```text
READY system.ready
FIRST_AFTER_READY system.pong {}
```

Evidence: `tests/integration/test_live_api.py:235-277`,
`backend/classroom_interpreter/api/websocket.py:41-49`.

### 6. `test_purge_refuses_tampered_summary`

Verdict: **credible for the hash gate.**

It mutates the retained summary, expects a hash refusal, and checks that session content
remains. This is a useful fail-closed test. It does not cover all purge crash windows.

Evidence: `tests/integration/test_purge.py:146-169`,
`backend/classroom_interpreter/agents/cleanup.py:88-108`.

### 7. `test_partial_purge_resumes_to_completion`

Verdict: **partially credible.**

It puts the session in `PURGING`, manually removes files, and verifies completion. That
tests missing-file idempotency. It does not interrupt the real purge implementation at
each journal/files/row boundary and then restart a new process, so it does not fully prove
the claimed resumable protocol.

Evidence: `tests/integration/test_purge.py:188-228`.

### 8. `test_sqlite_busy_never_loses_work`

Verdict: **too permissive for recovery success and unrelated to metrics isolation.**

It correctly checks `PRAGMA integrity_check`, but after releasing the lock accepts either
`SUMMARY_SAVED` or `RECOVERY_REQUIRED`. A work item can therefore fail to recover and the
test still passes. It writes a heartbeat row only to hold a lock; it does not stress a
Health Monitor metrics writer against pipeline writes.

Evidence: `tests/integration/test_reliability_matrix.py:186-227`.

## 3.1 — Out-of-order processing plus ordered commit

Status: **NOT IMPLEMENTED — BLOCKING**.

The repository only claims the segment equal to `sessions.next_translation_sequence`.
This is head-of-line serialization, not concurrent translation with an independent
sequencer/reorder buffer. A slow or retrying sequence prevents all later ASR-complete
segments in that session from even starting translation.

The orchestrator also ignores `asr_worker_count` and `translation_worker_count`: despite
storing both values, `_spawn_workers()` creates exactly one ASR task and one translation
task, both backed by one worker object.

Evidence:

- `backend/classroom_interpreter/storage/repository.py:703-713`
- `backend/classroom_interpreter/agents/translation.py:22-27`
- `backend/classroom_interpreter/runtime/pipeline.py:57-70`
- `backend/classroom_interpreter/runtime/pipeline.py:167-173`

Required repair: allow multiple translation requests for ASR-complete segments, persist
their first-attempt context snapshots, hold completed results durably, and release commits
through a per-session sequencer. Add a slow-sequence test that proves later provider calls
finish while public commits remain ordered.

## 3.2 — Context snapshot bound to first attempt

Status: **NOT IMPLEMENTED — BLOCKING**.

`TranslationAgent._process_claim()` calls `ContextAgent.build()` on every attempt. Neither
the migration nor segment/attempt rows contain a serialized context snapshot/hash/version.
Retry therefore has no durable first-attempt context to reload. The current serial design
makes normal context drift difficult to trigger, but repairing 3.1 will expose this bug
immediately.

Evidence:

- `backend/classroom_interpreter/agents/translation.py:88-107`
- `backend/classroom_interpreter/agents/context.py:34-94`
- `backend/migrations/004_pipeline.sql:1-54`

Required repair: atomically persist a bounded, versioned context snapshot when the segment
first becomes translation-eligible; every retry must use that exact snapshot. Add the
requested X→later-context-change→retry-still-X test.

## 3.3 — Recovery rebuilds segment boundaries

Status: **NOT IMPLEMENTED — BLOCKING**.

Startup reconciliation only changes `RECORDING/PAUSED` to `RECOVERY_REQUIRED`. Resume
changes that status to `FINALIZING`, reconstructs workers, and calls the ordinary finalizer.
It does not identify the last confirmed segment end and rerun VAD over all later committed
capture audio.

The Segment Agent instead reloads a durable cursor containing pending PCM and detailed
VAD state, then continues after `next_chunk_sequence`. This is a different recovery
architecture. It may be defensible, but it does not satisfy the explicitly requested
boundary-rebuild rule, and the existing “restart between chunks” test does not actually
exercise such restarts.

Evidence:

- `backend/classroom_interpreter/runtime/pipeline.py:456-509`
- `backend/classroom_interpreter/agents/segmentation.py:105-175`
- `backend/classroom_interpreter/agents/segmentation.py:179-202`
- `tests/unit/test_segmentation.py:130-163`

Required repair: implement the requested re-derivation boundary, or raise a formal ADR
request with failure analysis proving why the cursor-state design is safer and obtain
explicit acceptance before treating 3.3 as complete.

## 3.4 — Health/metrics write isolation

Status: **NOT IMPLEMENTED AS SPECIFIED — BLOCKING FOR RELIABILITY ACCEPTANCE**.

There is no concrete `HealthMonitorAgent`; only a Protocol exists. `/health` calculates a
snapshot from the live listener and synchronous reads of the main runtime DB. The schema
contains `agent_heartbeats` and `metric_samples` in that same DB, but no production writer
implements an independent DB or bounded in-memory buffer with periodic batch flush.

Consequently there is currently no high-frequency metrics writer to contend with pipeline
writes—the lock issue is avoided by omission, not validated isolation. The reported
1,200-row pressure result is not preserved as a repository test or artifact. The existing
SQLite-busy test is a generic lock/integrity test, not this requirement.

Evidence:

- `backend/classroom_interpreter/agents/contracts.py:76`
- `backend/classroom_interpreter/api/health.py:58-70`
- `backend/migrations/001_initial.sql:146-160`
- repository search: no production writer for `metric_samples`/`agent_heartbeats`

Required repair: implement the selected isolation model, define flush/drop/backpressure
semantics, and add a sustained concurrent metrics+pipeline write test with latency and
loss measurements.

## 3.5 — Per-Agent exception isolation

Status: **PARTIALLY IMPLEMENTED — BLOCKING**.

The standalone ASR and translation `run()` methods have catch-all loops. The orchestrator
does not use those methods; it calls `.step()` in its own loops, catches every exception,
sets `worked=False`, and records nothing in `last_step_error`, session health, or logs.
This keeps the task alive but silently hides recurring programming faults.

The segmenter loop catches only `CorruptCaptureChunk`. Any other exception terminates the
task. An independent probe injected `RuntimeError` and observed propagation:

```text
SEGMENTER_LOOP_PROPAGATED RuntimeError probe boom
```

Evidence:

- `backend/classroom_interpreter/runtime/pipeline.py:180-222`
- `backend/classroom_interpreter/agents/transcription.py:62-80`
- `backend/classroom_interpreter/agents/translation.py:68-86`

Required repair: supervise every runtime loop, record a content-free fault/heartbeat,
distinguish recoverable item errors from programming failures, and keep unrelated agents
alive. Add an injected unexpected-exception integration test proving process survival,
health visibility, and continued progress of other agents.

## 3.6 — WebSocket disconnect/reconnect replay

Status: **PARTIALLY IMPLEMENTED — BLOCKING**.

Persist-first events and cursor replay exist. The server first queries/replays the DB and
only afterward subscribes to live publication. An event committed between those actions
is absent from the replay result and has no subscriber, so that live connection misses it.
The independent probe reproduced the loss. The UI's three-second REST polling later
refreshes committed history, but that is eventual UI mitigation and does not make the
WebSocket cursor contract lossless.

Evidence:

- `backend/classroom_interpreter/api/websocket.py:41-49`
- `backend/classroom_interpreter/runtime/events.py:22-52`
- `frontend/src/App.tsx:170-186`
- `tests/integration/test_live_api.py:235-277`

Required repair: subscribe before the replay boundary and deduplicate by sequence, or
atomically establish a high-water mark and replay through it before consuming the live
queue. Add a deterministic event-in-gap test.

## Additional blocking findings

### A. `SUMMARY_SAVED` is committed before summary metadata

Severity: **P1**.

After file write/read-back, the code commits the `SUMMARY_SAVED` session transition, then
uses a separate transaction to write `summary_path`, `summary_hash`, model and timestamp.
A crash in between leaves a session claiming `SUMMARY_SAVED` without the metadata required
for verification or purge. Startup reconciliation ignores `SUMMARY_SAVED`, and recovery
resume only acts on `RECOVERY_REQUIRED`, so this state can become stuck.

This contradicts D-030's documented order, which states SHA256 match precedes
`SUMMARY_SAVED`.

Evidence:

- `backend/classroom_interpreter/agents/summary.py:317-345`
- `backend/classroom_interpreter/runtime/pipeline.py:456-509`
- `DECISIONS.md:115-123`

Required repair: commit verified file metadata and the state transition atomically in one
DB transaction, with a tested recovery rule for a final file that exists before DB commit.

### B. Summary temp-file crash is not recoverable

Severity: **P1**.

The temp file is opened with exclusive create (`xb`). A crash after temp fsync but before
rename leaves `<summary>.md.tmp`; the next retry raises `FileExistsError` rather than
verifying/adopting/quarantining the exact temp file. No test injects this crash window.

Evidence: `backend/classroom_interpreter/agents/summary.py:356-375`.

### C. Recovery DELETE can remove an actively recording session

Severity: **P1 data-loss path**.

The API delegates directly to `delete_unfinished()`. That method refuses only
`SUMMARY_SAVED`/`PURGED`; it does not refuse `RECORDING`, `PAUSED`, or a session currently
owned by the runtime. The repository test explicitly starts a live session and deletes it.
The UI normally hides the recovery list while a session is open, but a direct localhost
request can delete durable files/rows while the listener still owns the session.

Evidence:

- `backend/classroom_interpreter/api/sessions.py:181-191`
- `backend/classroom_interpreter/runtime/pipeline.py:449-452`
- `backend/classroom_interpreter/agents/cleanup.py:235-281`
- `tests/integration/test_purge.py:247-268`

Required repair: require a recoverable/non-active state and coordinate a confirmed stop
before destructive deletion; add a refusal test for active runtime ownership.

### D. The accelerated soak does not exercise its claimed restart/backlog path

Severity: **P1 evidence defect**.

During chunk production, `consumer_cycle()` calls ASR and translation but never invokes
the segmenter, so there are no segments to process and sampled `pending=0`. The 40%
“restart” rebuilds an untouched segmenter cursor. All 450 chunks are first segmented only
in the final drain, after the restart injection. The final drain then calls one ASR step
and one translation step sequentially.

Therefore the run does prove eventual deterministic processing of 1,800 synthetic segments,
fault retries, summary and synthetic purge. It does **not** prove mid-stream segmentation
cursor recovery, realistic backlog, worker concurrency, orchestrator supervision,
WebSocket replay, UI, CoreAudio, or wall-clock resource stability.

Evidence: `scripts/soak_accelerated.py:331-375`.

Required repair: make production and consumption overlap, create measurable backlog,
restart only after durable cursor/leases/results exist, run through the orchestrator, and
assert recovery across the restart boundary.

## SOAK TEST BOUNDARY

### Verified

- A deterministic 9,000-second synthetic audio representation can eventually produce
  1,800 segments through direct Segment/ASR/Translation Agent calls.
- Seeded mock provider faults are retried to completion in that harness.
- The harness observed zero duplicate result rows and ordered final database rows.
- Synthetic summary and synthetic-data purge completed.
- The process-level full unit/integration suite passes.

### Not verified

- Real MiMo access, contract behavior, latency, quality, quota, billing, or retention.
- Real microphone capture on current source for 5 min, 30 min, or 3 h.
- Real three-hour wall-clock process, CoreAudio callback, RSS/FD/queue stability.
- Permission first-run/deny/revoke; physical device loss/reconnect; display sleep/lid/wake.
- Concurrent translation plus ordered release.
- Meaningful pipeline backlog and mid-flight restart recovery in the soak.
- WebSocket reconnect without an event gap.
- Summary crash recovery at each filesystem/DB boundary.
- Independent Oracle usability in a real class.

The MiMo adapter's endpoint, `api-key` authentication, one WAV input item, ASR model and
`asr_options.language=en` match the current official MiMo documentation. This is contract
inspection only, not a real provider test.

## BLOCKING ISSUES

1. Replace head-of-line translation with concurrent processing plus durable ordered release.
2. Persist and reuse first-attempt translation context snapshots.
3. Implement/approve the required segment-boundary rebuild recovery architecture.
4. Implement observable per-Agent supervision; segmenter must not silently die and worker
   programming faults must not be swallowed invisibly.
5. Close the WebSocket replay→subscribe loss window.
6. Make summary metadata plus `SUMMARY_SAVED` one crash-consistent commit and recover stale
   temp/final files.
7. Refuse destructive deletion of an active/owned session.
8. Implement the defined Health Monitor/metrics isolation model and evidence.
9. Repair the accelerated soak so its restart, backlog and runtime claims are real.
10. Add adversarial regression tests for each item above; do not weaken current tests.
11. After repair, rerun Reviewer and Validator gates. Real MiMo/macOS/three-hour/Oracle
    gates remain separately required for production acceptance.

## NON-BLOCKING / DEFERRED ISSUES

- Repository `.venv` import slowness; isolated venv is the current workaround, not a
  packaging fix.
- Upstream Starlette/AnyIO deprecation warning.
- Summary provider retry loop has no backoff between attempts.
- Real MiMo and hardware evidence are correctly marked NOT TESTED, but remain release gates.
- D-024 Python capture adapter remains candidate until hardware evidence exists.

## Final conclusion

**FAIL.**

The snapshot is a substantial full-stack prototype and all 173 deterministic tests pass,
but the requested architecture is not fully implemented and several green tests do not
exercise the properties their names/reports claim. The translation, recovery, supervision,
WebSocket and summary crash-consistency gaps are acceptance blockers. No Repair work was
performed in this review.

