"""WebSocket endpoint (01_BACKEND_CONTRACT.md §4).

`ws://localhost:8000/ws?client=command_centre`
`ws://localhost:8000/ws?client=attendee&attendee_id=att_demo_1`

The one rule that matters on reconnect: `resync` carries the **full** current
state, never a delta. A delta applied against an unknown base is how the map
goes blank mid-demo.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..services.engine import get_engine
from ..ws.manager import MANAGER

log = logging.getLogger("eventflow.ws")
router = APIRouter()


def _resync_payload(engine) -> dict:
    return engine.resync_payload()


@router.websocket("/ws")
async def websocket_endpoint(
    socket: WebSocket,
    client: str = Query(default="command_centre"),
    attendee_id: str | None = Query(default=None),
) -> None:
    engine = get_engine()
    conn = await MANAGER.connect(socket, client, attendee_id)

    # Send a resync immediately so a fresh client never renders an empty frame.
    await MANAGER.send_to(conn, "resync", _resync_payload(engine), engine.store.sim_time)

    try:
        while True:
            message = await socket.receive_json()
            if message.get("action") == "resync":
                await MANAGER.send_to(conn, "resync", _resync_payload(engine), engine.store.sim_time)
            elif message.get("action") == "ping":
                await MANAGER.send_to(conn, "pong", {}, engine.store.sim_time)
    except WebSocketDisconnect:
        await MANAGER.disconnect(conn)
    except Exception:
        log.exception("websocket error")
        await MANAGER.disconnect(conn)
