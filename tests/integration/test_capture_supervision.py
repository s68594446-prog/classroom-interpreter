"""A microphone that stops must not look like a microphone that is working.

Measured during a 29-minute run: an ADC discontinuity killed the capture thread
at t+23.5 minutes. Chunk writing stopped dead. The session stayed RECORDING,
`/health` stayed green, the UI showed nothing, and the failure surfaced only six
minutes later when the class was ended -- at which point the audio was gone.

`AudioListener.check()` exists precisely to detect this, and nothing in the
running application called it. Its only callers were the standalone audio CLI
spike and one unit test, so the whole detection mechanism was dead code in the
product.

The session's `last_error_category` / `last_error_safe_message` are what the UI
reads. They were left null while the reason sat in `capture_events` and the
state-transition audit, neither of which is on screen.
"""

from __future__ import annotations

import time

import pytest

from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import WebSocketEventKind

from test_live_api import make_client


class FaultingListener:
    """A listener that reports itself unhealthy, the way a dead device does."""

    def __init__(self, database, root, *, fault="DEVICE_STOPPED_OR_STALLED"):
        self.fault = None
        self.fault_detail = None
        self._pending_fault = fault
        self.checks = 0
        self.closed = False
        self.session_id = None

    async def start(self, session_id):
        self.session_id = session_id

    async def stop(self):
        self.closed = True

    def close(self):
        self.closed = True

    def check(self) -> bool:
        self.checks += 1
        if self.checks >= 2:
            self.fault = self._pending_fault
            self.fault_detail = "OSError"
            return False
        return True

    def snapshot(self):
        return {"capture_active": self.fault is None, "state": "RECORDING",
                "reason_code": self.fault}


def arm(tmp_path, **settings):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    listener = FaultingListener(database, tmp_path)
    runtime.listener_factory = lambda root: listener
    runtime.settings = runtime.settings.model_copy(
        update={"capture_check_seconds": 0.05, **settings}
    )
    return client, database, runtime, listener


def test_the_supervisor_actually_calls_check(tmp_path):
    """Mutation that turns this red: remove the capture-supervisor task."""
    client, database, runtime, listener = arm(tmp_path)
    with client:
        client.post("/api/sessions", json={"course_name": "Supervised"})
        time.sleep(0.6)
    assert listener.checks >= 2, (
        "nothing ever asked the listener whether it was still alive; this is "
        "the dead-code path that let capture stop silently"
    )


def test_a_dead_microphone_is_written_where_the_ui_can_see_it(tmp_path):
    """Mutation that turns this red: drop the `update_session_error` call.

    The reason existed in `capture_events` and in the audit table the whole
    time. Neither is rendered anywhere, so the class appeared in the recovery
    list with no explanation.
    """
    client, database, runtime, listener = arm(tmp_path)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Dead"}).json()
        session_id = view["session"]["id"]
        time.sleep(0.6)
        record = runtime.repository.get_session(session_id)

    assert record.last_error_category == "CAPTURE", (
        "the session carries no error, so the UI has nothing to show and the "
        "owner sees an unfinished class with no reason"
    )
    assert "capture stopped" in (record.last_error_safe_message or "")


def test_the_fault_detail_survives(tmp_path):
    """A reason code alone cannot tell a full disk from a lost device.

    Mutation that turns this red: drop `fault_detail` from the safe message.
    """
    client, database, runtime, listener = arm(tmp_path)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Detail"}).json()
        session_id = view["session"]["id"]
        time.sleep(0.6)
        record = runtime.repository.get_session(session_id)
    assert "OSError" in (record.last_error_safe_message or "")


def test_the_error_message_carries_no_classroom_content(tmp_path):
    """Ordinary logs and error fields stay content-free."""
    client, database, runtime, listener = arm(tmp_path)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Privacy"}).json()
        session_id = view["session"]["id"]
        time.sleep(0.6)
        record = runtime.repository.get_session(session_id)
    message = record.last_error_safe_message or ""
    assert "Privacy" not in message, "the course name leaked into an error field"
    assert len(message) < 200


def test_a_healthy_microphone_is_left_alone(tmp_path):
    """The supervisor must not invent faults.

    Mutation that turns this red: report a fault whatever `check()` returns.
    """
    class Healthy(FaultingListener):
        def check(self):
            self.checks += 1
            return True

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    listener = Healthy(database, tmp_path)
    runtime.listener_factory = lambda root: listener
    runtime.settings = runtime.settings.model_copy(
        update={"capture_check_seconds": 0.05}
    )
    with client:
        view = client.post("/api/sessions", json={"course_name": "Fine"}).json()
        session_id = view["session"]["id"]
        time.sleep(0.4)
        record = runtime.repository.get_session(session_id)
    assert listener.checks >= 2
    assert record.last_error_category is None


def test_the_check_interval_is_configurable_and_frequent(tmp_path):
    settings = Settings(_env_file=None, classroom_interpreter_data_dir=tmp_path / "d")
    assert settings.capture_check_seconds <= 5, (
        "a slow check means minutes of a class can be lost before anyone is told"
    )


def test_the_cause_survives_the_consequence(tmp_path, database):
    """A later error must not erase the one that explains the session.

    The real sequence, measured on a live run: capture dies at t+23min; ending
    the class then fails the summary, because the transcript is incomplete --
    and the summary failure overwrote the capture fault. What the recovery
    screen said was "summary failed", which is true, useless, and hides the
    only thing that explains the class.

    Mutation that turns this red: drop the COALESCE from
    `_record_session_error` so `first_error_*` is overwritten like
    `last_error_*`.
    """
    from classroom_interpreter.storage import SessionRepository

    repository = SessionRepository(database)
    session_id = repository.create_session("Cause", app_version="test").id
    repository.update_session_error(
        session_id, category="CAPTURE", safe_message="microphone capture stopped"
    )
    repository.update_session_error(
        session_id, category="SUMMARY", safe_message="summary failed: X"
    )

    record = repository.get_session(session_id)
    assert record.last_error_category == "SUMMARY"
    assert record.first_error_category == "CAPTURE", (
        "the consequence overwrote the cause"
    )
    assert record.first_error_safe_message == "microphone capture stopped"
