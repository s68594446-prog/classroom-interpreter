# Classroom Interpreter — Architecture Decision Log

All material architecture changes must be added here before implementation. `Accepted` means accepted for the Phase 0 review candidate, not user approval to begin the next Phase.

| ID | Date | Status | Decision | Reason / consequence |
|---|---|---|---|---|
| D-001 | 2026-09-05 | Accepted | Build V1 as a local modular monolith with independently testable agent boundaries, not ten network microservices. | Preserves responsibility and failure isolation without adding deployment, IPC, and distributed-state failure modes. Audio capture may use a dedicated thread/helper process. |
| D-002 | 2026-09-05 | Accepted | Backend owns microphone capture; React is control/display only. | Browser refresh must not terminate classroom recording. Exact macOS adapter remains a Phase 1/2 spike. |
| D-003 | 2026-09-05 | Accepted | Use durable recovery capture chunks plus VAD-derived semantic ASR segments. | Fixed chunks bound crash loss; they do not violate the rule against mechanical ASR segmentation. |
| D-004 | 2026-09-05 | Accepted | SQLite WAL is the persistent queue and state source of truth; in-memory queues are wake-up hints. | Enables restart recovery and bounded RAM. Provider calls occur outside transactions. |
| D-005 | 2026-09-05 | Accepted | Queue semantics are at-least-once with leases and idempotent stage commits. | Exactly-once external API execution cannot be guaranteed across crashes. Duplicate provider calls are possible; duplicate committed subtitles are not. |
| D-006 | 2026-09-05 | Accepted | Fail closed on terminal segment or summary failure. | An incomplete transcript cannot be silently labelled a completed class; session becomes `RECOVERY_REQUIRED` and purge is blocked. |
| D-007 | 2026-09-05 | Accepted | Final summary is written outside the purgeable session directory and verified by atomic write, sync, read-back, structure check, and hash. | Enforces `NO VALID SUMMARY = NO PURGE` and prevents cleanup path overlap. |
| D-008 | 2026-09-05 | Accepted | Runtime content lives in the macOS application-data directory, not the Git repository. | Reduces accidental commits and separates code from sensitive data. Test roots are isolated temporary directories. |
| D-009 | 2026-09-05 | Accepted | V1 binds to `127.0.0.1` and is described as local-first with cloud AI processing. | UI and state are local, but MiMo receives required audio/text; `100% local` would be false. |
| D-010 | 2026-09-05 | Accepted | V1 implements MiMo adapters and mocks only; other provider interfaces remain unimplemented. | Supports test isolation without scope expansion. No silent fallback. |
| D-011 | 2026-09-05 | Accepted | Default models are configurable: `mimo-v2.5-asr` for ASR, `mimo-v2.5` for realtime translation, `mimo-v2.5-pro` for final summary. | Current official docs list these models. Live quality, access, latency, quota, and cost must be validated before final freeze. |
| D-012 | 2026-09-05 | Accepted | ASR uses explicit `asr_options.language=en`. | Official MiMo docs support `auto`, `zh`, and `en` and recommend explicit language when known. V1 is English-only. |
| D-013 | 2026-09-05 | Accepted | Committed subtitles are immutable in V1. | Prevents history churn and simplifies ordered replay. Current English may remain provisional until translation commits. |
| D-014 | 2026-09-05 | Accepted | Hierarchical summary is internal to Summary Agent rather than many new agents. | Controls context growth while avoiding meaningless agent proliferation. |
| D-015 | 2026-09-05 | Accepted | V1 uses logical deletion and makes no forensic secure-erase claim; session encryption is deferred. | APFS/SSD overwriting is not reliable. Safe key lifecycle and crash recovery need a separate design and tests. |
| D-016 | 2026-09-05 | Accepted | Actual system sleep cannot be treated as lossless capture. Request idle-sleep prevention, detect gaps, and recover on wake. | macOS cannot record while the machine is truly asleep or powered off. The UI must state the gap honestly. |
| D-017 | 2026-09-05 | Accepted | One active recording session per app instance in V1. | Simplifies device ownership, resource bounds, UX, and recovery for the primary use case. |
| D-018 | 2026-09-06 | Accepted gate for Phase 2 | Phase 2 begins with a Python/CoreAudio-compatible capture spike. Keep it only if permission, reconnect, and three-hour checks pass; otherwise escalate to a Swift/AVFoundation helper over IPC and record a new ADR. | Prefer the simpler path, but freeze the capture backend before production audio-pipeline implementation. |
| D-019 | 2026-09-06 | Accepted | English ASR transcript is Summary Agent's factual source of truth; Chinese translation is an organization aid. | Prevents avoidable ASR-to-translation-to-summary error compounding. Conflicts follow English or are marked uncertain. |
| D-020 | 2026-09-06 | Accepted | Retain an approved metadata-only `purged_session_metadata` ledger row, separate from the temporary session row. | The table has only the explicit allowlist. Cleanup copies those fields transactionally, then deletes the temporary session and cascading content. |
| D-021 | 2026-09-06 | Accepted | Enforce `UNIQUE(session_id, sequence)` for segments and one-to-one primary-key result rows for ASR/translation. | At-least-once retries may repeat calls but cannot create duplicate committed results or copied segment identities. |
| D-022 | 2026-09-06 | Accepted | Translation ordering and commit cursors are per session, not process-global. | Workers may serve separate sessions concurrently. A timed-out sequence follows bounded retry/recovery and never holds a global lock forever. |
| D-023 | 2026-09-06 | Accepted | Three-hour stability requires both an accelerated 1000–3000 segment equivalent test and a real wall-clock target-macOS microphone test. | The two tests expose different queue/logic versus OS/handle/time-dependent failures and cannot substitute for one another. |

## D-024 — Phase 2 capture spike (2026-09-06, candidate, not accepted)

Context: Phase 1 approved. Validate Python/CoreAudio before considering Swift.
Options: sounddevice/PortAudio (first candidate); PyAudio (same PortAudio layer,
no assumed reliability advantage); Swift/AVFoundation only after measured Python failure.
Implement raw native-rate PCM16 recovery WAVs, 20-second bounded chunks, fsync/rename,
SHA256 and SQLite commit. Store sidecar metadata before rename to recover the
rename/DB crash window. AUDIO_CAPTURED requires final WAV plus committed metadata.
Sequence is per session; epochs isolate pause/restart/gaps. Host monotonic capture
time is derived from PortAudio ADC/currentTime offset, with host arrival watchdog.
Queue overflow fails closed, never silently discards; failed raw tails are retained.
SoXR streaming HQ resampling lives on the consumer, never the callback; normalized
frames are a bounded downstream interface, raw chunks remain authoritative.
Normalization reads only committed chunks. This adds up to one 20-second recovery
chunk of availability latency in this spike; low-latency durable subchunk journaling
is NOT silently introduced. Phase 3 must review this latency before live ASR design.
Use `caffeinate -i -w PID` during recording; actual sleep is a gap, not captured time.
Graceful input shutdown uses PortAudio abort followed by draining/sealing already queued
frames. A target-Mac SIGINT probe deadlocked inside `Pa_StopStream`/CoreAudio
`AudioOutputUnitStop`; this failure is retained and must be re-tested after repair.
Add migrations 002 and schema-aware 003 rather than modifying the approved migration
001. Migration 003 safely adopts either experimental v2 schema observed during the
spike; legacy manifests keep an unknown clock ID rather than borrowing today's boot.
Fix SQLite
connection ownership before long runs: `with sqlite3.Connection` does not close it.
Evidence and final decision are recorded in `PHASE2_REVIEW.md`; acceptance is pending
5-minute, 30-minute, 3-hour hardware, accelerated, recovery and failure evidence.
Sources: https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html
and https://python-soxr.readthedocs.io/en/stable/soxr.html.

## Phase 2 implementation gate

Audio only is authorized. No MiMo/translation/summary/purge behavior. The final
backend choice cannot be accepted on synthetic tests or successful installation.

## Overnight build decisions (2026-09-08, Phases 3–7; user-authorized continuous build)

The user authorized continuous Phase 2–10 implementation overnight
("让 Zcode 直接做完整个项目，有问题后面再修"). Implementation scheduling is
excepted from per-phase approval waits; acceptance gates are NOT excepted. No real
microphone (`HARDWARE_CAPTURE_ALLOWED=false`), no real MiMo call
(`REAL_MIMO_ALLOWED=false`), no TCC change, no manual sleep/device action, and no
real-data purge (`REAL_DATA_PURGE_ALLOWED=false`) were authorized tonight; those
evidence classes stay NOT TESTED. Decisions below are recorded before their
implementation, per the frozen rule.

- **D-025 (Accepted for build)**: Runtime composition is in-process asyncio worker
  loops inside the backend modular monolith (segmenter, transcription, translation,
  summary workers) driven exclusively by durable SQLite rows; in-memory
  `asyncio.Event`s are wake-up hints only. The orchestrator coordinates state and
  owns no capture/ASR/translation/summary/cleanup implementation. Consequence:
  restart recovery is a DB scan; no broker or extra process is introduced.
- **D-026 (Accepted for build)**: Segment Agent consumes committed
  `capture_chunks` rows (never in-memory audio) through the Phase 2
  SoXR-normalized 16 kHz boundary, using a durable per-session
  `pipeline_cursors` consumer position (migration 004). Segmentation is a
  deterministic local energy/RMS VAD with hangover, pre-roll, silence threshold
  and configured min/preferred/max duration plus max-duration force-close with
  overlap. The cursor advances in the same transaction as segment insert;
  re-derivation after crash is idempotent via the existing
  `UNIQUE(session_id, sequence)` + sha256 identity check. No ML VAD dependency in V1.
- **D-027 (Accepted for build)**: MiMo adapters use the documented
  `POST /v1/chat/completions` protocol with `api-key` header auth, single
  `input_audio` data-URL content item (WAV), `asr_options.language=en`,
  `choices[0].message.content` as the result surface, a local 10 MB
  encoded-payload gate that fails closed as `MALFORMED` before any network call,
  and a typed `ProviderError` classification
  (AUTH/BALANCE/RATE_LIMIT/TIMEOUT/SERVER/MALFORMED/CONTENT_FILTER/NETWORK/UNKNOWN)
  derived from transport errors, HTTP status and documented provider codes.
  No silent provider/model fallback; missing key is a configuration error, not a
  mock substitution. Exact wire shape verified against official docs on
  2026-09-08; live behavior remains NOT TESTED until a real authorized call.
- **D-028 (Accepted for build)**: Every UI-visible fact is persisted first in
  `websocket_events` (monotonic per-session `event_sequence`, migration 001
  schema) and then broadcast; WebSocket reconnect replays from a last-seen cursor
  out of SQLite so refresh/duplicate delivery cannot reorder or duplicate
  committed display history. `subtitle.current` is emitted after durable ASR
  commit; `subtitle.committed` only after durable translation commit.
- **D-029 (Accepted for build)**: Translation context is bounded three ways
  (segment count, wall-clock seconds, estimated tokens via a deterministic CJK-
  aware estimator) and reads only committed `DONE` predecessors of the same
  session. The translation prompt is a versioned, structurally separated
  instruction following the ARCHITECTURE §13 contract; output is rejected when
  empty or containing prompt-wrapper artifacts.
- **D-030 (Accepted for build)**: Summary is a resumable hierarchical job
  (`summary_jobs` + `summary_chunks` work rows, migration 004): ordered English
  transcript is chunked by a token budget, each chunk is summarized by the
  provider, and a synthesis pass produces the required ten-section Markdown.
  Validation (header fields + exactly the ten required non-empty sections)
  happens before any file write; the file protocol is temp write → flush/fsync →
  atomic rename → directory fsync → read-back → structure re-validation →
  SHA256 match → `SUMMARY_SAVED`, written outside the purgeable session tree.
  Invalid/empty/truncated provider output never reaches `SAVED`.
- **D-031 (Accepted for build)**: Cleanup receives an exact validated manifest
  (paths canonicalized, symlink-rejected, strictly inside the session directory,
  never the app root or summaries store), keeps an idempotent step journal
  (`purge_journal`, migration 004), revalidates the retained summary by read-back
  + hash before and after deletion, and only then inserts the allowlisted
  `purged_session_metadata` row and deletes the temporary session row in one
  transaction. `NO VALID SUMMARY = NO PURGE` is enforced by state gate plus
  independent revalidation, not convention.

## Independent-review repair decisions (2026-09-08)

These decisions narrow the implementation to the ten blocking findings in
`reports/INDEPENDENT_REVIEW_2026-09-08.md`. They are repair design records, not
acceptance or independent verification.

- **D-032 (Repair implementation)**: A verified summary file is necessary but
  not sufficient for `SUMMARY_SAVED`. The summary path, SHA256, model,
  timestamp, session-state transition and transition audit are committed in one
  SQLite transaction after file fsync, rename, directory fsync and read-back.
  A final file left before that DB commit is safe to replace/adopt on retry;
  stale regular `.md.tmp` files are removed before exclusive recreation, while
  symlink temp paths fail closed.
- **D-033 (Repair implementation)**: Destructive unfinished-session deletion is
  permitted only for `RECOVERY_REQUIRED` or never-started `NEW` sessions and is
  refused whenever the current runtime owns the session. Recording, paused,
  finalizing, summarizing, saved and purging states are never DELETE-recovery
  targets.
- **D-034 (Repair implementation)**: ASR and translation worker-count settings
  create that many independent leased workers. Translation workers may claim any
  eligible ASR-complete segment and persist results in a durable staging table.
  A separate per-session sequencer alone advances `next_translation_sequence`,
  changes segments to `DONE`, writes public translation results and emits
  committed events. The first context used before a provider call is stored as
  versioned canonical JSON plus SHA256 and reused by every retry.
- **D-035 (Repair implementation)**: Restart recovery does not trust open VAD
  cursor buffers. It keeps only the contiguous `DONE` segment prefix, removes
  later derived segment rows/files, resets the durable cursor to the first
  persisted capture sample after that prefix's end boundary, and re-runs VAD
  over the retained capture chunks before downstream drain.
- **D-036 (Repair implementation)**: Every runtime worker loop has a catch-all
  supervision boundary that records a content-free fault and continues after a
  bounded idle wait. Health heartbeats and numeric metrics are buffered in
  memory and batch-flushed to a dedicated `health.sqlite3`; metrics writes never
  acquire the runtime pipeline database write lock. A bounded buffer drops the
  oldest sample under pressure and reports the drop count as metadata only.
- **D-037 (Repair implementation)**: A WebSocket connection subscribes to the
  live publisher before persisted replay. Events accumulated during replay are
  then consumed from the live queue and deduplicated by the connection's last
  sequence, closing the replay-to-subscribe loss window.
- **D-038 (Repair implementation)**: Accelerated soak production, segmentation,
  ASR, translation staging and ordered sequencing overlap. The injected restart
  occurs only after a non-zero durable cursor and measurable queued backlog
  exist; the harness reconstructs agents, invokes boundary rebuild, and records
  pre/post-restart evidence instead of reusing the earlier soak result.
- **D-039 (Takeover implementation)**: Purge must leave classroom content
  unrecoverable, not merely unreachable by SQL. Deleting rows returns their
  pages to SQLite's freelist with the bytes intact, so a purged session's
  transcripts and translations stayed readable in the database file with
  `strings` (measured: 21,563 English and 16,166 Chinese occurrences after a
  *successful* purge). Two defences, neither subsuming the other: every
  connection sets `PRAGMA secure_delete = ON`, so ordinary deletes zero their
  own bytes without paying to rewrite the file; and `CleanupAgent.compact()`
  runs `wal_checkpoint(TRUNCATE)` + `VACUUM` after a purge commits, reclaiming
  content stranded in free pages by builds that predate this decision.
  `compact()` is best-effort and swallows `sqlite3.Error`: it runs after the
  purge is already committed and recorded, so a locked database must leave
  recoverable bytes for the next purge rather than turn a completed purge into
  a failure. Verified after teardown, where the file is settled; a check made
  from inside a live process can still see a small transient count, which the
  soak reports but does not gate on.
- **D-040 (Takeover implementation)**: The purge residue check runs *before*
  directory removal. Ordered the other way it was unreachable — any surviving
  file inside the session tree made `_remove_dirs` raise
  `OSError("Directory not empty")` first — so what actually protected
  finalization was an incidental errno rather than the domain refusal. The
  guard now raises `PurgeRefused`, and the regression test asserts that type
  rather than accepting either exception.
- **D-041 (Live-provider integration)**: Realtime translation disables
  provider-side reasoning via `chat_template_kwargs.enable_thinking=false`.
  `mimo-v2.5` is a reasoning model and by default spends most of its budget in
  `reasoning_content`. Measured against the live API on 2026-09-11 over five
  lecture sentences: p50 latency 5.40s -> 2.29s (max 8.31s -> 2.54s) and
  completion tokens ~152 -> ~14, with no quality loss on any sample. Sentence
  translation does not need deliberation, and a caption five seconds late is
  not realtime. Only this spelling works -- top-level `enable_thinking` and
  `reasoning_effort=low` were both ignored by the provider. Summary generation
  deliberately keeps reasoning: it is offline, quality-sensitive, and nobody is
  waiting on it live.
- **D-042 (Live-provider integration)**: ASR transcripts are stripped of chat
  template scaffolding before they leave the provider adapter. The real
  `mimo-v2.5-asr` endpoint wraps the transcript inconsistently -- the same
  request shape returned `<chinese> Good morning, everyone.`,
  `think>\n<chinese> Today we will...` (an unbalanced `</think>` remnant), and
  unwrapped text, across calls. The wrapper cannot be disabled: both
  thinking-off spellings still produce it for the ASR model. Because the English
  transcript is the factual source for the summary, these tokens are removed in
  `parse_asr_response` rather than downstream. Stripping is prefix-anchored and
  never touches the interior of a transcript, so a lecturer who says "think" or
  dictates an XML tag keeps every word.
- **D-043 (Latency)**: First-caption latency is dominated by the segment wait,
  not the provider. Measured live 2026-09-11: ASR ~3.6s + translation ~2.3s =
  ~6s of API time against a 12s segment wait, so the wait was two thirds of the
  ~18s delay. `segment_preferred_seconds` drops 12 -> 6; `max` stays 30 so a
  lecturer who never pauses still produces bounded output. **Streaming was
  measured and rejected**: MiMo's SSE stream delivered its *first* token at p50
  3.09s, later than a complete non-streamed reply at 2.29s, so it adds
  complexity and latency at once. The English transcript is published as soon
  as ASR returns rather than waiting for Chinese, so perceived latency is the
  ASR leg alone.
- **D-044 (Auto-pause)**: Recording auto-pauses after
  `auto_pause_silence_seconds` (default 30, 0 disables) of continuous silence.
  The threshold is measured against the segmenter's durable `silence_run` --
  silence in *captured audio* -- not a wall clock, so a stalled pipeline cannot
  fake it and a restart resumes the count. It is ignored while an utterance is
  open, because a pause inside a sentence is speech rhythm, not an idle room.
  Settings reject a threshold at or below `segment_silence_milliseconds`, which
  would pause the class on every ordinary sentence gap.
- **D-045 (Accuracy)**: A per-course glossary is injected into both the ASR and
  the translation prompt. Measured on synthesized lecture audio, residual
  transcription error was almost entirely domain-term spelling
  ("heteroscedasticity" for "heteroskedasticity") rather than misheard speech,
  and those errors land in the English transcript that the summary treats as
  fact. For ASR the terms act as spelling anchors and carry an explicit
  non-native-accent hint; for translation a term may pin an exact Chinese
  rendering so terminology does not drift between synonyms across segments.
  Terms are keyed by `course_name`, so a weekly course keeps its vocabulary.
  A glossary is **user configuration, not classroom content** -- it holds only
  what the user typed -- so it is absent from `CONTENT_TABLES` and deliberately
  survives purge. The list is capped at 60 entries: past that, extra terms stop
  helping and start crowding the instruction. Providers take a
  `glossary_provider` callable rather than a new protocol argument, so the
  `ASRProvider` / `TranslationProvider` protocols and every mock stay unchanged.

## MiMo documentation snapshot (retained from Phase 0)

Official sources checked:

- [Speech Recognition API](https://mimo.mi.com/docs/en-US/api/audio/Speech-Recognition)
- [Speech Recognition usage guide](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/Speech-Recognition)
- [OpenAI Chat Completions compatibility](https://mimo.mi.com/docs/en-US/api/chat/openai-api)
- [Rate limits](https://mimo.mi.com/docs/en-US/api/guidance/rate-limit)
- [Error codes](https://mimo.mi.com/docs/en-US/api/guidance/error-codes)

Confirmed at review time:

- `POST https://api.xiaomimimo.com/v1/chat/completions` is documented for ASR and text.
- ASR accepts one Base64 WAV/MP3 audio item and documents a 10 MB encoded limit.
- ASR model is `mimo-v2.5-asr`; `asr_options.language=en` is supported.
- Text models include `mimo-v2.5` and `mimo-v2.5-pro`.
- Published per-account model limits currently show 100 RPM for these models; ASR shows 10K TPM and text models 10M TPM.
- The error guide includes 429, 500, and 503. Retry policy must still be bounded and category-aware.

Not confirmed by documentation alone:

- target account entitlement, regional availability, balance, or effective concurrency;
- maximum recommended audio duration below the 10 MB payload cap;
- realtime classroom P50/P95/P99 latency and recognition quality;
- provider idempotency behavior;
- exact meaning of ASR TPM for capacity planning;
- retention/privacy terms appropriate to the user's classroom use.

These are integration or policy blockers, not facts to guess.
