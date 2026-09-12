"""The last segment of every class was silently dropped, and the summary failed.

`progress()["pending_segments"]` counted every ASR and translation state and
omitted CAPTURED -- the state a freshly sealed segment sits in until an ASR
worker leases it.

The finalization drain is the one place that matters. `seal_tail()` runs inside
the drain and produces exactly such a segment; the check immediately after it
reported pending == 0 with no progress, so the drain broke out and left the
segment untranscribed. The summary then refused the incomplete transcript and
the session went to RECOVERY_REQUIRED with no summary job ever created.

Reproduced against real audio: 3 DONE, 1 CAPTURED, no summary, exactly matching
what the owner saw on ending a class. Every existing finalization test passed
throughout, because the fake listener writes all of its chunks up front and so
never seals anything during the drain.
"""

from __future__ import annotations

import time

import pytest

from classroom_interpreter.domain.models import SegmentState

from test_live_api import make_client, wait_for_status


def test_a_captured_segment_counts_as_pending_work(tmp_path):
    """The whole defect, at the smallest scale that shows it.

    Mutation that turns this red: drop `by_state.get("CAPTURED", 0)` from
    `progress()`.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Pending"}).json()
        session_id = view["session"]["id"]
        # Wait for the app's own segmenter loop to seal the fake listener's
        # chunks. Driving the segmenter from this thread instead raced the
        # loop inside the resampler's C state and segfaulted the interpreter --
        # the segmenter is now serialised, but the right fix for the test is
        # not to reach into a running component from another thread.
        for _ in range(100):
            if runtime.progress(session_id)["segment_counts"]:
                break
            time.sleep(0.05)
        with database.connect() as connection:
            connection.execute(
                "UPDATE segments SET state = ? WHERE session_id = ?",
                (SegmentState.CAPTURED.value, session_id),
            )
            connection.commit()
        counts = runtime.progress(session_id)

    assert counts["segment_counts"].get("CAPTURED"), "no CAPTURED segment to judge"
    assert counts["pending_segments"] >= counts["segment_counts"]["CAPTURED"], (
        "a sealed-but-unprocessed segment is invisible to anything asking "
        "whether there is work left, including the finalization drain"
    )


def test_ending_a_class_leaves_nothing_captured(tmp_path):
    """Mutation that turns this red: the same one.

    Without CAPTURED in the count the drain exits while a segment is still
    waiting for its first ASR call, and that segment never reaches DONE.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Drain"}).json()
        session_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        wait_for_status(client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED"))
        counts = runtime.progress(session_id)["segment_counts"]

    assert counts.get("CAPTURED", 0) == 0, (
        f"{counts.get('CAPTURED')} segment(s) were sealed and then abandoned by "
        "the drain; their audio never reached the transcript or the summary"
    )
    assert counts.get("DONE", 0) > 0, "nothing was processed at all"


def test_the_drain_reaches_the_summary(tmp_path):
    """A dropped segment did not merely lose a sentence -- it lost the summary.

    The summary refuses an incomplete transcript, which is correct
    (`NO VALID SUMMARY = NO PURGE`), so one abandoned segment cost the whole
    document and sent the session to RECOVERY_REQUIRED.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Summary"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        final = wait_for_status(
            client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED")
        )

    assert final["session"]["status"] == "SUMMARY_SAVED", (
        "the class could not be summarised; with a segment abandoned mid-drain "
        "this is what the owner saw every time they pressed 结束上课"
    )
