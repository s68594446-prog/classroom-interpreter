from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from classroom_interpreter.agents.context import ContextAgent, estimate_tokens
from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig
from classroom_interpreter.agents.transcription import TranscriptionAgent
from classroom_interpreter.agents.translation import TranslationAgent
from classroom_interpreter.domain.models import (
    ContextWindow,
    ProviderError,
    ProviderErrorCategory,
    TranslationRequest,
    TranslationResult,
    utc_now,
)
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mocks import MockTranslationProvider
from classroom_interpreter.providers.mimo import (
    TRANSLATION_PROMPT_VERSION,
    MimoTranslationProvider,
    render_translation_user_content,
    validate_translation_output,
)

import helpers_phase3 as h


# ----------------------------------------------------------------- fixtures


class FakeASR:
    """Deterministic ASR double that seals the pipeline without network."""

    name = "fake-asr"
    model = "fake-asr-v1"

    async def transcribe(self, segment, *, language: str = "en"):
        from classroom_interpreter.domain.models import TranscriptResult

        return TranscriptResult(
            segment_id=segment.segment_id,
            english_text=f"Segment {segment.sequence}: the probability is 0.05.",
            provider=self.name,
            model=self.model,
            latency_ms=1,
            audio_seconds=segment.duration_ms / 1000,
            created_at=utc_now(),
        )


def build_pipeline(database, tmp_path, session_id, *, utterances=4, provider=None):
    blocks = h.envelope_blocks(
        [("silence", 0.5), ("speech", 2.5), ("silence", 1.0)] * utterances
    )
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    segmenter = SegmentAgent(
        database,
        tmp_path / "sessions" / session_id,
        session_id,
        SegmenterConfig(),
    )
    segmenter.process_available_chunks()
    segmenter.seal_tail()
    asr = TranscriptionAgent(database, FakeASR(), worker_id="asr-w", jitter_fraction=0.0)
    while asyncio.run(asr.step()):
        pass
    translation = TranslationAgent(
        database,
        provider or MockTranslationProvider(),
        worker_id="tr-w",
        jitter_fraction=0.0,
    )
    return translation


def segment_rows(database, session_id):
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM segments WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def make_session_with_done_asr(database, tmp_path, utterances=4, *, drain=True):
    session_id = h.make_session(database)
    translation = build_pipeline(
        database, tmp_path, session_id, utterances=utterances
    )
    if drain:
        run_steps(translation)
    return session_id, translation


def run_steps(agent, max_steps=200):
    steps = 0
    while asyncio.run(agent.step()):
        steps += 1
        if steps >= max_steps:
            raise AssertionError("worker did not converge")
    return steps


# ------------------------------------------------------------ context agent


def test_estimate_tokens_is_deterministic_and_cjk_aware():
    assert estimate_tokens("") == 0
    assert estimate_tokens("风险") == 2
    assert estimate_tokens("risk") == 1
    assert estimate_tokens("risk管理") == 2 + 1


def test_context_reads_only_committed_predecessors(database, tmp_path):
    session_id, _ = make_session_with_done_asr(database, tmp_path, utterances=4)
    context = ContextAgent(database)
    window = context.build(session_id, 2)
    assert [s.sequence for s in window.previous_segments] == [0, 1]
    assert window.current_english_text.startswith("Segment 2")
    assert window.estimated_tokens > 0
    # Latest segment in the session has full context.
    latest = context.build(session_id, 3)
    assert [s.sequence for s in latest.previous_segments] == [0, 1, 2]


def test_context_bounded_by_count(database, tmp_path):
    session_id, _ = make_session_with_done_asr(database, tmp_path, utterances=6)
    context = ContextAgent(database, max_segments=2)
    window = context.build(session_id, 5)
    assert [s.sequence for s in window.previous_segments] == [3, 4]


def test_context_bounded_by_time(database, tmp_path):
    session_id, _ = make_session_with_done_asr(database, tmp_path, utterances=6)
    context = ContextAgent(database, max_segments=10, max_seconds=9)
    window = context.build(session_id, 5)
    sequences = [s.sequence for s in window.previous_segments]
    assert len(sequences) >= 1 and len(sequences) < 5
    assert window.window_end_ms - window.window_start_ms <= 9_000 + 5_000


def test_context_bounded_by_tokens(database, tmp_path):
    session_id, _ = make_session_with_done_asr(database, tmp_path, utterances=6)
    context = ContextAgent(database, max_segments=10, max_tokens=30)
    window = context.build(session_id, 5)
    assert window.estimated_tokens <= 30
    assert len(window.previous_segments) < 5


# ------------------------------------------------- output validation/prompt


def test_translation_output_rejects_empty():
    with pytest.raises(ProviderFailure):
        validate_translation_output("   ", "source")


@pytest.mark.parametrize(
    "text",
    [
        "Translation: 风险是 0.05",
        "translation: 风险是 0.05",
        "翻译：风险是 0.05",
        "[CONTEXT] echo",
        "Answer: The answer is 0.05.",
    ],
)
def test_translation_output_rejects_prompt_wrappers(text):
    with pytest.raises(ProviderFailure):
        validate_translation_output(text, "source")


def test_translation_output_accepts_plain_chinese():
    assert validate_translation_output("  概率是 0.05。 ", "source") == "概率是 0.05。"


def test_translation_prompt_contract_structure():
    from classroom_interpreter.domain.models import ContextSegment, ContextWindow
    from classroom_interpreter.providers.mimo import TRANSLATION_SYSTEM_PROMPT

    window = ContextWindow(
        session_id="s",
        current_segment_id="seg",
        previous_segments=(
            ContextSegment(sequence=0, english_text="prior one", chinese_text="前一段"),
        ),
        window_start_ms=0,
        window_end_ms=4_000,
        estimated_tokens=10,
        current_english_text="The VaR at 1.96 sigma is 5%.",
    )
    request = TranslationRequest(
        segment_id="seg", session_id="s", sequence=1,
        current_english_text="The VaR at 1.96 sigma is 5%.", context=window,
    )
    content = render_translation_user_content(request)
    assert "[CONTEXT]" in content and "[CURRENT SEGMENT]" in content
    assert content.index("[CONTEXT]") < content.index("[CURRENT SEGMENT]")
    assert "prior one" in content and "前一段" in content
    assert content.strip().endswith("The VaR at 1.96 sigma is 5%.")
    assert TRANSLATION_PROMPT_VERSION == 1
    # The instruction itself must require terminology/number preservation.
    for needle in (
        "never summarize",
        "Do not alter numbers, formulae, symbols, abbreviations, conditions, or proper names",
        "translate only the CURRENT segment",
        "Output only the translation text",
    ):
        assert needle in TRANSLATION_SYSTEM_PROMPT


def test_mimo_translation_provider_payload_and_result(tmp_path):
    from classroom_interpreter.providers.mimo import ProviderHttpResponse

    captured = {}

    async def post(url, headers, payload):
        captured.update(url=url, headers=headers, payload=payload)
        body = '{"id":"r1","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"概率是 5%。"}}]}'
        return ProviderHttpResponse(
            status=200,
            headers={},
            body=body.encode("utf-8"),
        )

    provider = MimoTranslationProvider(
        api_key="k", base_url="https://x/v1", model="mimo-v2.5", post=post
    )
    window = ContextWindow(
        session_id="s", current_segment_id="seg",
        previous_segments=(), window_start_ms=0, window_end_ms=0,
        estimated_tokens=0, current_english_text="The probability is 5%.",
    )
    request = TranslationRequest(
        segment_id="seg", session_id="s", sequence=0,
        current_english_text="The probability is 5%.", context=window,
    )
    result = asyncio.run(provider.translate(request))
    assert result.chinese_text == "概率是 5%。"
    assert result.english_text_sha256 == hashlib.sha256(
        "The probability is 5%.".encode("utf-8")
    ).hexdigest()
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["api-key"] == "k"
    assert captured["payload"]["model"] == "mimo-v2.5"
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert "[CURRENT SEGMENT]" in captured["payload"]["messages"][1]["content"]


# ------------------------------------------------------- ordered commit flow


def test_translations_commit_in_strict_sequence_order(database, tmp_path):
    session_id, translation = make_session_with_done_asr(database, tmp_path)
    rows = segment_rows(database, session_id)
    assert all(r["state"] == "DONE" for r in rows)
    # Sequences are contiguous and the durable cursor advanced through all of
    # them in order; commit_translation_result rejects out-of-order commits.
    assert [r["sequence"] for r in rows] == list(range(len(rows)))
    with database.connect() as connection:
        cursor = connection.execute(
            "SELECT next_translation_sequence FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()["next_translation_sequence"]
        results = connection.execute(
            "SELECT segment_sequence, chinese_text, english_text_sha256 FROM translation_results WHERE session_id = ? ORDER BY segment_sequence",
            (session_id,),
        ).fetchall()
    assert cursor == 4
    assert [r["segment_sequence"] for r in results] == [0, 1, 2, 3]
    for r in results:
        expected_source = (
            f"Segment {r['segment_sequence']}: the probability is 0.05."
        )
        assert r["english_text_sha256"] == hashlib.sha256(
            expected_source.encode("utf-8")
        ).hexdigest()


def test_duplicate_translation_commit_is_idempotent(database, tmp_path):
    from classroom_interpreter.storage.repository import CommitOutcome

    session_id, translation = make_session_with_done_asr(database, tmp_path, utterances=1)
    run_steps(translation)
    repository = translation._repository
    result = repository.get_segment(session_id, 0)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT chinese_text, english_text_sha256 FROM translation_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    from classroom_interpreter.domain.models import TranslationResult as TR

    replay = TR(
        segment_id=result.id,
        chinese_text=row["chinese_text"],
        english_text_sha256=row["english_text_sha256"],
        provider="mock",
        model="mock-translation-v1",
        latency_ms=1,
        created_at=utc_now(),
    )
    outcome = repository.commit_translation_result(session_id, 0, replay)
    assert outcome is CommitOutcome.ALREADY_COMMITTED


def test_failed_translation_blocks_head_of_line_but_later_asr_proceeds(database, tmp_path):
    """A terminal translation failure blocks ordered commits for that session;
    no later segment may commit before it (F-011 style fail-closed)."""
    calls = {"n": 0}

    class FailFirst(MockTranslationProvider):
        async def translate(self, request):
            if request.sequence == 1:
                calls["n"] += 1
                raise ProviderFailure(
                    ProviderError(category=ProviderErrorCategory.AUTH,
                                  retryable=False, http_status=401,
                                  safe_message="provider rejected credentials")
                )
            return await super().translate(request)

    session_id, translation = make_session_with_done_asr(
        database, tmp_path, drain=False
    )
    translation.provider = FailFirst()
    run_steps(translation)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "DONE"
    assert rows[1]["state"] == "TRANSLATION_FAILED"
    assert rows[2]["state"] == "ASR_DONE"  # never promoted past the blockage
    assert rows[3]["state"] == "ASR_DONE"
    with database.connect() as connection:
        cursor = connection.execute(
            "SELECT next_translation_sequence, last_error_category FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    assert cursor["next_translation_sequence"] == 1
    assert cursor["last_error_category"] == "AUTH"


def test_translation_retry_matrix_then_success(database, tmp_path):
    attempts = {"n": 0}

    class Flaky(MockTranslationProvider):
        async def translate(self, request):
            if request.sequence == 0 and attempts["n"] < 2:
                attempts["n"] += 1
                raise ProviderFailure(
                    ProviderError(category=ProviderErrorCategory.SERVER,
                                  retryable=True, http_status=500,
                                  safe_message="provider server error")
                )
            return await super().translate(request)

    session_id = h.make_session(database)
    translation = build_pipeline(database, tmp_path, session_id, utterances=1)
    translation.provider = Flaky()
    translation.backoff_base_seconds = 0.0
    run_steps(translation)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "DONE"
    assert attempts["n"] == 2


def test_translation_rate_limit_honors_retry_after(database, tmp_path):
    attempts = {"n": 0}

    class Throttled(MockTranslationProvider):
        async def translate(self, request):
            if request.sequence == 0 and attempts["n"] < 1:
                attempts["n"] += 1
                raise ProviderFailure(
                    ProviderError(category=ProviderErrorCategory.RATE_LIMIT,
                                  retryable=True, http_status=429,
                                  retry_after_ms=50,
                                  safe_message="provider rate limit reached")
                )
            return await super().translate(request)

    session_id = h.make_session(database)
    translation = build_pipeline(database, tmp_path, session_id, utterances=1)
    translation.provider = Throttled()
    translation.backoff_base_seconds = 30.0
    start = time.monotonic()
    run_steps(translation)
    time.sleep(0.08)
    run_steps(translation)
    elapsed = time.monotonic() - start
    assert elapsed < 5
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "DONE"


def test_translation_budget_exhaustion_fails_segment(database, tmp_path):
    class AlwaysTimeout(MockTranslationProvider):
        async def translate(self, request):
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.TIMEOUT,
                              retryable=True, safe_message="translation request timed out")
            )

    session_id = h.make_session(database)
    translation = build_pipeline(database, tmp_path, session_id, utterances=1)
    translation.provider = AlwaysTimeout()
    translation.backoff_base_seconds = 0.0
    translation.max_attempts = 3
    run_steps(translation)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "TRANSLATION_FAILED"
    assert rows[0]["translation_retry_count"] == 3
    with database.connect() as connection:
        session = connection.execute(
            "SELECT last_error_category FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    assert session["last_error_category"] == "TIMEOUT"


def test_translation_lease_expired_reclaimed_single_commit(database, tmp_path):
    from classroom_interpreter.storage.repository import SessionRepository

    session_id, translation = make_session_with_done_asr(
        database, tmp_path, utterances=1, drain=False
    )
    repository = SessionRepository(database)
    claimed = repository.claim_next_translation_segment(
        "ghost-worker", lease_seconds=0.05, max_attempts=5
    )
    assert claimed is not None
    time.sleep(0.08)
    run_steps(translation)
    with database.connect() as connection:
        results = connection.execute(
            "SELECT COUNT(*) AS n FROM translation_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        rows = connection.execute(
            "SELECT state FROM segments WHERE session_id = ?", (session_id,)
        ).fetchall()
    assert results == 1
    assert rows[0]["state"] == "DONE"


def test_source_hash_mismatch_fails_closed(database, tmp_path):
    class WrongHash(MockTranslationProvider):
        async def translate(self, request):
            result = await super().translate(request)
            return result.model_copy(
                update={"english_text_sha256": "0" * 64}
            )

    session_id = h.make_session(database)
    translation = build_pipeline(database, tmp_path, session_id, utterances=1)
    translation.provider = WrongHash()
    run_steps(translation)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "TRANSLATION_FAILED"
    assert rows[0]["last_error_category"] == "MALFORMED"
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM translation_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
    assert count == 0
