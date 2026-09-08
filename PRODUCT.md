# Classroom Interpreter — Product Contract

Status: `V1_SCOPE_FROZEN — PHASE_1_REVIEW_CANDIDATE`  
Last updated: `2026-09-06`

## Product promise

A student can bring a MacBook Air to an approximately two-to-three-hour English lecture, start one local session, read near-real-time English and Simplified Chinese subtitles, end the class, receive one structured Chinese summary, and have the temporary classroom data removed only after the summary is safely verified.

The promise is reliability and recoverability, not feature breadth or a guaranteed sub-second subtitle delay.

## Primary user journey

1. User opens the local application and sees microphone, storage, and provider readiness.
2. User enters a course name and clicks **Start Class**.
3. The application captures audio independently of the browser tab, shows a timer and health state, and processes speech in natural segments.
4. English appears as current text after ASR; English plus Chinese becomes committed history after both are durable.
5. Network/provider delays increase the displayed live delay but do not discard captured audio.
6. User may pause and resume. The application records a visible gap rather than pretending audio exists.
7. User clicks **End Class**. New capture stops, all persisted work drains, and summary progress is shown.
8. The application writes and reads back a valid final summary.
9. Only then does it purge raw and intermediate data.
10. On a later launch, the user can open the saved summary. If the prior run crashed, the user sees a recovery option rather than silent deletion.

## V1 in scope

- macOS local operation;
- one active session at a time;
- Mac microphone input;
- English ASR;
- English to Simplified Chinese translation;
- current and committed realtime subtitles;
- short-term translation context;
- MiMo cloud API through provider adapters;
- SQLite durable processing queue;
- retry, backoff, backpressure, health status, and crash recovery;
- two-to-three-hour class operation and a three-hour soak gate;
- hierarchical post-class summary;
- automatic temporary-data purge after verified summary save;
- localhost-only React/TypeScript UI and Python/FastAPI backend.

## Explicitly out of scope

- Cantonese, Mandarin translation, or languages other than English to Simplified Chinese;
- iOS, Android, Windows, mobile companion apps;
- login, account, payment, subscription, cloud sync, or collaboration;
- teacher voiceprints, speaker identity, or diarization;
- browser extension;
- SaaS, Cloudflare, microservices, or remote deployment;
- alternative AI providers in V1;
- automatic content correction of committed subtitles;
- decorative animation, themes, or marketplace features;
- forensic secure erase claims;
- automatic deletion of an unfinished/recoverable session without user choice.

## Functional requirements

### Session controls

- Start, Pause, Resume, and End Class are idempotent commands with visible state.
- End Class stops new capture and drains existing work; it never deletes immediately.
- One active recording session is allowed.

### Capture and segmentation

- Capture is continuous while `RECORDING`, even if MiMo is unreachable.
- Memory use is bounded; durable capture chunks and segments live on disk.
- Semantic segments use local VAD/silence plus configured duration bounds, not fixed ASR cuts alone.
- Every ASR segment is durably persisted before processing.

### ASR and translation

- ASR is explicitly biased to English when supported.
- Translation outputs only natural, faithful Simplified Chinese for the current segment.
- Translation receives bounded recent context and cannot summarize, answer questions, or add information.
- Numbers, formulae, symbols, domain terms, abbreviations, and names are preserved.
- API errors are classified; retryable errors use finite backoff and non-retryable errors require recovery/action.

### Subtitle UX

- The header shows course name, session timer, `LIVE`/paused/finalizing state, and component health.
- Each committed item shows timestamp, English, and Chinese.
- `CURRENT` may change while incomplete; `COMMITTED` history is stable.
- Reconnect replays persisted committed events in order without duplicate display.
- Live delay is visible when processing falls behind.

### Summary

Summary runs only after capture is sealed and every segment is `DONE`. The English ASR transcript is the classroom factual source of truth. Chinese translation helps organize the final Chinese explanation, but facts, figures, formulae, conditions, technical terms, abbreviations, and proper names are checked against English. If English and Chinese conflict, Summary Agent follows English or marks the point `不确定`; it does not compound an ASR → translation → summary error chain.

The final Markdown summary starts with `课程名称`、`日期`、`课程时长`, followed by exactly these sections:

1. 本节课主题
2. 核心知识点
3. 重要定义
4. 重要公式 / 数字 / 条件
5. 老师重点强调内容
6. 老师使用的重要例子
7. 容易混淆的概念
8. 逻辑关系 / 因果关系
9. 需要复习的内容
10. 本节课完整结构化总结

The generated content must not invent facts. Unclear source content is marked `不确定`. Professional terms use Chinese with the English term in parentheses where helpful.

### Purge

- `NO VALID SUMMARY = NO PURGE`.
- Validity requires non-empty required sections, atomic write, appropriate flush/sync, successful read-back, and matching content hash.
- Cleanup deletes only the exact session manifest: raw capture, segments, transcripts, translations, queue rows, intermediate summaries, debug content, and temporary caches.
- Final summary remains outside the session directory.
- Purge is idempotent and recoverable after partial completion.

## Non-functional requirements

### Reliability

- Already captured audio survives Wi-Fi loss, MiMo failure, browser refresh, and backend restart within documented filesystem durability limits.
- No unbounded RAM queue or transcript context growth.
- Queue delivery is at-least-once with idempotent result commits and stable ordering.
- Three-hour equivalent and release wall-clock soak tests show no data loss, duplicate commits, task leaks, or uncontrolled resource growth.

### Performance

- Record `audio_end → ASR_done`, `ASR_done → translation_done`, and `audio_end → subtitle_committed` at P50/P95/P99.
- No hard latency SLA is claimed before real classroom measurement.
- Reliability takes priority over shaving latency at the cost of durability.

### Privacy

- Local-first with cloud AI processing, never marketed as fully local.
- MiMo receives only the data needed for the active stage.
- API keys and classroom content are absent from ordinary logs.
- Final summary is the only persistent classroom-content artifact after purge. A metadata-only purge ledger may retain the approved fields in `DATA_LIFECYCLE.md`.
- V1 deletion is logical filesystem deletion and not guaranteed forensic secure erase on SSD/APFS.

### Operability

- `/health` and the UI expose microphone, ASR, translation, queue, network/provider outcome, memory, disk, throughput, and heartbeat status.
- Health reason codes are actionable and content-free.
- Recovery identifies unfinished sessions and offers Resume Recovery or explicit Delete Session.

## Definition of done

V1 may be called complete only when all of the following have direct evidence:

- Start Class captures Mac microphone audio continuously.
- English ASR and Simplified Chinese translation succeed through the real MiMo adapter.
- Current/committed subtitles display in correct order.
- Network and provider failure do not discard already captured audio.
- Retry and persistent queue behavior pass deterministic tests.
- Restart discovers and resumes unfinished work.
- End Class drains all segments before summary.
- A structured summary is atomically saved and read back.
- Purge runs only after `SUMMARY_SAVED` and leaves only the final summary content.
- API keys are absent from Git and logs.
- Failure and recovery gates pass. Both the accelerated three-hour-equivalent workload test and the real wall-clock three-hour macOS hardware test pass; neither substitutes for the other.
- Independent Reviewer, Validator, and Oracle reports pass with evidence.

Green unit tests alone are not completion evidence.
