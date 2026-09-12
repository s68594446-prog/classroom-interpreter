from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from classroom_interpreter.domain.models import (
    EPHEMERAL_EVENT_SEQUENCE,
    WebSocketEvent,
    WebSocketEventKind,
)
from classroom_interpreter.storage import Database


class SubscriberQueue(asyncio.Queue):
    """A subscriber queue that remembers being dropped for falling behind.

    `put_nowait` raises once 1024 events are backed up, which happens when a
    client stops reading -- a network stall is enough, because the send blocks
    and the reader stops draining. The publisher then discarded the subscriber
    silently, and the connection stayed open forever receiving nothing: the UI
    showed 已连接 over a frozen screen, with no close event to trigger the
    reconnect that would have replayed the missed events from the cursor.
    """

    dropped: bool = False


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
        self._fan_out(event)
        return sequence

    def publish_ephemeral(
        self, session_id: str, kind: WebSocketEventKind | str, payload: dict
    ) -> None:
        """Fan out to live subscribers without persisting or sequencing.

        For interim transcripts (D-046), which are display-only and superseded
        within seconds. Persisting them would be wrong twice over: they would
        enter `websocket_events` as if they were facts, and -- because that
        table is the replay log -- a reconnecting client would be handed a
        burst of stale partials describing sentences that finished long ago.

        `event_sequence` is -1 so it can never advance a client's replay
        cursor; a client that reconnects resumes from the last real event.
        """
        event = WebSocketEvent(
            event_sequence=EPHEMERAL_EVENT_SEQUENCE,
            kind=WebSocketEventKind(str(kind)),
            session_id=session_id,
            payload=payload,
        )
        self._fan_out(event)

    def _fan_out(self, event: WebSocketEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:
                # A dead or hopelessly behind subscriber never blocks the
                # pipeline -- but it must not be dropped in silence either.
                # The flag tells the connection to close so the client
                # reconnects and replays what it missed from its cursor.
                setattr(queue, "dropped", True)
                self._subscribers.discard(queue)

    def subscribe(self) -> SubscriberQueue:
        queue = SubscriberQueue(maxsize=1024)
        self._subscribers.add(queue)
        return queue

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
