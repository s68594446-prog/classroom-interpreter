from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from classroom_interpreter.domain.models import WebSocketEvent, WebSocketEventKind

router = APIRouter()


@router.websocket("/ws/sessions/{session_id}")
async def session_events(websocket: WebSocket, session_id: str, cursor: int = -1) -> None:
    """Replays persisted events after `cursor`, then streams live events.

    Replay comes from the websocket_events table (D-028), so a refresh or
    reconnect reproduces the exact committed history without duplicates. The
    per-connection `last` cursor drops any replayed duplicate.
    """
    await websocket.accept()
    database = websocket.app.state.database
    runtime = getattr(websocket.app.state, "runtime", None)
    last = cursor

    async def send(event: WebSocketEvent) -> bool:
        try:
            await websocket.send_json(event.model_dump(mode="json"))
            return True
        except Exception:
            return False

    ready = WebSocketEvent(
        event_sequence=0,
        kind=WebSocketEventKind.SYSTEM_READY,
        session_id=session_id,
        payload={"replayed_after": cursor},
    )
    if not await send(ready):
        return

    for event in _replay(database, session_id, cursor):
        if not await send(event):
            return
        last = max(last, event.event_sequence)

    queue = None
    if runtime is not None:
        queue = runtime.publisher.subscribe()

    reader = asyncio.create_task(websocket.receive_text())
    try:
        while True:
            if queue is not None:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    event = None
            else:
                event = None
                await asyncio.sleep(0.25)
            if event is not None and event.event_sequence > last:
                if not await send(event):
                    break
                last = event.event_sequence
            if reader.done():
                message = reader.result()
                if message == "ping":
                    pong = WebSocketEvent(
                        event_sequence=0,
                        kind=WebSocketEventKind.SYSTEM_PONG,
                        session_id=session_id,
                        payload={},
                    )
                    if not await send(pong):
                        break
                    reader = asyncio.create_task(websocket.receive_text())
                else:
                    break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        reader.cancel()
        if queue is not None and runtime is not None:
            runtime.publisher.unsubscribe(queue)


def _replay(database, session_id: str, cursor: int) -> list[WebSocketEvent]:
    import json
    from datetime import datetime

    from classroom_interpreter.domain.models import utc_now

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_sequence, kind, payload_json, created_at
            FROM websocket_events
            WHERE session_id = ? AND event_sequence > ?
            ORDER BY event_sequence
            """,
            (session_id, cursor),
        ).fetchall()
    events = []
    for row in rows:
        try:
            occurred = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError):
            occurred = utc_now()
        events.append(
            WebSocketEvent(
                event_sequence=row["event_sequence"],
                kind=WebSocketEventKind(row["kind"]),
                session_id=session_id,
                occurred_at=occurred,
                payload=json.loads(row["payload_json"]),
            )
        )
    return events
