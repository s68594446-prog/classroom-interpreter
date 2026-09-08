from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig
from classroom_interpreter.agents.transcription import TranscriptionAgent
from classroom_interpreter.domain.models import (
    ProviderError,
    ProviderErrorCategory,
    SegmentState,
)
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mocks import MockASRProvider, ScriptedASRProvider

import helpers_phase3 as h


def make_pipeline(database, tmp_path, session_id, provider=None, **agent_kwargs):
    kwargs = dict(worker_id="worker-test", jitter_fraction=0.0)
    kwargs.update(agent_kwargs)
    agent = TranscriptionAgent(
        database,
        provider or MockASRProvider(),
        **kwargs,
    )
    return agent


def make_segmenter(database, tmp_path, session_id, **config) -> SegmentAgent:
    defaults = dict(min_seconds=2.0, max_seconds=30.0, silence_milliseconds=700,
                    overlap_milliseconds=0, threshold_rms=300.0)
    defaults.update(config)
    return SegmentAgent(
        database,
        tmp_path / "sessions" / session_id,
        session_id,
        SegmenterConfig(**defaults),
    )


def segment_rows(database, session_id):
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM segments WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def asr_rows(database, session_id):
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT a.* FROM asr_results a WHERE a.session_id = ?
            ORDER BY a.segment_sequence
            """,
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def run_steps(agent: TranscriptionAgent, *, max_steps: int = 100) -> int:
    steps = 0
    while asyncio.run(agent.step()):
        steps += 1
        if steps >= max_steps:
            raise AssertionError("ASR worker did not converge")
    return steps


def speech_session(database, tmp_path, utterances=2):
    session_id = h.make_session(database)
    blocks = h.envelope_blocks([
        ("silence", 0.5), ("speech", 2.5), ("silence", 1.0),
    ] * utterances)
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    return session_id


def test_full_mock_pipeline_chunks_to_asr_done(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=2)
    segmenter = make_segmenter(database, tmp_path, session_id)
    sealed = segmenter.process_available_chunks()
    sealed += segmenter.seal_tail()
    assert len(sealed) == 2
    run_steps(make_pipeline(database, tmp_path, session_id))
    rows = segment_rows(database, session_id)
    assert [r["state"] for r in rows] == ["ASR_DONE", "ASR_DONE"]
    results = asr_rows(database, session_id)
    assert len(results) == 2
    assert [r["segment_sequence"] for r in results] == [0, 1]
    for row in rows:
        assert row["lease_owner"] is None


def test_duplicate_commit_is_idempotent(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()
    agent = make_pipeline(database, tmp_path, session_id)
    run_steps(agent)
    from classroom_interpreter.storage.repository import CommitOutcome

    repository = agent._repository
    segment = repository.get_segment(session_id, 0)
    result = repository.commit_asr_result(
        session_id, 0, asyncio.run(_replay_result(agent, segment))
    )
    assert result is CommitOutcome.ALREADY_COMMITTED
    assert len(asr_rows(database, session_id)) == 1


async def _replay_result(agent, segment):
    from classroom_interpreter.domain.models import AudioSegmentRef

    ref = AudioSegmentRef(
        segment_id=segment.id,
        session_id=segment.session_id,
        sequence=segment.sequence,
        audio_path=segment.audio_path,
        audio_sha256=segment.audio_sha256,
        duration_ms=segment.duration_ms,
    )
    return await agent.provider.transcribe(ref, language="en")


def test_retryable_errors_eventually_recover(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()

    attempts = {"0": 0}

    def flaky(segment):
        attempts["0"] += 1
        if attempts["0"] < 3:
            raise ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.SERVER,
                    retryable=True,
                    http_status=500,
                    safe_message="provider server error",
                )
            )
        return None  # fall back to scripted default below

    provider = ScriptedASRProvider(
        script={0: flaky},
        default=lambda ref: _mock_result(ref),
    )
    agent = make_pipeline(database, tmp_path, session_id, provider=provider,
                          backoff_base_seconds=0.0, max_attempts=5)
    run_steps(agent)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_DONE"
    assert rows[0]["asr_retry_count"] == 3
    assert len(provider.calls) == 3


def _mock_result(ref):
    from classroom_interpreter.domain.models import TranscriptResult, utc_now

    return TranscriptResult(
        segment_id=ref.segment_id,
        english_text=f"Mock transcript for segment {ref.sequence}.",
        provider="scripted-mock",
        model="mock-asr-v1",
        latency_ms=1,
        audio_seconds=ref.duration_ms / 1000,
        created_at=utc_now(),
    )


def test_retry_budget_exhaustion_marks_failed_and_session_error(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()
    provider = ScriptedASRProvider(
        script={
            0: ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.TIMEOUT,
                    retryable=True,
                    safe_message="ASR request timed out",
                )
            )
        }
    )
    agent = make_pipeline(database, tmp_path, session_id, provider=provider,
                          backoff_base_seconds=0.0, max_attempts=3)
    run_steps(agent)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_FAILED"
    assert rows[0]["asr_retry_count"] == 3
    assert len(provider.calls) == 3
    with database.connect() as connection:
        session = connection.execute(
            "SELECT last_error_category FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    assert session["last_error_category"] == "TIMEOUT"


@pytest.mark.parametrize(
    "error",
    [
        ProviderError(category=ProviderErrorCategory.AUTH, retryable=False,
                      http_status=401, safe_message="provider rejected credentials"),
        ProviderError(category=ProviderErrorCategory.BALANCE, retryable=False,
                      http_status=402, safe_message="balance"),
        ProviderError(category=ProviderErrorCategory.CONTENT_FILTER, retryable=False,
                      safe_message="content filter"),
    ],
)
def test_non_retryable_errors_fail_immediately(database, tmp_path, error):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()
    provider = ScriptedASRProvider(script={0: ProviderFailure(error)})
    agent = make_pipeline(database, tmp_path, session_id, provider=provider)
    run_steps(agent)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_FAILED"
    assert len(provider.calls) == 1  # exactly one attempt, no blind retry
    assert rows[0]["last_error_category"] == error.category.value


def test_malformed_empty_response_retries_then_can_recover(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()

    attempts = {"n": 0}

    def empty_then_ok(ref):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.MALFORMED,
                              retryable=True,
                              safe_message="response content is empty or non-text")
            )
        return _mock_result(ref)

    provider = ScriptedASRProvider(script={0: empty_then_ok})
    agent = make_pipeline(database, tmp_path, session_id, provider=provider,
                          backoff_base_seconds=0.0)
    run_steps(agent)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_DONE"
    assert attempts["n"] == 2


def test_expired_lease_is_reclaimed_by_another_worker(database, tmp_path):
    from classroom_interpreter.storage.repository import SessionRepository

    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()

    # Worker A claims with a 50 ms lease and then "crashes" (never commits).
    repository = SessionRepository(database)
    repository.promote_captured_segments()
    claimed = repository.claim_next_asr_segment(
        "crashed-worker", lease_seconds=0.05, max_attempts=5
    )
    assert claimed is not None
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_PROCESSING"
    assert rows[0]["lease_owner"] == "crashed-worker"
    # Wait for the lease to expire.
    import time

    time.sleep(0.08)
    # Worker B reclaims the expired lease and commits normally.
    survivor = make_pipeline(database, tmp_path, session_id, worker_id="worker-b",
                             lease_seconds=120.0)
    run_steps(survivor)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_DONE"
    assert len(asr_rows(database, session_id)) == 1  # no duplicate commit


def test_corrupt_segment_file_fails_without_provider_call(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()
    rows = segment_rows(database, session_id)
    audio_path = rows[0]["audio_path"]
    payload = bytearray(open(audio_path, "rb").read())
    payload[-2] ^= 0xFF
    open(audio_path, "wb").write(bytes(payload))

    provider = ScriptedASRProvider()
    agent = make_pipeline(database, tmp_path, session_id, provider=provider)
    run_steps(agent)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_FAILED"
    assert rows[0]["last_error_category"] == "MALFORMED"
    assert provider.calls == []


def test_missing_segment_file_fails_without_provider_call(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()
    rows = segment_rows(database, session_id)
    import os

    os.unlink(rows[0]["audio_path"])
    provider = ScriptedASRProvider()
    agent = make_pipeline(database, tmp_path, session_id, provider=provider)
    run_steps(agent)
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_FAILED"
    assert provider.calls == []


def test_rate_limit_honors_retry_after(database, tmp_path):
    session_id = speech_session(database, tmp_path, utterances=1)
    make_segmenter(database, tmp_path, session_id).process_available_chunks()

    attempts = {"n": 0}

    def throttled_then_ok(ref):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderFailure(
                ProviderError(category=ProviderErrorCategory.RATE_LIMIT,
                              retryable=True, http_status=429,
                              retry_after_ms=50,
                              safe_message="provider rate limit reached")
            )
        return _mock_result(ref)

    provider = ScriptedASRProvider(script={0: throttled_then_ok})
    agent = make_pipeline(database, tmp_path, session_id, provider=provider,
                          backoff_base_seconds=30.0)
    import time

    start = time.monotonic()
    run_steps(agent)
    # Wait out the 50 ms provider hint, then drain again.
    time.sleep(0.08)
    run_steps(agent)
    elapsed = time.monotonic() - start
    rows = segment_rows(database, session_id)
    assert rows[0]["state"] == "ASR_DONE"
    # Retry waited for the provider-provided 50 ms hint, not the 30 s backoff base.
    assert elapsed < 5
    assert attempts["n"] == 2


def test_asr_completes_out_of_order_but_rows_stay_ordered(database, tmp_path):
    """ASR may commit out of order (later sequences finishing first)."""
    session_id = h.make_session(database)
    envelope = []
    for index in range(4):
        envelope += [("silence", 0.5), ("speech", 2.5), ("silence", 1.0)]
    blocks = h.envelope_blocks(envelope)
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    segmenter = make_segmenter(database, tmp_path, session_id)
    segmenter.process_available_chunks()
    segmenter.seal_tail()
    assert len(segment_rows(database, session_id)) == 4

    latencies = {0: 0.05, 1: 0.0, 2: 0.09, 3: 0.0}

    def variable_latency(ref):
        return _delayed_result(ref, latencies[ref.sequence])

    provider = ScriptedASRProvider(default=variable_latency)
    agent = make_pipeline(database, tmp_path, session_id, provider=provider)
    asyncio.run(_drive_concurrent(agent))
    rows = segment_rows(database, session_id)
    assert all(r["state"] == "ASR_DONE" for r in rows)
    results = asr_rows(database, session_id)
    assert [r["segment_sequence"] for r in results] == [0, 1, 2, 3]


async def _delayed_result(ref, latency_s):
    await asyncio.sleep(latency_s)
    return _mock_result(ref)


async def _drive_concurrent(agent: TranscriptionAgent):
    async def worker_loop():
        for _ in range(50):
            if not await agent.step():
                return

    await asyncio.gather(worker_loop(), worker_loop())
