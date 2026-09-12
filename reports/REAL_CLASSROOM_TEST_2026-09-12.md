# Real Classroom Audio Test — 2026-09-12

The first run against **actual accented human speech**. Until now every
accuracy claim in this project rested on `say`-synthesized audio, which is far
cleaner than a lecture hall.

**Method:** the owner played a recorded class through the laptop speakers; the
laptop microphone picked it up and the audio went through the real pipeline to
the real MiMo endpoint. That path is itself realistic — distance, room noise
and speaker colouration are what a classroom actually sounds like.

**Result: it works, and it exposed three defects that synthesized audio never
produced.** All three are fixed (D-047).

## It transcribes real lectures

Genuine output from the recording (insurance/finance material):

| | |
|---|---|
| EN | *"Whether he's making money or not, right?"* |
| ZH | 不管他赚不赚钱，对吧？ |
| EN | *"Onshore, we're still talking about direct onshore business."* |
| ZH | 在岸方面，我们仍在讨论直接在岸业务。 |
| EN | *"But again, we just talk about sales, the most important thing."* |
| ZH | 不过话说回来，我们还是要谈谈销售，这可是重中之重。 |

Domain terms (在岸/direct onshore, 财产、机动车辆与责任) came through correctly.

Measured latency on real audio: interim caption **~3.0 s**, final English
**~6.6 s**, Chinese **~7.2 s**.

## Defect 1 — lecturers code-switch *(fixed)*

One segment's "English" transcript came back as:

> 呃，至少是一千一百亿。China is a huge market.

The lecturer was speaking Chinese. We then sent that to the translator, asking
it to translate Chinese **into Chinese** — a wasted request whose output was a
paraphrase *less faithful* than the words actually spoken.

A predominantly-Chinese transcript now passes through untranslated. The
threshold is a **clear majority** (>50 % of letters, ignoring digits and
punctuation), not "contains any Chinese": a mixed sentence still contains
English that needs translating, and passing it through would leave the reader
with no Chinese at all — the worse of the two failures.

Worth noting: `asr_options.language=en` does **not** force English output. The
endpoint transcribes what was said, which is correct. The bug was ours,
downstream.

## Defect 2 — the interim floor was publishing nonsense *(fixed)*

Transcribing the opening of one utterance at increasing lengths:

| Audio | On screen at | Output |
|---|---|---|
| 1.2 s | 1.82 s | `"8."` |
| 1.5 s | 2.03 s | `"Age."` |
| 2.0 s | 2.44 s | `"The agents coming."` |
| **2.5 s** | **3.08 s** | **`"The agent commission."`** |

The 1200 ms default I had chosen the day before was publishing confident
garbage. Raised to **2500 ms**: one second of latency for the whole class of
error. Single-word partials are suppressed outright.

After the fix, one segment's interim caption matched its final English exactly:
*"whether he's making money or not, right?"*

## Defect 3 — single-word partials *(fixed)*

`"8."` and `"Age."` were real outputs. A one-token interim reads as fact and is
not one. Minimum two words.

## Known limitation, deliberately NOT fixed

Short interim transcripts sometimes begin with a stray `think`:

```
interim : "think the property, motor, and all."
final   : "The property, motor, and liability."
```

The raw endpoint output confirms the scaffolding is real —
`think>\n<chinese> The agent commission.` — and D-042 already strips the forms
that can be matched safely (`think>`, `<chinese>`).

This variant cannot be. `think the property` is probably a remnant, but
`I think we should stop here` is a lecturer talking, and at the start of a
fragment the two are indistinguishable. Stripping on a guess would **silently
delete a speaker's words** — a permanent loss to fix a cosmetic one.

Scope is small: it affects only the 2.5-second interim caption, never the final
English (which came back clean every time), and the UI already renders interim
text in dimmed italics marked 正在识别.

## Also fixed this session

- **Glossary injection channel.** A `text` part beside the audio is rejected
  with 400; only a `system` message works. With the feature on as first
  written, *every* transcription would have failed.
- **Interim transcription architecture** (D-046): English on screen ~2.3 s
  after speech instead of ~6.7 s, with partials structurally unable to enter
  the durable record.
- **Auto-pause** after 30 s of silence (D-044).

## Still NOT verified

- **Microphone capture in a real room.** This test played a recording through
  speakers. A live class has overlapping voices, a moving speaker, and no clean
  source.
- **Sustained load / rate limits.** Longest real run was ~30 s. Interim
  transcription multiplies request volume and the retry path has still never
  faced a real 429.
- **Three-hour session**, permission/device/sleep-wake matrices, real-data
  purge, and independent quality judgement of the summary.
- **Cost.** Not measured.

## Environment note

This machine's `OppaVPN` hijacks `api.xiaomimimo.com` to 198.18.1.106 and
routes a domestic Xiaomi service abroad, which made MiMo unreachable. I worked
around it inside the test process only (DoH lookup + in-process resolution
override); the repository and production code are untouched. **The app will not
reach MiMo in normal use until `xiaomimimo.com` is set to DIRECT in OppaVPN.**

> **Correction (2026-09-12, later the same day).** The diagnosis in this
> section was **wrong**, and it is left here rather than quietly edited because
> the mistake is instructive.
>
> `198.18.x` addresses are not a hijack — they are how TUN-mode proxies
> normally work. `baidu.com`, `qq.com` and `taobao.com` all resolve to
> `198.18.x` on this machine and all work fine. I saw a reserved-range address
> and concluded "broken" without checking whether *any* domain resolved
> differently.
>
> The real cause was timing: the `leaf` proxy core was not running during those
> tests (it started at 15:05 that day). Routes still sent traffic into
> `utun233`, but nothing was listening at the other end — hence connect
> timeouts rather than refusals, and hence `curl --resolve` succeeding, since
> that path bypassed the tunnel entirely.
>
> Verified afterwards with the VPN on and no DNS override: three consecutive
> calls returned HTTP 200 in 0.88–0.99 s. **No VPN rule change is needed**, and
> the owner's existing `site:cn → Direct` rule was correct all along. The
> in-process DNS override used during testing was working around a transient
> outage, not a misconfiguration.


## Verification

Backend **252 passed**; every fix above carries mutation probes that each fail
exactly one named test.
