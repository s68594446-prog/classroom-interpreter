# CLASSROOM INTERPRETER — PHASE 5 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — NOT ACCEPTED.** All evidence below is local
deterministic (E1/E2) with a backend-owned fake capture stand-in; no microphone
was opened (real capture remains HARDWARE NOT TESTED from Phase 2).

## Scope

Live UI + runtime composition: session lifecycle REST API, WebSocket replay
from persisted cursor, in-process worker runtime (D-025), orchestrator with
drain semantics, real health components, and the React/Vite control/display
surface implementing the user journey.

## Architecture recorded first

- D-025: in-process asyncio workers; SQLite rows are the only queue truth.
- D-028: every UI-visible fact is persisted in `websocket_events` first, then
  broadcast; reconnect replays from a last-seen cursor. Pings/pongs are
  ephemeral and never consume sequence numbers.

## Files changed / added

- `backend/classroom_interpreter/runtime/` (new): `events.py`
  (`EventPublisher`: persist-then-fan-out, in-order delivery), `pipeline.py`
  (`SessionOrchestrator`: single active session, start/pause/resume/end,
  worker loops, background finalize/drain, recovery listing + resume,
  content-free progress metrics incl. `live_delay_ms`).
- `backend/classroom_interpreter/api/sessions.py` (new): `POST /api/sessions`,
  `pause`/`resume`/`end` (idempotent commands), `GET /api/sessions/{id}`
  (lifecycle + progress + committed history), `GET /api/recovery`,
  `POST /api/recovery/{id}/resume`, `DELETE /api/recovery/{id}` deliberately
  returns 501 until the Phase 7 purge agent exists (no improvised deletion).
- `backend/classroom_interpreter/api/websocket.py`: rewritten to replay
  persisted events after `cursor`, then stream live events with a
  per-connection monotonic filter (no duplicates, no loss on refresh).
- `backend/classroom_interpreter/api/health.py`: real content-free components
  (microphone from backend-owned listener, storage thresholds, asr/translation
  pipeline states, provider readiness/error category; missing key = red
  MIMO_KEY_MISSING, never a silent mock).
- `backend/classroom_interpreter/api/router.py`: includes sessions router.
- `backend/classroom_interpreter/main.py`: runtime wiring (MiMo providers only
  when a key is configured; otherwise Start Class refuses with
  PROVIDER_NOT_CONFIGURED), optional static serving of `frontend/dist`.
- `backend/classroom_interpreter/agents/transcription.py`,
  `translation.py`: optional `event_sink`; `subtitle.current` published after
  durable ASR commit (CREATED only), `subtitle.committed` after durable
  translation commit.
- `frontend/src/contracts.ts`, `frontend/src/App.tsx`, `styles.css`: full
  journey — course name + Start Class, LIVE/PAUSED/FINALIZING/RECOVERY badge +
  timer, current-English panel distinct from committed EN+ZH history (keyed by
  sequence), Pause/Resume/End controls, backlog/live-delay warning, WebSocket
  reconnect with cursor, health rows, recovery list with resume. No decorative
  features. `npm run typecheck` and `npm run build` pass clean; dist rebuilt.
- `tests/integration/test_live_api.py` (7 tests), `tests/helpers_phase3.py`
  (+build_chunk_writer), `tests/integration/test_app_shell.py` updated from
  the Phase 1 skeleton contracts to the Phase 5 contracts (real health
  components, static frontend root, ephemeral pong) — documented evolution,
  not weakened assertions.

## Commands and exact results

```
cd frontend && npm run typecheck   → exit 0
cd frontend && npm run build       → exit 0 (dist rebuilt)
PYTHONPATH=backend .../python -m pytest
→ 140 passed, 1 warning in 6.23s   (Phase 4: 133 passed)
```

## Key behaviors implemented and verified

- Backend owns the microphone: the listener is constructed and held by the
  orchestrator; the browser only calls REST/WS. Refresh/reconnect cannot stop
  capture (covered by the FakeListener lifecycle mirroring the real contract;
  the real-mic version of this claim stays a Phase 2 hardware gate).
- Ordered/no-duplicate replay: WS replay from `websocket_events` + per-session
  monotonic `event_sequence`; reconnect with the last-seen cursor replays
  nothing and misses nothing (test asserts strict ordering and empty re-replay).
- Committed history: served from SQLite (`translation_results` join) on
  session GET; UI dedupes by segment sequence; committed rows immutable.
- Idempotent controls: second pause returns PAUSED; second End Class returns
  the post-drain record without a second drain.
- Drain: End Class stops capture, cancels loops, runs a synchronous drain to
  DONE; any terminal failure → RECOVERY_REQUIRED (never a fake-complete);
  SUMMARIZING reached when all segments are DONE. Recovery listing and
  resume-drain verified. Deletion refused until Phase 7 machinery exists.
- Start Class without a configured key is refused (503 PROVIDER_NOT_CONFIGURED)
  — readiness is honest at the UI.

## Evidence classification

- E1/E2 local deterministic: all backend tests use injected fake providers and
  a backend-owned FakeListener writing the same durable chunk path as Phase 2
  (ChunkWriter); no microphone, no network, no real MiMo.
- Real browser E2E against a real mic: NOT TESTED (hardware gates open).
- `npm typecheck`/`build`: local deterministic.

## Known failures and unknowns (retained)

- The FakeListener mirrors the listener's session/DB contract but does not
  exercise CoreAudio; the real-mic journey remains gated on Phase 2 hardware
  evidence.
- WS pong is ephemeral (sequence 0) — UI liveness uses socket state, not pong
  sequences.
- The polling interval (3 s) plus WS keeps the UI eventually consistent;
  exact UX latency on target hardware is unmeasured.

## Privacy and purge impact

No key, audio, transcript, or translation content in logs; progress metrics
are counters/durations only. Session content remains in the runtime data
directory; nothing is deleted. Purge invariants untouched.

## Open gates

Phase 2 hardware ladder + permission/device/sleep matrix; real MiMo tests;
real-browser E2E with real capture; independent Reviewer/Validator/Oracle.

## Recommended next phase

Phase 6 — Final Summary (implemented under the overnight authorization).
