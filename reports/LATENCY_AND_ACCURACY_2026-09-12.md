# Latency and Accuracy Work — 2026-09-12

Driven by two requirements: **速度要快** and **翻译要准确（有口音也能准确识别）**.
All numbers below are from the live MiMo endpoint, not mocks.

## Result

| | Before | After |
|---|---|---|
| **English caption appears** | 6.67 s | **2.32 s** (−65 %) |
| Chinese caption appears | 7.47 s | 7.47 s (unchanged) |
| ASR word error on hard clips | 3.1 % | **1.7 %** (−45 %) |
| Recording left running on an empty room | forever | auto-pauses at 30 s |

Chinese is deliberately unchanged: translating half a sentence produces worse
Chinese, so it still waits for the segment to seal. English now arrives first
and the Chinese fills in beneath it.

## First: the measurement was wrong

Earlier I reported ASR at 3.6 s per segment. That was mostly **VPN detour, not
model time**. This machine's `OppaVPN` hijacks `api.xiaomimimo.com` to
198.18.1.106 (an RFC 2544 range used for TUN-mode fake IPs) and routes it
abroad — MiMo is a domestic Xiaomi service.

Resolved the real address over DoH (47.237.8.234, Alibaba Cloud Singapore) and
overrode resolution **inside the test process only**; the repo and production
code are untouched. Against the real endpoint:

| | Measured earlier | Direct |
|---|---|---|
| ASR, full segment | 3.6 s | **0.75 s** |
| ASR, 1.5 s clip | — | **0.56–0.80 s** |
| Translation | 2.3 s | **0.81 s** |

So the model was never the bottleneck. Everything below is built on the
corrected numbers.

**For the owner:** set `xiaomimimo.com` to DIRECT in OppaVPN, or the app will
not reach MiMo at all outside my test harness.

## The architecture change: interim transcription (D-046)

Previously nothing could appear until a segment sealed — ~6 s of speech plus
the ASR leg. A separate loop now transcribes the utterance **still being
spoken** every 2 s:

```
speech ──┬── 1.5s ──► ASR(0.8s) ──► "Now, consider a European."      [interim]
         ├── 3.0s ──► ASR(0.6s) ──► "...with strike price K."        [interim]
         └── seal  ──► ASR ──► translate ──► final EN + ZH           [durable]
```

**Partials are structurally incapable of becoming facts.** This was the
condition for adding them at all:

| Mechanism | Why |
|---|---|
| `publish_ephemeral()` | no database row, no sequence allocated |
| `subtitle.partial` kind | distinct from the committed `subtitle.current` |
| `transcribe_bytes()` | returns a bare string, not a `TranscriptResult` — interim text has no segment identity to commit |
| `event_sequence = -1` | can never advance a client's replay cursor |

That last one was a real bug I introduced and caught: my first version
published partials through the normal publisher, which persists to
`websocket_events`. That table **is** the replay log, so a reconnecting client
would have received a burst of stale interim text for sentences that finished
minutes earlier — and partials would have entered the durable record as facts.

`PERSIST FIRST, PROCESS SECOND` and English-as-source-of-truth are untouched:
the sealed segment is still the only thing the summary ever sees.

**Cost:** roughly one extra ASR call per interval per utterance. It is a dial
(`PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS`, 0 disables), not a default-on
certainty, because that is the user's money.

## Accuracy: the glossary (D-045)

Borrowed from Youdao's 添加术语库. Residual ASR error on hard clips was almost
entirely **domain-term spelling**, not misheard speech — so a term list is the
right lever.

**The injection channel had to be found by probing**, because my first
implementation was simply wrong:

| Channel | Result |
|---|---|
| `text` part beside the audio | **400 Param Incorrect** — every transcription would have failed |
| `asr_options.hotwords` | 200, silently ignored (output byte-identical) |
| `asr_options.prompt` | 200, silently ignored |
| **`system` message** | **200 and effective** |

Effective is measured, not assumed: the same audio returned `Itô's lemma`
without the glossary and `Ito's lemma` with it, matching the entry. Across
three hard clips, word error went **3.1 % → 1.7 %**.

A term may also pin an exact Chinese rendering, so terminology stays stable
across a lecture instead of drifting between synonyms.

The glossary is keyed by **course name**, so a weekly course keeps its
vocabulary. It is **user configuration, not classroom content** — it holds only
what the user typed — so it is absent from `CONTENT_TABLES` and survives purge.

> An earlier single-sample run appeared to show the glossary making things
> *worse* (3.1 % → 4.1 %). That was sampling noise; comparing four prompt
> shapes over the same clips put every glossary variant at 1.7 %.

## Auto-pause (D-044)

Recording stops after 30 s of continuous silence (0 disables). Measured against
the segmenter's **durable silence run** — silence in captured audio, not a wall
clock — so a stalled pipeline cannot fake it and a restart resumes the count.
Ignored while an utterance is open, because a pause inside a sentence is speech
rhythm, not an empty room. Settings reject a threshold at or below the segment
silence gap, which would pause the class on every sentence break.

## Not fixed, and why

**List-number pollution.** MiMo's ASR sometimes prefixes a transcript with
`1. ` / `2. `. It happens **without** a glossary too, so it is the model's own
behaviour. I did not strip it in the parser: doing so risks eating a lecturer
who really did say "One." — and unlike the `<chinese>` scaffolding (D-042),
this is not a fixed template token that can be matched safely.

**Chinese still waits for a full sentence.** Translating a fragment yields
worse Chinese. English-first already removes most of the felt delay.

## Still NOT verified

- **Real classroom audio.** All clips were machine-synthesized (`say`), which
  is cleaner than a lecture hall. **Real accented human speech is untested** —
  the requirement that motivated the glossary is the one thing I could not
  measure honestly.
- **Sustained load.** Longest real run was a handful of calls. Interim
  transcription multiplies request volume, and the retry path has still never
  faced a real 429.
- **Cost.** No pricing or billing data accessed.
- Real three-hour microphone, permission/device/sleep-wake matrices, real-data
  purge, Oracle quality judgement — unchanged.

## Verification

Backend **242 passed**; frontend typecheck and build exit 0. Every change above
carries mutation probes that each fail exactly one named test. Three of my own
probes initially proved nothing (two equivalent mutations, one asserting
nothing) and were redone; that is recorded in the commits rather than smoothed
over.
