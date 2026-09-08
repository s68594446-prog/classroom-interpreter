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
