# Classroom Interpreter — Harness Rules

This file governs all development work in this repository. It separates runtime agents from development-harness roles.

## 1. Frozen working principles

- Do not expand V1 scope.
- Inspect `git status`, relevant diffs, and the Phase documents before editing.
- Preserve user files, credentials, runtime data, and unrelated changes.
- Architecture comes before implementation. Record material architecture changes in `DECISIONS.md` first.
- Make the smallest change that satisfies the active Phase.
- Never call an assumption, mock result, or green unit test “production verified.”
- Never expose an API key, classroom audio, transcript, translation, or summary input in logs, fixtures, screenshots, or commits.
- Do not weaken `PERSIST FIRST, PROCESS SECOND` or `NO VALID SUMMARY = NO PURGE`.
- Do not add providers, languages, platforms, accounts, cloud deployment, diarization, or decorative features in V1.

## 2. Required Phase order

1. Phase 0 — Architecture
2. Phase 1 — Skeleton
3. Phase 2 — Audio Pipeline
4. Phase 3 — ASR
5. Phase 4 — Translation
6. Phase 5 — Live UI
7. Phase 6 — Final Summary
8. Phase 7 — Data Purge
9. Phase 8 — Reliability
10. Phase 9 — 3 Hour Soak Test
11. Phase 10 — Final Audit

Do not begin a later Phase until the prior Phase has an accepted commit report. A Phase may prepare an interface required by the next Phase, but it may not implement that next Phase's behavior.

### 2.1 User-authorized continuous implementation exception — 2026-09-08

The project owner has explicitly authorized Zcode to implement the remaining project
through Phase 10 in one continuous overnight build, without pausing for intermediate
Phase approval. This exception changes implementation scheduling only:

- keep the Phase order and create a separate checkpoint/report for every Phase;
- do not collapse agent boundaries or frozen safety invariants;
- later work may proceed while earlier evidence gates remain open, but every such gate
  remains visibly `UNVERIFIED` or `FAIL`; implementation is not acceptance;
- Zcode may act as Builder and Repair Agent, but may not self-sign independent Reviewer,
  Validator, or Oracle approval;
- no real-data purge, macOS privacy-setting mutation, automatic sleep/lid/device action,
  or secret disclosure is authorized by this scheduling exception;
- Codex remains the independent Gatekeeper after the build and will review/fail/return
  issues phase by phase;
- the project may be called a full implementation candidate, not production-ready, until
  all real MiMo, target-macOS, three-hour, purge, reliability, and independent audit gates pass.

## 3. Development Harness roles

Roles are independent review responsibilities. They may be performed by separate agents or people; no implementation is accepted solely because its author says it works.

### Architect

- Owns architecture, contracts, state machines, data lifecycle, failure model, and acceptance criteria.
- Does not perform large implementation work.
- Marks facts as verified, inferred, proposed, or unknown.
- Records tradeoffs and architecture changes in `DECISIONS.md`.

### Builder

- Implements only the accepted Phase plan and interfaces.
- Does not silently change architecture or acceptance criteria.
- Adds focused tests and reports exact commands/results.
- Stops and raises a decision when implementation conflicts with a frozen invariant.

### Reviewer

- Reviews boundaries, concurrency, state transitions, ordering, secrets, error classification, resource ownership, data-loss paths, and cleanup scope.
- Treats retries, cancellation, restart, partial writes, and duplicate delivery as first-class cases.
- Produces findings with file/line evidence and severity; does not use “looks good” as a verdict.

### Validator

- Runs the applicable unit, integration, failure, recovery, and soak tests.
- Distinguishes deterministic mock evidence from real MiMo and real macOS evidence.
- Records environment, command, duration, result, and artifacts.
- Does not change code while acting as Validator.

### Oracle Reviewer

- Rechecks the product goal independently: can a student actually run a complete two-to-three-hour class on the target Mac?
- Examines UX, permission flow, backlog visibility, recovery choices, End Class behavior, saved-summary usability, and purge truthfulness.
- Does not infer user success from internal test status alone.

### Repair Agent

- Fixes only confirmed failures with the smallest scoped patch.
- Adds or updates a regression test for the failure.
- Does not refactor unrelated modules or redefine acceptance criteria.
- Returns the work to Reviewer, Validator, and Oracle gates.

## 4. Phase completion loop

```text
PLAN
→ IMPLEMENT
→ SELF CHECK
→ REVIEW
→ TEST
→ failure? REPAIR → REVIEW → TEST
→ COMMIT REPORT
→ NEXT PHASE only after approval
```

Every report must include:

- Phase and scope;
- files changed;
- decisions added or changed;
- tests run with exact results;
- evidence classification: local deterministic, simulated provider, real MiMo, target macOS, or not tested;
- known failures and unknowns;
- whether privacy and purge invariants were affected;
- recommended next Phase.

## 5. Runtime-agent boundary rules

- Runtime agents depend on shared contracts or public ports, not another agent's private module.
- Orchestrator coordinates states; it does not perform capture, ASR, translation, summary, or cleanup itself.
- Provider calls never occur inside SQLite write transactions.
- Audio capture never waits on network work.
- Queue rows are the source of truth; in-memory queues are only wake-up hints.
- Context is bounded by time/count/token configuration.
- Ordinary logs are content-free.
- Cleanup accepts exact, validated session manifests only and cannot delete the application root or summary store.
- Mock providers must be injectable and deterministic under a seed.
- No silent provider/model fallback.

## 6. Required evidence by Phase

| Phase | Minimum evidence before acceptance |
|---|---|
| 0 Architecture | All required documents internally consistent; diagrams/state machines/schema/interfaces reviewed; official MiMo facts linked; unknowns explicit |
| 1 Skeleton | Backend/frontend start locally; config validation; SQLite schema/migrations; state machines and idempotency tests; contracts import/type-check; localhost bind; secret scan |
| 2 Audio | Python/CoreAudio-compatible spike first; target-Mac permission/reconnect and capture-only three-hour check; freeze adapter ADR; segmentation fixtures; crash-tail recovery; bounded memory/disk behavior |
| 3 ASR | Mock error matrix; real short MiMo English test; durable retry/lease/restart evidence; request payload contract test |
| 4 Translation | Context-window bounds; prompt/output contract tests; terminology/number fixtures; real short MiMo test |
| 5 UI | Ordered current/committed display; refresh/reconnect replay; controls and health states; no capture ownership in browser |
| 6 Summary | Hierarchical chunking, resume, invalid/empty output rejection, atomic write/read-back tests; real short summary test |
| 7 Purge | Gate invariant, path containment, partial purge recovery, retained-summary verification, no content-log residue |
| 8 Reliability | Required failure matrix passes; restart/sleep/device/low-disk behavior documented and measured |
| 9 Soak | Accelerated equivalent plus real wall-clock target-macOS microphone soak with mock providers; short real MiMo run; resource/ordering/data-loss report |
| 10 Audit | Reviewer, Validator, and Oracle PASS; every Definition of Done item mapped to evidence |

## 7. Git and secret hygiene

- `.env`, runtime databases, sessions, audio, transcripts, translations, summaries, debug content, test recordings, and generated reports are ignored unless a deliberately synthetic fixture is reviewed and allowlisted.
- `.env.example` contains names and safe defaults only.
- Never commit a real API key or real classroom sample.
- Before a commit report, inspect full `git status` and relevant diff; run a secret/content scan appropriate to the files changed.

## 8. Historical Phase stop rules and current override

Historical state before the 2026-09-08 owner override: Phase 1 was approved and
Phase 2 Audio Capture Spike alone was authorized. MiMo, translation, summary and purge
were excluded from Phase 2, and a simulated three-hour run could not substitute for
real hardware time. Those Phase-specific scope boundaries and evidence distinctions
still apply to the individual checkpoint reports.

Phase 0 creates architecture documents and a minimal empty directory skeleton only. Do not add application dependencies, executable backend/frontend code, migrations, or runtime data. Stop after the Architecture Review and wait for user approval.

Phase 0 was approved with required amendments on 2026-09-06. Phase 1 was approved.
Under the user-authorized continuous implementation exception above, Zcode may now
implement later-Phase behavior in the required order, while preserving separate Phase
checkpoints and honest evidence status. The historical restrictions remain evidence of
what was excluded from Phase 1/2; they no longer impose a mandatory stop between later
Phase implementations. They still prevent later-Phase behavior from being misreported
as Phase 2 work or unverified implementation from being reported as accepted evidence.
