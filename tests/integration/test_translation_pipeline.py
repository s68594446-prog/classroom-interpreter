from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from classroom_interpreter.agents.context import ContextAgent, estimate_tokens
from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig
from classroom_interpreter.agents.transcription import TranscriptionAgent
from classroom_interpreter.agents.translation import TranslationAgent, TranslationSequencer
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
    sequencer = TranslationSequencer(agent.database)
    steps = 0
    while True:
        worked = asyncio.run(agent.step())
        released = False
        while asyncio.run(sequencer.step()):
            released = True
        if not worked and not released:
            break
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
    # them in order; commit_translation_result_direct rejects out-of-order commits.
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


def test_slow_head_does_not_block_later_translation_processing(database, tmp_path):
    """Provider work finishes out of order; only public release waits for N."""
    session_id, base = make_session_with_done_asr(
        database, tmp_path, utterances=3, drain=False
    )

    class SlowHead(MockTranslationProvider):
        def __init__(self):
            super().__init__()
            self.release_head = asyncio.Event()
            self.started = set()

        async def translate(self, request):
            self.started.add(request.sequence)
            if request.sequence == 0:
                await self.release_head.wait()
            return await super().translate(request)

    provider = SlowHead()
    workers = [
        TranslationAgent(
            database, provider, worker_id=f"parallel-{index}", jitter_fraction=0.0
        )
        for index in range(3)
    ]

    async def exercise():
        tasks = [asyncio.create_task(worker.step()) for worker in workers]
        await asyncio.gather(tasks[1], tasks[2])
        assert not tasks[0].done()
        with database.connect() as connection:
            staged = connection.execute(
                """
                SELECT segment_sequence FROM translation_staged_results
                WHERE session_id = ? ORDER BY segment_sequence
                """,
                (session_id,),
            ).fetchall()
            public = connection.execute(
                "SELECT COUNT(*) AS n FROM translation_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()["n"]
        assert provider.started == {0, 1, 2}
        assert [row["segment_sequence"] for row in staged] == [1, 2]
        assert public == 0
        provider.release_head.set()
        await tasks[0]
        sequencer = TranslationSequencer(database)
        released = 0
        while await sequencer.step():
            released += 1
        assert released == 3

    asyncio.run(exercise())
    with database.connect() as connection:
        visible = connection.execute(
            """
            SELECT segment_sequence FROM translation_results
            WHERE session_id = ? ORDER BY segment_sequence
            """,
            (session_id,),
        ).fetchall()
        staged_left = connection.execute(
            "SELECT COUNT(*) AS n FROM translation_staged_results WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        done_states = connection.execute(
            "SELECT state, COUNT(*) AS n FROM segments WHERE session_id = ? GROUP BY state",
            (session_id,),
        ).fetchall()
    assert [row["segment_sequence"] for row in visible] == [0, 1, 2]
    assert staged_left == 0
    assert {row["state"]: row["n"] for row in done_states} == {"DONE": 3}


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
    outcome = repository.commit_translation_result_direct(session_id, 0, replay)
    assert outcome is CommitOutcome.ALREADY_COMMITTED


def test_a_terminal_failure_does_not_block_the_sentences_after_it(database, tmp_path):
    """This test asserted the opposite until a live session showed the cost.

    The old contract was that a terminal failure blocks release: the cursor
    stops, later segments stay staged, and nothing after the failure is ever
    shown. The reasoning was sound -- the reader must not be handed sentence 4
    without sentence 3, because the transcript would read as continuous when a
    sentence is missing.

    What it means in practice is that the first failure ends the captions for
    the rest of the lecture. Observed in the owner's own session: sequence 2
    failed ASR, sequences 4 and 5 sat staged and fully translated in the
    database, paid for, and could never reach the screen. Nothing said why.

    The integrity concern is real, so it is answered directly rather than by
    stalling: the cursor steps over what can never produce a result and a
    `subtitle.gap` event is published for it, so the gap is marked on screen
    instead of being spliced out. D-006 is untouched -- the segment still
    counts as failed, the class still cannot be summarised as complete.
    """
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
    assert rows[2]["state"] == "DONE", "the sentence after a failure never arrived"
    assert rows[3]["state"] == "DONE"
    with database.connect() as connection:
        cursor = connection.execute(
            "SELECT next_translation_sequence, last_error_category FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    assert cursor["next_translation_sequence"] == 4
    assert cursor["last_error_category"] == "AUTH"
    with database.connect() as connection:
        staged = connection.execute(
            "SELECT segment_sequence FROM translation_staged_results WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    assert staged == [], "staged results were left stranded behind the failure"


def test_a_skipped_sentence_is_announced_not_spliced_out(database, tmp_path):
    """Stepping over a failure silently would join two sentences that were not
    spoken next to each other, and the reader could not tell.

    Mutation that turns this red: advance the cursor without publishing
    `subtitle.gap`.
    """
    class FailOne(MockTranslationProvider):
        async def translate(self, request):
            if request.sequence == 1:
                raise ProviderFailure(
                    ProviderError(category=ProviderErrorCategory.AUTH,
                                  retryable=False, http_status=401,
                                  safe_message="provider rejected credentials")
                )
            return await super().translate(request)

    events = []
    session_id, translation = make_session_with_done_asr(
        database, tmp_path, drain=False
    )
    translation.provider = FailOne()
    sequencer = TranslationSequencer(
        database,
        event_sink=lambda kind, payload, sid=None: events.append((kind, payload)),
    )
    steps = 0
    while steps < 200:
        worked = asyncio.run(translation.step())
        released = False
        while asyncio.run(sequencer.step()):
            released = True
        if not worked and not released:
            break
        steps += 1

    gaps = [payload for kind, payload in events if kind == "subtitle.gap"]
    assert [gap["sequence"] for gap in gaps] == [1], (
        f"the missing sentence was not announced; events were {events}"
    )
    assert gaps[0]["reason"] == "TRANSLATION_FAILED"


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


def test_translation_retry_reuses_first_attempt_context_snapshot(database, tmp_path):
    session_id, worker = make_session_with_done_asr(
        database, tmp_path, utterances=3, drain=False
    )

    class CaptureRetryContext(MockTranslationProvider):
        def __init__(self):
            super().__init__()
            self.context_sequences = []

        async def translate(self, request):
            self.context_sequences.append(
                tuple(item.sequence for item in request.context.previous_segments)
            )
            if len(self.context_sequences) == 1:
                raise ProviderFailure(
                    ProviderError(
                        category=ProviderErrorCategory.SERVER,
                        retryable=True,
                        http_status=500,
                        safe_message="injected first-attempt failure",
                    )
                )
            return await super().translate(request)

    async def exercise():
        sequencer = TranslationSequencer(database)
        # Commit sequence 0, establishing initial context X=(0,).
        assert await worker.step()
        assert await sequencer.step()

        # Hold sequence 1 in processing so sequence 2 can make its first attempt
        # before sequence 1 changes the latest committed context.
        held_one = worker._repository.claim_next_translation_segment(
            "held-sequence-one", lease_seconds=60, max_attempts=5
        )
        assert held_one is not None and held_one.sequence == 1

        provider = CaptureRetryContext()
        retrying = TranslationAgent(
            database,
            provider,
            worker_id="retrying-sequence-two",
            backoff_base_seconds=0.0,
            jitter_fraction=0.0,
        )
        assert await retrying.step()  # sequence 2 fails with snapshot X
        assert provider.context_sequences == [(0,)]

        # Finish and release sequence 1. A newly built context for sequence 2
        # is now (0, 1), proving the ambient context changed.
        await worker._process_claim(held_one)
        assert await sequencer.step()
        assert [
            item.sequence
            for item in ContextAgent(database).build(session_id, 2).previous_segments
        ] == [0, 1]

        assert await retrying.step()
        assert provider.context_sequences == [(0,), (0,)]
        assert await sequencer.step()

    asyncio.run(exercise())
    with database.connect() as connection:
        snapshot_count = connection.execute(
            """
            SELECT COUNT(*) AS n FROM translation_context_snapshots
            WHERE session_id = ? AND segment_sequence = 2
            """,
            (session_id,),
        ).fetchone()["n"]
    assert snapshot_count == 1


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
