"""WebSocket fan-out (01_BACKEND_CONTRACT.md §4).

`seq` is monotonic across the whole server, so a client can drop anything it has
already processed. On reconnect the client sends `{"action": "resync", ...}` and
gets a full state payload back — never a delta, because a delta applied to an
unknown base is how the map goes blank.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from ..simtime import iso
from datetime import datetime, timezone

log = logging.getLogger("eventflow.ws")

# 01 §4.2 — the attendee client only ever sees these.
ATTENDEE_EVENTS = {"nudge_pushed", "journey_risk_update", "resync"}


class Connection:
    def __init__(self, socket: WebSocket, client: str, attendee_id: str | None) -> None:
        self.socket = socket
        self.client = client
        self.attendee_id = attendee_id

    def wants(self, event: str, payload: dict[str, Any]) -> bool:
        if self.client == "attendee":
            if event not in ATTENDEE_EVENTS:
                return False
            target = payload.get("attendee_id") or (payload.get("nudge") or {}).get("attendee_id")
            return target is None or target == self.attendee_id
        return True


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: list[Connection] = []
        self._seq = 0
        self._lock = asyncio.Lock()

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def connect(self, socket: WebSocket, client: str, attendee_id: str | None) -> Connection:
        await socket.accept()
        conn = Connection(socket, client, attendee_id)
        async with self._lock:
            self._connections.append(conn)
        log.info("ws connected: client=%s attendee=%s (%d open)", client, attendee_id, len(self._connections))
        return conn

    async def disconnect(self, conn: Connection) -> None:
        async with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
        log.info("ws disconnected (%d open)", len(self._connections))

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def broadcast(self, event: str, payload: dict[str, Any], sim_time: str) -> None:
        message = {
            "event": event,
            "sim_time": sim_time,
            "seq": self.next_seq(),
            "payload": payload,
        }
        async with self._lock:
            targets = [c for c in self._connections if c.wants(event, payload)]

        dead: list[Connection] = []
        for conn in targets:
            try:
                await conn.socket.send_json(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            await self.disconnect(conn)

    async def send_to(self, conn: Connection, event: str, payload: dict[str, Any], sim_time: str) -> None:
        try:
            await conn.socket.send_json(
                {"event": event, "sim_time": sim_time, "seq": self.next_seq(), "payload": payload}
            )
        except Exception:
            await self.disconnect(conn)


MANAGER = WebSocketManager()


def server_time() -> str:
    return iso(datetime.now(timezone.utc))
