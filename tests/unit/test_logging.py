import json
import logging

import pytest
from pydantic import ValidationError

from classroom_interpreter.logging import MetadataJsonFormatter, MetadataLogEvent


def test_metadata_log_has_only_allowlisted_fields() -> None:
    event = MetadataLogEvent(
        agent="translation_agent",
        event="request_finished",
        status="ok",
        session_id="session-1",
        segment_id="segment-1",
        latency_ms=12,
    )
    record = logging.LogRecord("test", logging.INFO, "", 0, event, (), None)
    payload = json.loads(MetadataJsonFormatter().format(record))
    assert set(payload) == {
        "timestamp",
        "agent",
        "event",
        "status",
        "session_id",
        "segment_id",
        "latency_ms",
    }


def test_classroom_content_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MetadataLogEvent(
            agent="transcription_agent",
            event="debug",
            status="ok",
            transcript="forbidden",
        )

