import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket


class ConnectionHub:
    def __init__(self):
        self.agents: dict[str, WebSocket] = {}
        self.browsers: set[WebSocket] = set()
        self.last_seen: dict[str, datetime] = {}
        self.telemetry: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def register_agent(self, station_id: str, socket: WebSocket) -> None:
        async with self._lock:
            old = self.agents.get(station_id)
            self.agents[station_id] = socket
            self.last_seen[station_id] = datetime.now(UTC)
        if old and old is not socket:
            await old.close(code=4001, reason="replaced by a new connection")

    async def remove_agent(self, station_id: str, socket: WebSocket) -> None:
        async with self._lock:
            if self.agents.get(station_id) is socket:
                self.agents.pop(station_id, None)

    async def send_command(self, station_id: str, message: dict[str, Any]) -> bool:
        socket = self.agents.get(station_id)
        if not socket:
            return False
        await socket.send_json(message)
        return True

    async def broadcast(self, message: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for socket in tuple(self.browsers):
            try:
                await socket.send_json(message)
            except Exception:
                stale.append(socket)
        for socket in stale:
            self.browsers.discard(socket)

