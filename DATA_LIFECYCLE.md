# Classroom Interpreter — Data Lifecycle

Status: `PHASE_0_APPROVED_WITH_AMENDMENTS — PHASE_1_REVIEW_CANDIDATE`  
Last updated: `2026-09-06`

## Non-negotiable rule

`RAW CLASSROOM DATA IS EPHEMERAL.`

The final summary is the only classroom-content artifact intended to persist after a successful session. A minimal metadata-only purge ledger may also persist to prove which summary was saved and that cleanup completed. It may not contain audio, transcript, translation, prompt, summary body, or other classroom statements.

## Data classification

| Class | Examples | During session | After verified purge |
|---|---|---|---|
| Secret | `MIMO_API_KEY` | process environment / future keychain option; never DB/log | remains outside project content; never copied into summary |
| Raw classroom content | PCM frames, recovery capture chunks, WAV segments | session-scoped temporary storage | deleted |
| Derived classroom content | English transcript, Chinese translation, context windows | SQLite/session files | deleted |
| Summary work | chunk inputs/outputs, section summaries, synthesis payload | session-scoped temporary storage | deleted |
| Final classroom content | one structured Markdown summary | summary store | retained |
| Purge ledger metadata | session id, course name, timestamps, duration, summary path/hash/model, app version, status | session row | retained after purge; no classroom text |
| Operational metadata | latency, queue depth, error category, resource metrics, heartbeat | DB/log with content-free fields | may be retained with bounded policy |
| Debug content | explicitly enabled snippets or payload dumps | prohibited by default; if enabled, session-scoped | deleted automatically |

## Storage boundary

Runtime data does not live in the Git checkout. The expected production root is:

`~/Library/Application Support/Classroom Interpreter/`

Tests use an isolated temporary root. The application refuses dangerous roots such as `/`, a home directory, the repository root, or the summary directory itself.

```text
app-root/
  runtime.sqlite3             # queue plus exact allowlisted purge-ledger table
  sessions/<exact-session-id>/   # temporary and purgeable
  summaries/<safe-name>.md       # retained; never nested under sessions
  logs/                          # content-free and bounded
```

Paths stored in a purge manifest are canonicalized and must remain beneath the exact session directory. Symlinks are rejected during deletion.

## Lifecycle by session state

| State | New audio | Temporary content | Summary | Deletion allowed |
|---|---|---|---|---|
| `NEW` | no | minimal session row | none | user may cancel empty session |
| `RECORDING` | yes | grows on disk, bounded in RAM | none | no automatic purge |
| `PAUSED` | no | retained for resume | none | no automatic purge |
| `FINALIZING` | no | retained while queues drain | none | no |
| `SUMMARIZING` | no | transcript plus summary work retained | temporary only | no |
| `SUMMARY_SAVED` | no | retained until purge authorization | final file validated | exact manifest only |
| `PURGING` | no | progressively removed with journal | protected final file | idempotent continuation only |
| `PURGED` | no | temporary session absent; approved ledger row remains | retained | no further content deletion |
| `RECOVERY_REQUIRED` | no by default | retained | may or may not exist | no automatic purge; user sees recovery choices |

## Capture durability

1. PCM frames enter a bounded ring buffer.
2. Audio Listener writes fixed recovery chunks to a temporary file.
3. It flushes, syncs at an implementation-calibrated boundary, atomically renames, then records the sealed chunk.
4. Segment Agent derives natural-language WAV segments from the frame stream/recovery chunks.
5. It writes, validates, syncs, and atomically renames the segment before inserting `CAPTURED`.
6. An orphan scanner reconciles a file that became durable immediately before a crash but whose DB transaction did not commit.

Fixed recovery chunks are not fixed ASR segmentation. They exist solely to bound crash-loss exposure.

## Cloud transmission

| Stage | Sent to MiMo | Not sent for this stage |
|---|---|---|
| ASR | one ASR-ready audio segment and `language=en` option | full session, other segments, API key in body |
| Translation | current English segment plus bounded recent context | entire lecture, raw audio |
| Section summary | English transcript as factual source plus aligned Chinese translation as organization aid | unrelated sessions, raw audio |
| Final synthesis | validated section summaries grounded in English plus session metadata | raw audio; full transcript when section summaries suffice |

Provider requests are unavoidable cloud processing and must be disclosed to the user. Normal logs retain only request IDs when available, timings, model name, status, and safe error categories.

## Summary save protocol

Cleanup authorization requires every step:

1. all segments are durably `DONE` and sequences are contiguous;
2. summary generation completes;
3. required sections are present and non-empty where source material exists;
4. output is written to a random temporary file in the summary directory;
5. file contents are flushed and synced where appropriate;
6. temporary file is atomically renamed to the final path;
7. parent directory is synced where appropriate;
8. final file is reopened and read back;
9. parsed structure and content hash match the validated output;
10. final path is confirmed outside the session directory;
11. the session transitions to `SUMMARY_SAVED`.

An API success response, non-empty string, or existing filename alone is insufficient.

## Purge protocol

`Cleanup Agent` is given a versioned manifest containing exact session-scoped records and paths. It must:

1. revalidate `SUMMARY_SAVED`, final path containment, readable non-empty summary, and stored hash;
2. transition to `PURGING`;
3. delete in idempotent steps and journal only content-free receipts;
4. remove capture chunks, segments, transcript/translation rows, summary work, retries, debug artifacts, caches, and session-linked content-bearing logs;
5. verify that all manifest entries are absent — **this check runs before directory removal**, so a surviving file raises the domain refusal `PurgeRefused` rather than an incidental `OSError` from `rmdir` (D-040);
6. remove the now-empty session directories;
7. verify that the final summary still exists and matches;
8. in one database transaction, insert only `session_id`, `course_name`, `started_at`, `ended_at`, `duration`, `summary_path`, `summary_hash`, `summary_model`, `app_version`, `purged_at`, and `status=PURGED` into `purged_session_metadata`, then delete the temporary session and its cascading content rows;
9. compact the database so the deleted content is unrecoverable, not merely unreachable (D-039).

If any step fails, status becomes `RECOVERY_REQUIRED` or remains resumable `PURGING`. Cleanup never broadens its target. Missing temporary files count as already deleted; a missing or changed final summary aborts purge.

### Deleted means unrecoverable

Dropping a row is not enough. SQLite returns the row's pages to its freelist with
the bytes intact, so a purged session's transcripts and translations stay
readable in the database file with `strings` long after every logical check
passes. Two defences apply (D-039):

- every connection sets `PRAGMA secure_delete = ON`, so a delete zeroes its own
  bytes as it goes;
- `CleanupAgent.compact()` runs `wal_checkpoint(TRUNCATE)` + `VACUUM` after the
  purge commits, reclaiming content stranded in free pages by builds older than
  this rule.

Compaction is best-effort by design: it runs after the purge is committed and
recorded, so a locked database leaves recoverable bytes for the next purge to
clear rather than failing a purge that already succeeded. The property is
verified once the database is at rest — a process still holding a handle can
observe a small transient count that disappears when the last handle closes.

## User-requested deletion of unfinished sessions

An unfinished session is never auto-deleted. `Delete Session` is a separate destructive user action:

- show which recoverable content and potential summary will be lost;
- require action-time confirmation;
- use the same path-containment and idempotent deletion machinery;
- record a content-free deletion result;
- never imply a final summary exists.

## Crash cases

- Crash before a capture chunk seals: at most the bounded in-memory/unsealed tail may be lost; the exact bound is measured in Phase 2.
- Crash after file rename but before DB insert: recovery discovers and reconciles the orphan.
- Crash during provider call: the lease expires; the request may repeat, but commit remains idempotent.
- Crash after summary write but before `SUMMARY_SAVED`: recovery reads and validates the file, then resumes from the safe state.
- Crash during purge: recovery validates the final summary and continues the remaining manifest entries.

## Retention and logs

- No ordinary log message may include full classroom text or audio.
- Content-free logs and aggregate metrics use bounded size/age rotation; exact defaults are set after Phase 8 measurement.
- Session-linked operational rows are removed or de-identified during purge.
- Backups and cloud sync of the application-data root are outside application control and must be disclosed. A later packaging phase should evaluate exclusion from Time Machine/iCloud where appropriate and permitted.

## Deletion truthfulness

V1 deletion is logical filesystem deletion. Modern SSD wear leveling, APFS snapshots, Time Machine, and external backups mean the application cannot promise forensic secure erase. Repeated overwriting is not implemented or claimed.

Session-scoped encryption with key destruction is a future candidate. It is deferred because incorrect key persistence could either defeat privacy or destroy recovery. Adding it requires a recorded architecture decision, migration plan, and crash-recovery tests.
