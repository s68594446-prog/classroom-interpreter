"""Two ways an unfinished class stayed unfinished.

1. Auto-pause read `silence_run`, which is zeroed whenever an utterance opens --
   including a one-frame "utterance" that is a cough and is then discarded. On a
   live session forty minutes of empty room read as 63 seconds, so the
   ten-minute auto-pause could never fire.

2. Regression guard: resuming an unfinished class must retry what failed.
   `recover()` does this by `rebuild_unconfirmed_tail()`, which deletes every
   segment after the contiguous DONE prefix and re-derives it from capture. (An
   explicit "reopen failed segments" step was written and then removed: a
   mutation probe showed the rebuild had already done its job, so the reopen
   was dead code.)
"""

from __future__ import annotations

import asyncio

from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig
from classroom_interpreter.domain.models import (
    ProviderError,
    ProviderErrorCategory,
    SegmentState,
    SessionStatus,
)
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.storage import SessionRepository

import helpers_phase3 as h
from test_live_api import FakeASR, make_client, wait_for_status


# ------------------------------------------------------------- idle silence


def idle_after(database, tmp_path, envelope):
    session_id = h.make_session(database)
    h.write_chunks(
        database, tmp_path / "cap" / session_id, session_id,
        h.envelope_blocks(envelope), chunk_seconds=1,
    )
    agent = SegmentAgent(
        database, tmp_path / "sess" / session_id, session_id, SegmenterConfig()
    )
    agent.process_available_chunks()
    return agent, session_id


def test_a_discarded_blip_does_not_reset_the_idle_count(database, tmp_path):
    """Mutation that turns this red: return `silence_run * FRAME_MS` from
    `idle_silence_ms` again."""
    agent, _ = idle_after(
        database, tmp_path,
        [("silence", 20.0), ("speech", 0.05), ("silence", 20.0)],
    )
    with database.connect() as connection:
        discarded = connection.execute(
            "SELECT discarded_utterances FROM pipeline_cursors"
        ).fetchone()[0]

    assert discarded >= 1, "the blip was never judged, so nothing was tested"
    assert agent.idle_silence_ms() >= 39_000, (
        f"a cough reset forty seconds of silence to {agent.idle_silence_ms()}ms"
    )


def test_real_speech_still_resets_the_idle_count(database, tmp_path):
    """Mutation that turns this red: drop the reset in `_seal_segment`."""
    agent, _ = idle_after(
        database, tmp_path,
        [("silence", 20.0), ("speech", 3.0), ("silence", 5.0)],
    )
    with database.connect() as connection:
        sealed = connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    assert sealed >= 1
    assert agent.idle_silence_ms() < 10_000, (
        "a sealed sentence did not reset the idle count, so auto-pause would "
        "fire in the middle of a lecture"
    )


def test_the_idle_count_survives_a_restart(database, tmp_path):
    """Mutation that turns this red: drop `idle_frames` from `_checkpoint`."""
    agent, session_id = idle_after(
        database, tmp_path,
        [("silence", 20.0), ("speech", 0.05), ("silence", 20.0)],
    )
    before = agent.idle_silence_ms()
    restarted = SegmentAgent(
        database, tmp_path / "sess" / session_id, session_id, SegmenterConfig()
    )
    restarted.process_available_chunks()

    assert restarted.idle_silence_ms() == before


# --------------------------------------------------------------- recovery


class AlwaysFails:
    name = "down"
    model = "down"

    async def transcribe(self, segment, *, language="en"):
        raise ProviderFailure(
            ProviderError(
                category=ProviderErrorCategory.SERVER,
                retryable=True,
                http_status=503,
                safe_message="provider unavailable",
            )
        )


def test_resuming_a_class_retries_its_failed_segments(tmp_path):
    """Mutation that turns this red: drop `rebuild_unconfirmed_tail()` from
    `recover()`."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    runtime.settings = runtime.settings.model_copy(update={"asr_max_attempts": 1})
    runtime.asr_provider = AlwaysFails()
    with client:
        view = client.post("/api/sessions", json={"course_name": "Outage"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        first = wait_for_status(
            client, session_id, {"SUMMARY_SAVED", "RECOVERY_REQUIRED"}, timeout=20
        )
        assert first["session"]["status"] == "RECOVERY_REQUIRED", (
            "the outage should have left the class unfinished"
        )

        runtime.asr_provider = FakeASR()  # the provider is back
        assert client.post(f"/api/recovery/{session_id}/resume").status_code == 200
        final = wait_for_status(
            client, session_id, {"SUMMARY_SAVED", "RECOVERY_REQUIRED"}, timeout=20
        )

    assert final["session"]["status"] == "SUMMARY_SAVED", (
        "resuming never retried the failed segments, so the class can never "
        "be finished"
    )


def test_a_segment_that_fails_again_still_blocks_the_summary(tmp_path):
    """D-006 is not weakened: retrying is not forgiving.

    Mutation that turns this red: have `rebuild_unconfirmed_tail` keep failed
    segments in the trusted prefix, as if they were DONE.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    runtime.settings = runtime.settings.model_copy(update={"asr_max_attempts": 1})
    runtime.asr_provider = AlwaysFails()
    with client:
        view = client.post("/api/sessions", json={"course_name": "Still down"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        wait_for_status(client, session_id, {"RECOVERY_REQUIRED"}, timeout=20)
        client.post(f"/api/recovery/{session_id}/resume")
        final = wait_for_status(
            client, session_id, {"SUMMARY_SAVED", "RECOVERY_REQUIRED"}, timeout=20
        )

    assert final["session"]["status"] == "RECOVERY_REQUIRED"
