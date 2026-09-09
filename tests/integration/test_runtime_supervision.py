from __future__ import annotations

import asyncio

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
