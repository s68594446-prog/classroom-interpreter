# Gemini Evaluation — 2026-09-11

Measured against a real Gemini key, alongside the live MiMo numbers from the
same day. Test audio was machine-synthesized (`say`), never real classroom
content — deliberate, because the free tier's data terms make real lecture
audio unsafe to send (see *The free tier is not free for this project*).

**Verdict: Gemini is meaningfully faster and would halve our call count, but the
free tier cannot carry a 3-hour class, and its data terms disqualify it for real
classroom audio. Worth paying for; not worth switching to for free.**

## Latency

| Path | Latency | Calls per segment |
|---|---|---|
| MiMo ASR + MiMo translation (current) | 3.6 s + 2.3 s = **5.9 s** | 2 |
| Gemini translation alone | **p50 0.93 s**, p90 1.06 s | 1 (+ASR elsewhere) |
| **Gemini single call: ASR + translation together** | **4.82 s** | **1** |

Gemini's translation is **~2.5× faster** than MiMo's (0.93 s vs 2.29 s). The
combined call is faster than our two-step chain *and* halves the request count,
because the audio is transcribed and translated in one pass.

Model selection matters more than expected — newer is not faster:

| Model | p50 | max | notes |
|---|---|---|---|
| `gemini-2.5-flash` | **1.78 s** | 15.51 s | best of the four |
| `gemini-3.5-flash` | 6.81 s | 18.49 s | |
| `gemini-3.8-flash` | 15.64 s | 80.66 s | 2/5 failed |
| `gemini-2.5-flash-lite` | — | — | 404 on v1beta |

Auth: `x-goog-api-key` header and `?key=` both work; `Bearer` is rejected (it
expects an OAuth token).

## Quality

Transcription and translation were both accurate on the finance material,
including domain terms:

> EN: *Today we will derive the Black-Scholes equation from first principles.
> Value at risk measures the worst expected loss at a given confidence level.*
> ZH: 今天我们将从第一性原理推导出布莱克-斯科尔斯方程。风险价值衡量在给定置信水平下的最坏预期损失。

Unlike MiMo's ASR, the output carried **no chat-template scaffolding** — no
`<chinese>` or `think>` remnants (cf. D-042). Structured JSON output via
`responseMimeType: application/json` worked first try.

## The free tier cannot carry a class

Free tier is **15 RPM / 1500 RPD** for Flash. A 3-hour class at the current 6 s
segmentation is ~1800 segments:

| Configuration | Calls/class | vs 1500 RPD | vs 15 RPM |
|---|---|---|---|
| Gemini for ASR + translation separately | 3600 | ❌ 2.4× over | ❌ 20 RPM |
| **Gemini single combined call** | **1800** | ❌ 1.2× over | ✅ 10 RPM |
| Single call, back to 12 s segments | 900 | ✅ fits | ✅ 5 RPM |

So the combined call fixes the per-minute problem but still exceeds the **daily**
cap by ~20 % for one 3-hour class. Fitting inside the free tier means returning
to 12 s segmentation — which gives back the latency improvement from D-043.

This was observed directly, not extrapolated: **six consecutive calls succeeded,
then six straight 429s**, recovering after a short wait. The per-minute ceiling
is real and easy to hit — my own evaluation exhausted it.

## The free tier is not free for this project

Google's API terms state that for unpaid services Google "uses the content you
submit to the Services and any generated responses to provide, improve, and
develop Google products and services", and that **"human reviewers may read,
annotate, and process your API input and output."** The paid tier states the
opposite: "Google doesn't use your prompts … or responses to improve our
products."

We record professors and classmates. The entire data-lifecycle design —
`NO VALID SUMMARY = NO PURGE`, `secure_delete`, D-039's zeroed free pages —
exists to stop classroom content from spreading. Sending that audio to a tier
that feeds human review and model training would defeat all of it at the first
hop. The cost saving is paid for in other people's privacy.

**This is a free-vs-paid distinction, not a Google-vs-MiMo one.** Paid Gemini
carries no such term.

## The architectural catch with the combined call

The single call is attractive but it **weakens a property we deliberately
built**. Our contract is that the English ASR text is the factual source and the
Chinese is derived from it, bound by `english_text_sha256` — that hash is what
proves the translation corresponds to the transcript the summary was built from.

When one call emits both, the model may translate straight from audio rather
than from the English it reports. The two outputs become siblings, not
derivation and source, and the hash binding degrades from a real check into a
formality. Reviewers lose the ability to detect a Chinese line that says
something the English never said.

Keeping them separate costs a round trip and buys verifiability. That is a
product decision, not a technical one, and it belongs to the owner.

## Recommendation

1. **Do not adopt the free tier.** The data terms are disqualifying for real
   classroom audio, and the daily cap does not fit a 3-hour class anyway.
2. **Paid `gemini-2.5-flash` is worth considering for translation only** —
   ~2.5× faster than MiMo (0.93 s vs 2.29 s), with clean output and no
   scaffolding to strip. Keeping MiMo's ASR preserves the English-as-source
   contract.
3. **Treat the combined call as a separate decision.** It is the bigger win
   (halved calls, 4.82 s end-to-end) but costs the source-hash guarantee.
4. **Gemini Live API was not tested.** It is the option that could take
   first-caption latency below a second by streaming audio continuously instead
   of per-segment, which would change the product rather than tune it. That
   needs a paid key and a different pipeline shape.

## Not verified

- **Gemini Live API** — untested; the interesting case remains unmeasured.
- **Sustained load / real rate-limit behaviour under a full class.** The longest
  run here was a dozen calls.
- **Real classroom audio.** Synthesized speech only, and deliberately so.
- **Cost at paid tier.** No pricing was accessed.
- **Quality at scale.** A handful of finance sentences is not an evaluation.

## Unrelated finding: MiMo was unreachable during this run

Every MiMo call failed with `ConnectTimeout`. `api.xiaomimimo.com` resolves to
**198.18.1.106**, inside the RFC 2544 benchmarking range (198.18.0.0/15) that
VPN and proxy tools commonly use for TUN-mode fake IPs. General connectivity was
fine (google.com returned 200), so this is **local proxy/VPN routing on this
machine, not a MiMo outage**. The MiMo numbers quoted above are from earlier the
same day, when it was reachable.
