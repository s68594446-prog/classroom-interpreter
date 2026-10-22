"""Ending a class must work even when the microphone does not.

`end_class` called `listener.close()` with nothing around it. If the device had
already gone -- unplugged, taken by another app, or simply faulted -- the
exception propagated out of the endpoint and the session was left in RECORDING
with no error recorded, no drain started and no summary. Pressing 结束上课 just
failed, and there was no remaining way to finish the class.

The audio is already on disk by then. Draining and summarising it does not need
the microphone, so the microphone must not be able to block it.
"""

from __future__ import annotations

import time

import pytest

from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.storage import SessionRepository

from test_live_api import make_client, wait_for_status


class HostileListener:
    """A microphone that refuses to close, the way a vanished device does."""

    def __init__(self, database, *a, **k):
        self.repository = SessionRepository(database)
        self.session_id = None
        self.fault = None
        self.fault_detail = None
        self.closed_attempts = 0

    async def start(self, session_id):
        self.session_id = session_id
        # The real listener performs this transition; without it `end_class`
        # sees a session that is not RECORDING and returns before it ever
        # reaches `close()`, so the test would pass while proving nothing.
        self.repository.transition_session(
            session_id, SessionStatus.RECORDING,
            reason_code="HOSTILE_START", actor="test_listener",
        )

    async def stop(self):
        return None

    def check(self) -> bool:
        return True

    def close(self):
        self.closed_attempts += 1
        raise OSError("device disappeared")

    def snapshot(self):
        return {"capture_active": True, "state": "RECORDING"}


def test_a_microphone_that_cannot_close_does_not_block_ending_the_class(tmp_path):
    """Mutation that turns this red: remove the try/except around
    `listener.close()` in `end_class`."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    listener = HostileListener(database)
    runtime.listener_factory = lambda root: listener
    with client:
        view = client.post("/api/sessions", json={"course_name": "Hostile"}).json()
        session_id = view["session"]["id"]
        response = client.post(f"/api/sessions/{session_id}/end")
        assert response.status_code == 200, (
            "ending the class failed outright because the microphone would not "
            "close; there is no other way to finish a class"
        )
        final = wait_for_status(
            client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED")
        )

    assert listener.closed_attempts == 1
    assert final["session"]["status"] in {"SUMMARY_SAVED", "RECOVERY_REQUIRED"}, (
        "the session never left RECORDING, so the class can never be finished"
    )


def test_the_close_failure_is_recorded_not_hidden(tmp_path):
    """Swallowing it silently would repeat the mistake it is fixing.

    Mutation that turns this red: drop the `update_session_error` call in the
    handler.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    runtime.listener_factory = lambda root: HostileListener(database)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Recorded"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        assert runtime.worker_faults.get("capture-close"), (
            "the close failure was not counted, so it never reaches /health"
        )
        wait_for_status(client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED"))

    # The durable first-error fields preserve the capture cause even when
    # the background summary has already updated last_error_*. Read after drain.
    record = runtime.repository.get_session(session_id)
    assert record.first_error_category == "CAPTURE"
    assert "OSError" in (record.first_error_safe_message or ""), (
        "the failure type was discarded, leaving no way to tell what happened"
    )
    assert "Recorded" not in (record.first_error_safe_message or ""), (
        "the course name leaked into an error field"
    )
