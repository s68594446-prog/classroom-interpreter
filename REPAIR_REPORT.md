# Classroom Interpreter — Repair Handoff (Partial, Not Accepted)

Date: 2026-09-09  
Repair baseline: `ee72af2` (`pre-review snapshot`)  
Source finding set: `reports/INDEPENDENT_REVIEW_2026-09-08.md`  
Status: **partial repair candidate; independent Codex re-review required**

This report does not declare any blocking issue resolved. The owner stopped the
repair run after the most serious checks and will hand remaining work to Zcode.
All changes are uncommitted. The independent review report is preserved unchanged.

> **Superseded status note (added 2026-09-10, Claude Code takeover).**
> The historical record above is preserved as written. Two statements in it are
> no longer current:
>
> * "All changes are uncommitted" — those changes were committed as `4948567`
>   (*partial repair handoff snapshot*), and the further uncommitted work that
>   followed it was preserved and committed as `8bf3a2c`
>   (*pre-claude-takeover snapshot*).
> * The test counts quoted in this report (including "184 passed") are a
>   snapshot of that run, not of the current tree.
>
> Current status, evidence and remaining gaps are in
> `reports/CLAUDE_CODE_TAKEOVER_REPORT.md`. That report also does not declare
> a PASS; independent Codex review is still required.

## Safety checks completed in this shortened run

- Active/owned `RECORDING` deletion is refused with HTTP 409 and capture rows/files remain.
- Non-owned `FINALIZING` deletion is refused with HTTP 409 and the session row remains.
- Summary metadata, SHA256, state transition and audit now share one SQLite transaction.
- A trigger-injected failure after the summary session UPDATE but before audit insertion
  rolls the transaction back; state becomes recovery-required through the orchestrator,
  path/hash remain NULL, and purge is refused.
- A rename-boundary injected crash leaves `.md.tmp`; recovery removes the
  stale regular temp file and successfully retries. Symlink temp paths fail closed.

Focused command:

```text
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest \
  tests/integration/test_purge.py::test_delete_active_recording_session_is_refused_without_data_loss \
  tests/integration/test_purge.py::test_delete_finalizing_session_is_refused_without_row_loss \
  tests/integration/test_summary_pipeline.py::test_summary_metadata_and_saved_state_roll_back_together_on_crash_boundary \
  tests/integration/test_summary_pipeline.py::test_summary_retry_recovers_temp_left_by_crash_before_rename -q
4 passed, 1 upstream Starlette/AnyIO warning
```

Last full backend run before the owner stopped the repair:

```text
184 passed, 1 upstream Starlette/AnyIO warning in 24.19s
```

`git diff --check`: exit 0.

## Ten independent-review findings: current repair state

| # | Repair-candidate status | Code/test evidence | Bug reintroduction that should make the test fail |
|---|---|---|---|
| 1 active DELETE | Candidate implemented; not independently reviewed | `agents/cleanup.py`, `runtime/pipeline.py`; `test_delete_active_recording_session_is_refused_without_data_loss`, `test_delete_finalizing_session_is_refused_without_row_loss` | Remove runtime ownership or state gate: either API test deletes rows/files or returns non-409 |
| 2 summary DB/state atomicity | Candidate implemented; not independently reviewed | `storage/repository.py::commit_summary_saved`; `test_summary_metadata_and_saved_state_roll_back_together_on_crash_boundary` | Split metadata/state/audit commits: trigger injection exposes `SUMMARY_SAVED` or non-NULL metadata |
| 3 concurrent translation + sequencer | **Partial** | migration 005 staging table, `TranslationSequencer`, slow-head test | Restore head-only claim or direct public worker commit: `test_slow_head_does_not_block_later_translation_processing` fails |
| 4 first-attempt context snapshot | Candidate implemented; not independently reviewed | migration 005 context table; `test_translation_retry_reuses_first_attempt_context_snapshot` | Rebuild context on retry: observed contexts become `(0,)`, then `(0,1)` |
| 5 runtime exception isolation | Candidate implemented; not independently reviewed | runtime catch-all + content-free fault record; `test_segmenter_unexpected_exception_is_recorded_and_loop_continues` | Remove catch-all: injected `RuntimeError("probe boom")` terminates task before second call |
| 6 segment boundary recovery | Candidate implemented; not independently reviewed | `SegmentAgent.rebuild_unconfirmed_tail`; rewritten crash test + corrupted-open-cursor test | Trust saved pending PCM: rebuilt boundary starts near injected 9000 ms or mismatches one-shot |
| 7 WebSocket replay race | Candidate implemented; not independently reviewed | subscribe-before-replay; `test_websocket_buffers_event_published_after_replay_query_before_stream` | Move subscribe after replay: injected race-window event is followed by pong and is missing |
| 8 stale summary temp | Candidate implemented; not independently reviewed | `_atomic_write`; rename crash/retry test | Restore exclusive create without cleanup: recovery returns to `RECOVERY_REQUIRED`/`FileExistsError` |
| 9 health metrics isolation | Candidate implemented; not independently reviewed | `agents/health.py`, dedicated `health.sqlite3`; two pressure/lock tests | Write metrics to runtime DB: locked-health isolation and main-table-zero assertions fail |
| 10 accelerated soak evidence | **Not repaired** | `scripts/soak_accelerated.py` remains the pre-review harness | N/A until rewritten and rerun |

## Required Zcode follow-up before Codex re-review

1. Finish item 3 rather than assuming the green test is sufficient:
   - pass `Settings.asr_worker_count` and `Settings.translation_worker_count` into
     production `SessionOrchestrator` construction (current `main.py` still relies on
     constructor defaults);
   - add a non-default worker-count test;
   - review whether the legacy public `commit_translation_result` path can bypass the
     sequencer and either restrict it or prove it cannot be reached by production workers.
2. Rewrite `scripts/soak_accelerated.py` for migration 005 and the new sequencer. The
   current script calls `TranslationAgent.step()` without `TranslationSequencer`, so it
   no longer represents the repaired pipeline and must not be run/reported as evidence.
3. The new soak must overlap chunk production and segmentation, record a non-zero durable
   segment cursor and measurable backlog before restart, interrupt/reconstruct workers,
   use the recovery boundary rebuild, drain via runtime orchestration, and save fresh logs
   and a fresh report under a new artifact directory. Do not reuse Phase 9's old numbers.
4. Run the complete backend suite after the final soak-harness patch, frontend typecheck
   and build, migration upgrade/idempotency tests, secret/content scan, and the fresh
   9000-second-equivalent soak.
5. Return the code, exact commands, raw counts, warnings, artifact paths and this report
   to Codex. Zcode must not mark Reviewer/Validator/Oracle PASS.

## Newly observed items (recorded, not expanded)

- One full run during item 6 had the existing SIGINT subprocess test time out waiting
  for startup (`1 failed, 179 passed`); an immediate isolated rerun passed in 0.58 s and
  the next full run passed. Treat this as a timing flake for Reviewer awareness; no
  unrelated test timeout was changed.
- The accelerated soak is now incompatible with the staged-translation protocol until
  item 10 is repaired. This is a known incomplete integration, not a production claim.

## Acceptance boundary

No real MiMo, real microphone, wall-clock three-hour, permission/device/sleep, or Oracle
evidence was produced. The above items remain repair candidates until Codex independently
reads the code, reruns adversarial tests and issues a new verdict.
