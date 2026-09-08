# Classroom Interpreter — Test Plan

Status: `PHASE_1 REVIEW CANDIDATE`  
Last updated: `2026-09-06`

## Test objective

Prove that V1 can preserve and process a two-to-three-hour class on the target Mac, recover from realistic failures, save a usable structured summary, and purge temporary content only after the save is valid.

Tests are evidence layers. A mock test does not prove MiMo access; a real MiMo test does not prove three-hour stability; a green backend suite does not prove the student-facing flow.

## Evidence classes

| Code | Evidence | Proves | Does not prove |
|---|---|---|---|
| E1 | pure unit/contract test | deterministic logic and schema behavior | OS, DB, provider, or UX behavior |
| E2 | local integration with SQLite/filesystem/mocks | persistence, transitions, failure routing | real MiMo or target-mic behavior |
| E3 | target macOS hardware test | microphone, permission, device, sleep, resources | cloud quality at scale |
| E4 | real MiMo short integration | current endpoint/auth/model/payload and sample quality | long-run stability or quota sufficiency |
| E5 | accelerated three-hour-equivalent workload | scale, ordering, backlog, recovery under equivalent segments/audio minutes | wall-clock leaks/timers |
| E6 | wall-clock three-hour target-macOS microphone soak with mock providers | microphone/permission/sleep behavior, timers, handles, memory/resource trend, UI/WS longevity | real MiMo long-run cost/availability |
| E7 | Oracle user-flow audit | actual end-to-end usability and truthfulness | exhaustive internal edge cases |

## Test suites

### Unit

- state-transition allow/deny tables for session, segment, and summary;
- path canonicalization, symlink rejection, and purge-manifest validation;
- summary schema/non-empty/truncation validation;
- provider error classification and redaction;
- bounded backoff/jitter and `Retry-After` parsing;
- context-window count/time/token bounds;
- VAD segmentation rules with silence, maximum duration, and overlap;
- event ordering/replay cursor behavior;
- latency percentile calculation without content;
- environment configuration and localhost-only defaults.

### Integration

- atomic segment file then `CAPTURED` row;
- orphan-file and missing-file reconciliation;
- SQLite WAL concurrency, busy timeout, leases, lease expiry, and idempotent commits;
- complete mocked flow from synthetic audio to committed subtitle;
- randomized ASR latency with contiguous, serial translation context and ordered publication;
- WebSocket disconnect/refresh/cursor replay;
- End Class drain blocks until all segments are `DONE`;
- hierarchical summary resume and reuse of completed section summaries;
- atomic summary write, read-back, and hash verification;
- purge removes exact temporary content and retains exact summary.

### Failure and recovery

Every row F-001 through F-040 in `FAILURE_MODES.md` receives one automated or explicitly manual case. Cases must capture:

- initial durable state;
- injected fault and timing boundary;
- observed transition sequence;
- files/rows preserved or removed;
- user-visible health/recovery result;
- restart behavior;
- data-loss assessment.

Forced termination points include before/after file rename, DB insert, provider response, result commit, summary rename, `SUMMARY_SAVED`, each purge step, and `PURGED`.

### Provider contract tests

`MockASRProvider` and `MockTranslationProvider` support seeded scenarios:

- fixed and random latency;
- throughput slower/faster than realtime;
- timeout, network error, 429 with/without `Retry-After`, 500, 503;
- 401/402/403 terminal errors;
- empty, malformed, truncated, or unexpected response;
- duplicate response/request IDs;
- out-of-order completion.

Mocks must never be selected silently outside test configuration.

Real MiMo tests are short, opt-in, cost-labelled, and skipped when credentials are absent. They verify the officially documented payload and parse path, explicit English language option, translation contract, summary contract, safe errors, and response metadata. They do not log request content or credentials.

### Audio fixtures

Only synthetic or explicitly licensed/consented fixtures enter the repository:

- silence;
- short English utterances with expected boundaries;
- long continuous speech;
- noisy/far-field sample;
- numbers, formulae, abbreviations, insurance/risk/statistics terms;
- boundary speech spanning recovery capture chunks;
- sample-rate/device format conversion.

No real classroom recording becomes a committed fixture.

## Three-hour strategy

### PR/iteration gate: accelerated three-hour-equivalent (E5)

Feed at least three hours of synthetic audio duration and 1000–3000 segments without waiting three wall-clock hours. Include seeded latency, 429/500/timeouts, silence, long speech, disconnect periods, restart, and slow-provider backlog. Verify:

- zero missing durable segment sequences;
- zero duplicate committed subtitles;
- committed order strictly increases;
- all retryable work eventually drains after recovery;
- no unbounded in-memory queue/context;
- database and disk growth matches documented temporary content only;
- file handles and task counts return near baseline after drain;
- final summary gate and purge leave one final summary content artifact.

### Release gate: real wall-clock three-hour macOS hardware soak (E6)

Run the backend, frontend/WebSocket client, real target-Mac microphone capture path, SQLite, and mock providers for at least three continuous wall-clock hours. Periodically refresh/disconnect the UI and exercise permission observation, sleep/wake handling, and audio-device anomalies. Sample RSS, microphone/file handles, task/thread counts, SQLite WAL/DB size, disk use, queue depth, and latency.

Pass requires no crash, data loss, duplicate commit, ordering error, stuck lease, permanent backlog after provider recovery, or monotonic unexplained resource growth. Numeric leak thresholds are established from Phase 2/8 baselines and frozen before Phase 9; they cannot be invented after seeing a failing result.

E5 and E6 must both pass before the product may claim three-hour stability. An accelerated pass never substitutes for wall-clock macOS hardware evidence.

### Real provider confidence run (E4)

Use a shorter, bounded, consented or synthetic English audio run against MiMo. Confirm endpoint/model access, rate behavior, English ASR, translation, hierarchical summary sample, and safe error handling. Long real-provider soak is optional and budget-controlled; it cannot replace E6.

## Performance measurements

For every completed segment record:

- `audio_end → ASR_done`;
- `ASR_done → translation_done`;
- `audio_end → subtitle_committed`.

Report P50/P95/P99, queue depth, oldest pending age, processed-audio/elapsed-audio ratio, and catch-up time. Do not freeze a latency SLA until measured in at least one representative classroom environment.

## Data lifecycle assertions

Before purge:

- all source sequences contiguous and `DONE`;
- final summary required structure valid;
- atomic write/read-back/hash evidence exists;
- final path is outside session path.

After purge:

- raw/recovery audio absent;
- ASR segments absent;
- transcript/translation/context and summary-work content absent;
- queue/attempt/session-linked content rows absent;
- debug content and caches absent;
- final summary present, readable, unchanged;
- `purged_session_metadata` contains exactly the approved fields and no content columns;
- normal logs contain no classroom content/API key.

## Security and privacy checks

- secret scan Git history and working tree;
- assert `.env`, app-data root, audio, DB, transcripts, summaries, and debug dumps are ignored;
- fuzz purge manifest paths and symlinks;
- verify loopback bind and reject external interface configuration in V1;
- inspect logs/UI errors for credential or content leakage;
- verify provider payload minimization by stage.

## Traceability to Definition of Done

| Product outcome | Required evidence |
|---|---|
| Mac microphone records continuously | E3 target-Mac permission/capture plus E6 wall-clock run |
| English ASR / Chinese translation | E4 real MiMo plus E1/E2 contract/failure cases |
| Correct realtime ordered subtitles | E2 randomized ordering + UI replay; E7 user-flow audit |
| Network failure does not lose captured audio | F-001/F-002/F-035 via E2/E5 |
| Restart recovers unfinished session | forced-kill cases F-012/F-013/F-027/F-028 |
| End Class drains before summary | E2 transition and incomplete-segment cases |
| Summary valid and durable | E2 atomic write/fault tests + E4 sample + E7 audit |
| Purge retains only final summary content | E2 lifecycle assertions + forced-kill purge + E7 audit |
| No obvious memory/resource leak | E5 accelerated plus E6 wall-clock trends |
| API key absent from Git/logs | secret scan + redaction tests + reviewer inspection |
| Three-hour stability | E5 and E6 both pass |
| Final acceptance | independent Reviewer, Validator, Oracle reports all PASS |

## Test report format

```text
Test ID / Phase:
Evidence class:
Environment and versions:
Command or manual procedure:
Duration / workload:
Expected result:
Actual result:
Artifacts and metric paths:
Content/secret handling check:
PASS / FAIL / BLOCKED:
Unknowns:
```

## Phase 0 validation

Phase 0 has no application code and therefore no product test result. Its validation consists of document consistency, Mermaid/state-machine review, schema/interface coverage, official MiMo documentation verification, and a repository secret/status check. Product behavior remains unimplemented and untested until later Phases.
## Phase 2 spike execution register — 2026-09-06

Scope: audio-only. Automated fixtures are deterministic generated PCM; no real
classroom input is embedded in tests. Ordinary pytest never acquires a microphone.

- `tests/audio/test_capture.py`: rolling WAVs, checksum and full-payload readback,
  metadata, relative/monotonic timing, atomic visibility, orphan tails, restart
  adoption, resampling, mono conversion, pause epochs, gap fault, ownership.
- `tests/failure/test_audio_failures.py`: slow writer/queue overflow, disk guard,
  writer/seal/normalizer exceptions, absent device, stop failure, lifecycle race,
  cancellation releasing capture.
- `tests/failure/test_audio_process.py`: actual child SIGKILL and isolated restart,
  orphan rejection, SIGINT and SIGTERM graceful tail finalization.
- `tests/hardware/README.md`: opt-in built-in microphone 5/30/180 minute protocols,
  permission matrix, sleep/wake and device interruption manual protocol.

Exact run results, machine/resource evidence and deferred gates belong in
`PHASE2_REVIEW.md`. A generated capture_test_pass does not certify production or
the entire phase. User has deferred the three-hour real recording; do not infer it
from the accelerated 10,800-second test.
