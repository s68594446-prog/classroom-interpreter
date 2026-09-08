from __future__ import annotations

import json
from dataclasses import dataclass, field

from classroom_interpreter.domain.models import WebSocketEvent, WebSocketEventKind
from classroom_interpreter.storage import Database


@dataclass(slots=True)
class EventPublisher:
    """Persists every UI-visible fact first (websocket_events), then fans out.

    publish() is synchronous: the DB insert and the subscriber fan-out happen
    atomically within one event-loop step, so every subscriber observes events
    in event_sequence order (D-028). Pings/pongs are never persisted.
    """

    database: Database
    _subscribers: set = field(default_factory=set, repr=False)

    def publish(self, session_id: str, kind: WebSocketEventKind | str, payload: dict) -> int:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(event_sequence), -1) + 1 AS next
                FROM websocket_events WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            sequence = int(row["next"])
            connection.execute(
                """
                INSERT INTO websocket_events(
                    session_id, event_sequence, kind, payload_json, created_at
                ) VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (session_id, sequence, str(kind), json.dumps(payload, sort_keys=True)),
            )
        event = WebSocketEvent(
            event_sequence=sequence,
            kind=WebSocketEventKind(str(kind)),
            session_id=session_id,
            payload=payload,
        )
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:
                # A dead subscriber never blocks the pipeline.
                self._subscribers.discard(queue)
        return sequence

    def subscribe(self):
        queue = set()  # placeholder to satisfy type checkers
        del queue
        import asyncio

        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue) -> None:
        self._subscribers.discard(queue)

    def replay(self, session_id: str, cursor: int = -1) -> list[WebSocketEvent]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_sequence, kind, payload_json, created_at
                FROM websocket_events
                WHERE session_id = ? AND event_sequence > ?
                ORDER BY event_sequence
                """,
                (session_id, cursor),
            ).fetchall()
        return [
            WebSocketEvent(
                event_sequence=row["event_sequence"],
                kind=WebSocketEventKind(row["kind"]),
                session_id=session_id,
                occurred_at=datetime_from_iso(row["created_at"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]


def datetime_from_iso(value: str):
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return utc_now_fallback()


def utc_now_fallback():
    from classroom_interpreter.domain.models import utc_now

    return utc_now()
