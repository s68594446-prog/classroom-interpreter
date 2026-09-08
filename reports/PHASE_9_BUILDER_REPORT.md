# CLASSROOM INTERPRETER — PHASE 9 BUILDER REPORT

Date: 2026-09-08
Status: **IMPLEMENTED — E5 ACCELERATED PASS (deterministic). NOT ACCEPTED as
phase completion.** E6 (real wall-clock target-macOS microphone soak) and E4
(real MiMo short run) remain NOT TESTED — neither is substituted by this run.

## Scope

Accelerated three-hour-equivalent full-pipeline soak driven by the REAL runtime
agents (SegmentAgent → TranscriptionAgent → TranslationAgent → SummaryAgent →
CleanupAgent) over a synthetic 9,000 s (2.5 h) lecture at 48 kHz → 16 kHz,
with seeded fault injection, a mid-soak restart, backlog interleave, resource
sampling, and post-purge lifecycle assertions.

## Harness

- `scripts/soak_accelerated.py` (new, committed to the repo, re-runnable,
  seed=20260908): writes durable 20 s capture chunks through the Phase 2
  `ChunkWriter` durability path, interleaves consumer cycles to build backlog,
  injects per-call errors (2% 500, 1.5% 429+Retry-After 5 ms, 1% timeout for
  ASR; 1.5% 503, 1% timeout for translation) with bounded retry budgets
  (max_attempts=8, backoff 1–20 ms), rebuilds all agents at 40% (durable cursor
  reload), drains, summarizes, purges, and verifies.

## Command

```
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python \
  scripts/soak_accelerated.py \
  --root /private/tmp/classroom-interpreter-overnight/phase9-soak --seconds 9000
```
Exit code 0. Artifacts (content-free):
`/private/tmp/classroom-interpreter-overnight/phase9-soak/soak_report.json`,
`metrics.jsonl` (46 samples), post-purge DB, one retained summary file.

## Results

| Check | Result |
|---|---|
| Lecture duration | 9,000 s (2.5 h equivalent) |
| Recovery chunks written | 450 (20 s each), all sealed + checksummed |
| Segments | **1,800 of 1,800 expected**; sequences contiguous 0..1799; all DONE |
| Duplicate ASR / translation commits | 0 / 0 |
| Translation commit order | strictly 0..1799 (ordered cursor) |
| Provider faults injected & recovered | 27×500 + 26×429 + 21×timeout (ASR); 25×503 + 24×timeout (translation) — all drained, zero terminal failures |
| Restart injection | 1 (agent rebuild at chunk 180/450; cursor reload, no duplication) |
| Max consumer backlog steps | bounded; `pending` at every sample = 0 (interleaved pacing) |
| Wall-clock elapsed | 54.7 s (accelerated; not a wall-clock soak claim) |
| RSS initial / peak / final | 45.45 / 46.28 / 40.73 MiB — bounded, no growth trend |
| Open FDs baseline / final | 7 / 7 (no handle leak) |
| Post-purge | outcome PURGED; session dir absent; session row removed; `purged_session_metadata` hash matches retained summary byte-for-byte; one summary artifact |
| Overall | **pass: true** |

## Evidence classification

- **E5 accelerated three-hour-equivalent workload: PASS** on current source
  (hashes in `FINAL_BUILDER_HANDOFF.md`).
- **E6 real wall-clock three-hour macOS microphone soak: NOT TESTED** —
  requires the user-authorized hardware session.
- **E4 real MiMo run: NOT TESTED** — no authorized key.

## Known failures and unknowns (retained)

- The accelerated run does not exercise CoreAudio callback stability, macOS
  sleep/permission behavior, or real-provider latency/quality — E6/E4 gates
  stand.
- Wall-clock timers (sleep/wake gaps) are untested by construction here.
- Fault injection is seeded, not adversarial; real provider misbehavior may
  include patterns not covered (e.g., slow streams, partial bodies).

## Privacy and purge impact

Synthetic tone audio only; report/metrics are content-free. The soak's own
purge completed: only the summary + allowlisted metadata + content-free
journal remain in its isolated root.

## Open gates

E6 soak, E4 real MiMo, Phase 2 hardware ladder, independent
Reviewer/Validator/Oracle.

## Recommended next phase

Phase 10 — Final audit candidate.
