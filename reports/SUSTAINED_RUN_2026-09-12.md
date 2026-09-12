# Sustained Live Run — 2026-09-12

A continuous run of the real pipeline against a real microphone, with **exact
ground truth recorded for every sentence played**. That last part is what makes
this different from previous sessions: accuracy and latency stop being
impressions and become numbers that can be wrong.

**Planned 60 minutes; stopped by the owner at 29.** Everything below is from the
29 minutes actually captured — 247 sentences played, 67 segments committed.

## Method

Five accented English voices (en_GB, en_IN, en_ZA, en_IE, en_AU) reading an
insurance/finance lecture through the laptop speakers, picked up by the laptop
microphone, through the production orchestrator to the real MiMo endpoint. Every
utterance was logged with start and end timestamps, so transcripts could be
aligned to what was actually said rather than eyeballed.

The owner's own class recording could not be used: the Chrome extension was not
connected this session and computer-use grants browsers read-only access, so
the recording tab could not be driven. **Synthesized accented speech through a
real acoustic path is not the same as a real lecturer** — see *Not verified*.

## What the run measured

| | Measured |
|---|---|
| English caption behind the speaker | **p50 12.5s**, p90 19.1s, max 21.0s |
| Chinese caption behind the speaker | p50 13.7s, p90 20.8s |
| ASR call | 1.61s avg, 2.61s max |
| Translation call | 1.65s avg, 3.53s max |
| Provider retries / 429s / errors | **0** in 134 calls |
| Interim captions delivered | **0** |
| English captions out of order | **8 of 66** |
| Word error rate (microphone, aligned) | **14.0%** |
| Peak RSS / descriptors | 74.8 MB / 225 |

## Three earlier claims this run contradicts

**"English caption at 2.32s."** Measured end to end it is **12.5s**. The earlier
figure was taken off the publisher queue, which sits above both layers that were
actually broken — see defects 1 and 2. The number was real; nothing reached a
browser.

**"ASR word error 1.7%."** That was clean audio fed straight into the provider.
Through a microphone it is **14.0%**. Most of the gap is acoustic, not model:
the best accent scored 0.0% and the worst 40.0% on the same material.

**D-043 "segmentation 12s → 6s".** `segment_preferred_seconds` was never passed
to the segmenter. The change altered a number nothing read.

## Defects found and fixed

**1. Interim captions never reached the browser.** The WebSocket handler
forwards an event only when `event_sequence > last`; `last` starts at the
client's cursor, -1 by default, and an ephemeral event's sequence is exactly -1.
`-1 > -1` is false. Every partial was discarded one layer below the code that
produced it. Existing tests subscribed to the publisher directly and passed.

**2. Capture chunks were hard-coded to 20 seconds, and that is what caption
latency was made of.** The segmenter consumes only committed chunks, so a
speaker waited on average half a chunk before processing could begin. The writer
validated `10 <= seconds <= 30`, so no configuration could fix it. Sealing costs
~1.5ms regardless of length — at 1s chunks the whole durability path is 0.15% of
realtime. Now `capture_chunk_seconds`, default 2.

**3. The VAD did not survive contact with a microphone.** Segment duration p50
was **29.0s against a 30.0s cap**, with 16 of 36 segments closing on the ceiling
rather than on a pause. Three causes: a single 20ms frame above threshold reset
the silence run to zero; `segment_preferred_seconds` was dead; and the threshold
was a fixed absolute RMS, which cannot hold across rooms and input gain.
Replayed through the real 10ms pipeline on the audio this run captured:

| | p50 | p90 | at the 30s cap |
|---|---|---|---|
| before | 27.8s | 30.0s | 16/35 |
| after | **6.5s** | **11.0s** | **0/96** |

**4. English captions could appear out of order.** `subtitle.committed` passes
through the durable ordered cursor; `subtitle.current` was published straight
from whichever of two ASR workers finished first. A reorder gate restores order
and gives up on a gap after 3s, because a frozen display is worse than one
inversion.

**5. The database holding every transcript was world-readable.** `runtime.sqlite3`
at 0644 inside a 0755 directory, recorded segment audio at 0644 — while the
content-free health database was correctly 0600. The protection was applied to
the one store that did not need it.

**6. A file descriptor leaked per health flush.** `with sqlite3.connect(...)`
commits but does not close. 102 open handles to one file during the run.
`ClosingConnection` exists for exactly this and was never applied there.

Separately: **`websockets` was in `pyproject.toml` but absent from the installed
venv**, so uvicorn's `/ws` endpoint 404-ed and live captions did not work at all.
There is now a test that performs a real handshake rather than trusting
`TestClient`, which speaks WebSocket in-process and never goes through uvicorn.

## Open, not fixed

- **Ending the session produced no summary.** It went to `RECOVERY_REQUIRED`
  with one segment stuck in `CAPTURED`, no summary job created, and
  `last_error_category` null — the failure is currently unattributable. Content
  was retained, so `NO VALID SUMMARY = NO PURGE` held. Not diagnosed.
- **There is almost no operational logging.** `log_metadata` has two call sites
  in the whole backend; per-segment latency and provider outcomes exist only in
  `processing_attempts`. The silent finalization failure above is what that
  costs.
- **`/health` is not under `/api`** while every other route is.
- **The repo lives inside iCloud Drive.** 2053 files in `.venv` were evicted;
  each blocking read costs 1.75s, which stalled backend startup for minutes.
  `brctl download` made no progress; 48-way parallel reads cleared it in 2m44s.

## Not verified

- **The fixes are not yet confirmed on a live run.** Defects 2, 3 and 4 are
  verified by unit tests and by replaying the captured audio through the real
  frame pipeline — **not** by a fresh end-to-end recording. The latency and
  segment-duration figures above are simulation on real audio, not a second
  measurement.
- **A real lecturer.** Synthesized accented speech through a real acoustic path
  is closer than clean PCM and still not a person in a room.
- **Sustained load past 29 minutes**, rate limits (no 429 was ever provoked),
  three-hour sessions, sleep/wake, real-data purge, and cost.

## Verification

284 passed. Every fix carries a mutation probe that fails exactly one named
test; the VAD, permission, ordering and descriptor fixes were each re-verified
by reverting the production change and confirming the suite goes red.
