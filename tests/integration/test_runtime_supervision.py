from __future__ import annotations

import asyncio

import pytest

from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import SegmentState, utc_now
from classroom_interpreter.storage import SessionRepository
from classroom_interpreter.runtime.pipeline import SessionOrchestrator


def test_segmenter_unexpected_exception_is_recorded_and_loop_continues(
    database, tmp_path
):
    """Regression for the reviewer probe that propagated `RuntimeError`.

    The first segmenter step raises the same unexpected exception. The test
    proves the loop reaches a second step, another agent keeps running, and the
    content-free fault record is observable instead of task termination.
    """

    class FlakySegmenter:
        calls = 0

        def process_available_chunks(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("probe boom")
            return ()

    class HealthyWorker:
        worker_id = "asr-probe"
        calls = 0

        async def step(self):
            self.calls += 1
            return False

    runtime = SessionOrchestrator(
        Settings(
            _env_file=None,
            classroom_interpreter_data_dir=tmp_path / "data",
        ),
        database,
        asr_provider=object(),
        translation_provider=object(),
    )
    runtime._segmenter = FlakySegmenter()
    healthy = HealthyWorker()

    async def exercise():
        segment_task = asyncio.create_task(runtime._segmenter_loop())
        other_task = asyncio.create_task(runtime._asr_loop(healthy))
        deadline = asyncio.get_running_loop().time() + 2
        while runtime._segmenter.calls < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert not segment_task.done()
        runtime._stop.set()
        await asyncio.gather(segment_task, other_task)

    asyncio.run(exercise())
    assert runtime._segmenter.calls >= 2
    assert healthy.calls >= 1
    assert runtime.worker_faults["segmenter"] == {
        "count": 1,
        "error_type": "RuntimeError",
    }


def _asr_pending_segment(database, tmp_path, session_id, sequence=0):
    """A segment sitting in ASR_PENDING, ready to be claimed."""
    repository = SessionRepository(database)
    segment = repository.create_segment(
        session_id=session_id,
        sequence=sequence,
        start_ms=sequence * 1000,
        end_ms=(sequence + 1) * 1000,
        overlap_ms=0,
        audio_path=tmp_path / f"seg-{sequence}.wav",
        audio_sha256=f"hash-{sequence}",
    )
    repository.transition_segment(
        session_id,
        sequence,
        SegmentState.ASR_PENDING,
        reason_code="TEST",
        actor="test",
    )
    return segment


def _isolation_runtime(database, tmp_path):
    return SessionOrchestrator(
        Settings(
            _env_file=None,
            classroom_interpreter_data_dir=tmp_path / "data",
        ),
        database,
        asr_provider=object(),
        translation_provider=object(),
    )


class _FlakyWorker:
    """Raises an unexpected exception on the first step, then works."""

    def __init__(self, worker_id="flaky"):
        self.worker_id = worker_id
        self.calls = 0

    async def step(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("probe boom")
        return False


@pytest.mark.parametrize(
    "loop_name, fault_key",
    [
        ("_asr_loop", "flaky"),
        ("_translation_loop", "flaky"),
        ("_sequencer_loop", "translation-sequencer"),
    ],
)
def test_every_worker_loop_isolates_unexpected_exceptions(
    database, tmp_path, loop_name, fault_key
):
    """The segmenter is not the only loop that must survive a bad step.

    The independent review's concern was that non-segmenter loops could die
    or silently swallow exceptions. Each loop here gets the same injected
    `RuntimeError`; the loop must record a content-free fault, stay alive,
    and reach a second step.

    Mutation that turns this red: delete the `except Exception` arm from the
    loop under test in `runtime/pipeline.py` -- the task then dies on the
    first step and `calls` never reaches 2.
    """
    runtime = _isolation_runtime(database, tmp_path)
    worker = _FlakyWorker()

    if loop_name == "_sequencer_loop":
        runtime._translation_sequencer = worker
        coro_factory = runtime._sequencer_loop
        args = ()
    else:
        coro_factory = getattr(runtime, loop_name)
        args = (worker,)

    async def exercise():
        task = asyncio.create_task(coro_factory(*args))
        deadline = asyncio.get_running_loop().time() + 2
        while worker.calls < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert not task.done(), "loop died on an unexpected exception"
        runtime._stop.set()
        await task

    asyncio.run(exercise())
    assert worker.calls >= 2, "loop did not reach a second step after the fault"
    assert runtime.worker_faults[fault_key] == {
        "count": 1,
        "error_type": "RuntimeError",
    }
    # Content-free: the fault record carries a type and a count, never the
    # exception message or any classroom content.
    assert "probe boom" not in repr(runtime.worker_faults)


def test_stopping_workers_releases_leases_they_died_holding(database, tmp_path):
    """A worker cancelled inside a provider call must not strand its segment.

    Found by the rewritten soak (scripts/soak_pipeline.py), not by review.
    `end_session` cancels every worker task and then gives `_finalize` a 120s
    drain budget -- but a worker cancelled mid-request leaves its segment
    leased for `asr_lease_seconds`, which is also 120s. The drain could
    therefore spend its entire budget waiting on leases whose owners were
    already dead, then summarize an incomplete transcript. (The purge gate
    still held: the summary failed validation and the session went
    RECOVERY_REQUIRED rather than to a false SUMMARY_SAVED.)

    Cancellation is awaited before the release, so every lease dropped here is
    provably stale.

    Mutation that turns this red: drop the `expire_leases_for_workers` call
    from `stop_workers_for_finalization` -- the lease then keeps its original
    far-future expiry and the segment stays unclaimable.
    """
    import helpers_phase3 as h

    session_id = h.make_session(database, "lease-release")
    repository = SessionRepository(database)

    runtime = _isolation_runtime(database, tmp_path)
    runtime.active_session = session_id
    runtime._build_workers(session_id)

    segment = _asr_pending_segment(database, tmp_path, session_id)
    claimed = repository.claim_next_asr_segment(
        runtime._asr_workers[0].worker_id, lease_seconds=120, max_attempts=5
    )
    assert claimed is not None and claimed.id == segment.id

    with database.connect() as connection:
        before = connection.execute(
            "SELECT lease_owner, lease_expires_at FROM segments WHERE id = ?",
            (segment.id,),
        ).fetchone()
    assert before["lease_owner"] == runtime._asr_workers[0].worker_id
    assert before["lease_expires_at"] > utc_now().isoformat()

    released = asyncio.run(runtime.stop_workers_for_finalization())

    assert released == 1, "the stale lease was not handed back"
    # The segment is immediately reclaimable by a fresh worker instead of
    # being stranded for the rest of the lease.
    reclaimed = repository.claim_next_asr_segment(
        "fresh-worker", lease_seconds=120, max_attempts=5
    )
    assert reclaimed is not None and reclaimed.id == segment.id


def test_stopping_workers_leaves_other_sessions_leases_alone(database, tmp_path):
    """Only leases owned by the stopped workers are released.

    Mutation that turns this red: release every live lease instead of
    filtering by `lease_owner`, which would let one orchestrator's shutdown
    steal work in flight elsewhere.
    """
    import helpers_phase3 as h

    session_id = h.make_session(database, "other-owner")
    repository = SessionRepository(database)
    segment = _asr_pending_segment(database, tmp_path, session_id)
    repository.claim_next_asr_segment(
        "somebody-elses-worker", lease_seconds=120, max_attempts=5
    )

    runtime = _isolation_runtime(database, tmp_path)
    runtime.active_session = session_id
    runtime._build_workers(session_id)
    released = asyncio.run(runtime.stop_workers_for_finalization())

    assert released == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT lease_owner, lease_expires_at FROM segments WHERE id = ?",
            (segment.id,),
        ).fetchone()
    assert row["lease_owner"] == "somebody-elses-worker"
    assert row["lease_expires_at"] > utc_now().isoformat()
