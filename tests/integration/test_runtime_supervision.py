from __future__ import annotations

import asyncio

import pytest

from classroom_interpreter.config import Settings
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
