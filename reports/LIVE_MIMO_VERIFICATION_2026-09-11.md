# Live MiMo Provider Verification — 2026-09-11

First real MiMo API calls in the project's history. Until today every MiMo gate
was marked NOT TESTED because no key existed; all provider evidence came from
deterministic mocks.

**Two real defects were found that mocks could not have caught, and both are
fixed.** Model quality was not independently judged — see *Not verified*.

## Setup

| | |
|---|---|
| Key location | `.env`, mode `600`, covered by `.gitignore` (`.env`, `.env.*`) |
| Key in tracked files | none — scanned all `*.py` / `*.md` / `*.json` / `*.ts` |
| Base URL | `https://api.xiaomimimo.com/v1` |
| Models | `mimo-v2.5-asr`, `mimo-v2.5`, `mimo-v2.5-pro` |
| Test audio | macOS `say` → 16 kHz mono PCM16 WAV (no real classroom audio used) |

Auth: **both** `api-key` and `Authorization: Bearer` headers return 200. The
adapter's existing `api-key` choice is correct; no change needed.

## Finding 1 — ASR transcripts carry chat-template scaffolding *(fixed, D-042)*

The real `mimo-v2.5-asr` endpoint wraps the transcript, **inconsistently across
calls with identical request shape**:

| Audio | Raw `content` returned |
|---|---|
| "Good morning, everyone." | `'<chinese> Good morning, everyone.'` |
| Black-Scholes sentence | `'think>\n<chinese> Today we will derive...'` |
| Portfolio beta sentence | `' The portfolio beta is 1.35, ...'` (no wrapper) |

Note the `think>` — an unbalanced `</think>` remnant, not a well-formed tag.

**It cannot be disabled.** `thinking.type=disabled` and
`chat_template_kwargs.enable_thinking=false` both still produce it for the ASR
model, even though both work for the text models:

```
baseline              3.56s  scaffolded
chat_template_kwargs  2.05s  scaffolded
thinking disabled     1.79s  scaffolded
```

**Why this mattered.** The English ASR text is the project's factual source for
the summary. Untouched, these tokens would have entered every transcript shown
to the user, every translation input, and the final summary — while every
existing check still passed, because the source-hash binding only proves the
translation saw *the same* text, not *correct* text.

**Fix.** `strip_asr_scaffolding` in `parse_asr_response`. Prefix-anchored: it
removes a known tag only when the remaining text starts with it, and never
edits the interior. A lecturer saying "Now think about what the theorem means"
or dictating `<english>` keeps every word — pinned by its own test.

## Finding 2 — Realtime translation was paying for reasoning it doesn't need *(fixed, D-041)*

`mimo-v2.5` is a reasoning model that by default spends most of its budget in
`reasoning_content`. Measured over five lecture sentences:

| | p50 latency | max | completion tokens |
|---|---|---|---|
| Default (thinking on) | 5.40 s | 8.31 s | ~152 |
| `chat_template_kwargs.enable_thinking=false` | **2.29 s** | **2.54 s** | **~14** |

**−58 % latency, −91 % tokens, no quality loss on any sample.** Sentence
translation is not a task that needs deliberation, and a caption arriving five
seconds late is not realtime.

Only that spelling works. Two plausible alternatives are silently ignored:

| Parameter | Effect |
|---|---|
| `chat_template_kwargs.enable_thinking=false` | ✅ works |
| `thinking.type=disabled` | ✅ works |
| `enable_thinking=false` (top level) | ❌ ignored |
| `reasoning_effort="low"` | ❌ ignored |

Summary generation **deliberately keeps reasoning** — offline, quality-sensitive,
nobody waiting. A test pins that asymmetry so it is not "unified" later.

## End-to-end run through production adapters

`MimoASRProvider` → `MimoTranslationProvider`, real audio, real calls:

| # | ASR | Translate | Source hash | English (after strip) → Chinese |
|---|---|---|---|---|
| 0 | 3.35 s | 2.51 s | OK | "Good morning, everyone." → 大家早上好。 |
| 1 | 3.81 s | 3.43 s | OK | Black-Scholes / VaR sentence → 今天我们将从基本原理推导布莱克-斯科尔斯方程。风险价值衡量的是在给定置信水平下的最大预期损失。 |
| 2 | 3.40 s | 2.72 s | OK | beta 1.35 / 22 % Q3 2024 → 该投资组合的贝塔系数为1.35，年化波动率在2024年第三季度达到了22%。 |
| 3 | 3.79 s | 3.66 s | OK | European call / risk-neutral → 在风险中性估值下，考虑一个执行价格为K、到期日为T的欧式看涨期权。其价格等于预期收益的折现值。 |

**Mean end-to-end 6.67 s per segment** (ASR ≈ 3.6 s + translation ≈ 3.1 s).
Domain terms rendered correctly throughout. All four source-hash bindings
verified.

Summary path: `summarize_chunk` 18.47 s → `synthesize` 32.99 s → 1410 chars,
**passes `validate_summary_document`** (all ten sections, correct headers). It
correctly reconstructed `C = e^{-rT}·E_Q[(S_T − K)^+]` from spoken prose.

## Latency reality check

At 6.67 s per segment the Chinese caption trails the English by roughly that
much. Workers run in parallel so **throughput** is not the constraint —
**first-caption delay** is. Users should expect Chinese to arrive several
seconds behind speech; this is inherent to a cloud round trip and is not a bug.
AskSia advertises `<0.1 s`, which is a fundamentally different (streaming)
architecture — that claim is also unverified by us.

## Also observed

**Silence produces a hallucinated character.** 3 s of digital silence returned
`'？'` rather than empty. VAD should prevent silent segments from reaching ASR,
so this is not currently reachable — but it means ASR output must never be
trusted as a silence detector. Recorded, not fixed.

**One recognition error.** "Now, consider a European call option…" came back as
"1. Consider a European call option…". Provider accuracy, not an integration
defect; the translation faithfully carried the error through, which is correct
behaviour for a pipeline whose Chinese is derived from the English.

## Still NOT verified

- **Rate limits and quota.** Docs say 100 RPM; I did not run a burst test, so
  the retry/backoff path has never faced a real 429.
- **Cost.** No pricing or billing data was accessed.
- **Sustained load.** Longest real session here was four segments; a 3-hour
  class is ~900 segments.
- **Model quality at scale** — four synthesized sentences is not an evaluation.
  No independent judgement of factual accuracy or translation quality.
- **Real classroom audio.** All test audio was machine-synthesized speech, which
  is cleaner than a lecture hall. Real-microphone accuracy remains unknown.
- **Real three-hour microphone capture**, permission/device/sleep-wake matrices,
  and real-data purge — unchanged from the takeover report.

## Verification state

Backend **216 passed** (9 new), frontend typecheck and build exit 0. Three
mutation probes each fail exactly one named test:

| Mutation | Test that fails |
|---|---|
| `parse_asr_response` skips stripping | `test_asr_parser_strips_real_chat_template_scaffolding` |
| prefix match → substring replace | `test_asr_parser_never_edits_the_lecturer_s_own_words` |
| drop `**NO_THINKING`  | `test_translation_payload_disables_provider_side_reasoning` |

Regression fixtures are raw strings recorded from the live API, not what the
documentation describes.
