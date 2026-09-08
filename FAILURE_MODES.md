# Classroom Interpreter — Failure Modes

Status: `PHASE_0_REVIEW_CANDIDATE`  
Last updated: `2026-09-05`

## Failure policy

Failures are classified before retry. Capture durability, queue state, and purge authorization fail closed. Once an audio chunk or segment is durable, a network/provider fault must not delete it. When continued capture itself cannot be guaranteed, the UI must show a red/degraded state and record an explicit gap.

Definitions:

- **No data loss**: all successfully sealed capture data remains recoverable. It does not claim preservation of audio that the OS/device never delivered or an unsealed crash tail.
- **Bounded tail risk**: only the currently unsealed, bounded capture buffer may be lost; Phase 2 must measure the bound.
- **Recovery required**: automatic work pauses safely and the user sees a resumable session/action.
- **Terminal provider error**: authentication, balance, regional denial, content filter, or retry budget exhaustion; no infinite retry.

## Required failure matrix

| ID | Failure | Expected behavior | Data-loss expectation | Recovery strategy | Planned coverage |
|---|---|---|---|---|---|
| F-001 | Wi-Fi off for 30 seconds | Capture and segmentation continue; API workers back off; delay turns yellow | no loss of sealed audio | reconnect and drain in order | integration + failure test with network fault proxy |
| F-002 | Wi-Fi off for 5 minutes | Same as F-001; disk-backed backlog grows and delay is explicit | no loss of sealed audio if disk remains safe | bounded retries, provider circuit cooldown, automatic catch-up | failure + accelerated backlog soak |
| F-003 | MiMo ASR timeout | Lease attempt fails as `TIMEOUT`; segment remains retryable | none | capped exponential backoff with jitter | unit + integration mock |
| F-004 | MiMo translation timeout | English remains durable/current; commit waits | none | retry translation only | unit + integration mock |
| F-005 | HTTP 429 | honor valid `Retry-After`, reduce concurrency, surface throttled state | none | bounded backoff; recovery if budget exhausted | mock contract + failure test |
| F-006 | HTTP 500 | retry within budget; no state loss | none | backoff and later drain | mock contract + failure test |
| F-007 | HTTP 503 | mark provider overloaded; pause new provider claims briefly | none | cooldown/backoff and drain | mock contract + failure test |
| F-008 | 401/403/402 auth, region, or balance | stop blind retries; red actionable status | none | user fixes access, then explicit resume | unit + integration mock; safe-error check |
| F-009 | Malformed/empty ASR response | reject result; never set `ASR_DONE` | none | classify `MALFORMED`, retry within budget | provider contract test |
| F-010 | Malformed/empty translation | reject result; never set `DONE` | none | retry; then recovery required | provider contract test |
| F-011 | One segment exhausts retries | finalization stops; no successful incomplete summary | none | `RECOVERY_REQUIRED`; explicit retry after issue fixed | recovery test |
| F-012 | Process crash while recording | sealed chunks survive; active tail may be bounded loss | bounded unsealed tail only | startup reconciliation; offer Resume Recovery | forced-kill recovery test |
| F-013 | FastAPI restart | capture helper behavior depends on chosen supervision; durable work survives | bounded tail if capture process also dies | discover expired leases/orphans and resume | recovery integration test |
| F-014 | Browser refresh/close | recording continues because browser does not own microphone | none | UI reconnects with last event sequence and replays DB state | UI integration/E2E |
| F-015 | Microphone permission denied/revoked | Start fails or active capture turns red; no fake live state | audio after OS denial was never captured; prior sealed data safe | show exact action, seal current data, retry after permission restored | target-mac manual + adapter test |
| F-016 | Input device disconnect | seal available data, record gap, attempt controlled reopen/new epoch | gap while device unavailable; prior data safe | user selects device or automatic same-device reconnect | target-mac failure test |
| F-017 | Mac idle sleep attempt | app holds idle-sleep assertion while recording | none if assertion works | release assertion after end/pause policy | target-mac test |
| F-018 | Lid close, forced sleep, or power loss | cannot capture while truly asleep; on wake mark gap and recover | unavoidable uncaptured gap; sealed data safe | new capture epoch, UI warning, resume queues | target-mac manual + recovery test |
| F-019 | Disk low before critical threshold | health turns yellow; warn with estimated remaining time | none | user frees space; continue if safe | simulated disk quota test |
| F-020 | Disk full/write failure | stop claiming work; try to seal; never pretend recording continues | active/unsealed tail at risk; durable data safe | `RECOVERY_REQUIRED`, free space, reconcile | fault-injected filesystem test |
| F-021 | SQLite busy/lock | short bounded retry; no provider call inside transaction | none | busy timeout, serialize writer, recover expired lease | concurrency integration test |
| F-022 | SQLite corruption/I/O error | stop state mutation and purge; protect files | durable files may remain; DB work unavailable | backup/recovery procedure; no auto-delete | fault injection + documented manual recovery |
| F-023 | Summary API failure | remain `SUMMARIZING`/recovery; retain all inputs | none | resume hierarchical job with bounded retry | summary failure test |
| F-024 | Summary returns empty/truncated/invalid | validation rejects; purge gate closed | none | retry or user recovery | unit + integration mock |
| F-025 | Summary generated but file write fails | never set `SUMMARY_SAVED` | none | retain inputs; retry atomic write | filesystem fault test |
| F-026 | Summary written but read-back/hash fails | never authorize purge | none | quarantine/overwrite temp safely; retry | filesystem fault test |
| F-027 | Summary saved; crash before status update | final file may be valid but DB not advanced | none | recovery independently validates file and resumes transition | forced-kill recovery test |
| F-028 | Crash during purge | final summary remains protected; some temp files may already be absent | no loss of final summary; temp partial deletion expected | revalidate summary and continue manifest idempotently | forced-kill purge test |
| F-029 | Purge file deletion partially fails | remain `PURGING`/recovery; do not claim clean | none of final summary | retry exact failed entries; report residue | permission fault test |
| F-030 | Purge path traversal/symlink/bad manifest | cleanup refuses operation | none | manual review; regenerate safe manifest | security unit tests |
| F-031 | 1000+ segments | stable ordering, bounded memory, indexed DB access | none | backpressure and drain | scale/soak test |
| F-032 | Long silence | durable capture policy remains bounded; no flood of empty ASR calls | no speech content loss | VAD keeps liveness and seals recovery chunks | audio fixture + soak |
| F-033 | Continuous long speech | force a segment at configured maximum with overlap | no deliberate drop; possible boundary accuracy tradeoff | next segment retains overlap/context | audio fixture test |
| F-034 | High noise | avoid pathological microsegments within VAD limits; show quality risk | captured audio preserved; ASR accuracy may degrade | configuration and user warning; no invented transcript correction | noisy fixture + real test |
| F-035 | Provider throughput below realtime | queue grows on disk, live delay rises, capture continues | none while disk capacity is safe | catch up when throughput >1x; recovery if capacity unsafe | throttled-provider soak |
| F-036 | Duplicate worker/process | leases prevent simultaneous valid commits | possible duplicate provider cost; no duplicate commit | compare-and-swap/idempotent result write | concurrency test |
| F-037 | Out-of-order provider completion | later results wait for ordered publication | none | SQLite-backed sequence/replay | randomized-latency integration test |
| F-038 | WebSocket disconnect/slow client | drop/rebuild client connection, not pipeline data | none | reconnect with cursor and replay committed rows | UI integration + soak |
| F-039 | Memory pressure | bounded buffers; health warning; no full transcript loaded for live translation | none unless OS kills process, then F-012 | tune worker/context bounds; restart recovery | soak/resource test |
| F-040 | API key appears in exception | redact before log/UI; treat as security failure | classroom data unaffected; credential exposure prevented | sanitization and secret rotation guidance | unit test + repository scan |

## Retry policy categories

| Category | Default retry? | Notes |
|---|---|---|
| network disconnect / timeout | yes | capped exponential backoff with jitter |
| 429 rate limit | yes | honor valid `Retry-After`; adapt concurrency |
| 500 / 503 | yes | bounded server-failure budget and cooldown |
| malformed / empty response | limited | contract failure; retain safe response metadata only |
| 401 / 403 / 402 | no automatic loop | require credential, region, or balance action |
| content filter | no blind retry | preserve source; surface recovery action without logging content |
| local disk / database error | no provider work | protect data and block purge until local state is safe |

Exact attempt counts and time caps are configuration proposed and validated in Phase 8. “Bounded” is mandatory; the initial numeric defaults are not frozen in Phase 0.

## Recovery precedence

1. protect an existing valid final summary;
2. prevent unauthorized purge;
3. preserve sealed audio and segment files;
4. reconcile durable files and database state;
5. expire stale leases and resume incomplete stages;
6. restore UI state and explain any capture gap;
7. resume performance optimizations only after correctness is restored.
