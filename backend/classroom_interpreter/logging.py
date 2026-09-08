from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


class MetadataLogEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent: str
    event: str
    status: str
    session_id: str | None = None
    segment_id: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    schema_version: int | None = Field(default=None, ge=0)
    error_code: str | None = None


class MetadataJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, MetadataLogEvent):
            payload = record.msg.model_dump(mode="json", exclude_none=True)
        else:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "application",
                "event": "unstructured_log",
                "status": record.levelname.lower(),
            }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(MetadataJsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_metadata(logger: logging.Logger, event: MetadataLogEvent) -> None:
    logger.info(event)
