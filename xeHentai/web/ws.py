"""WebSocket connection manager for real-time task progress updates."""

import asyncio
import json
from typing import Any, Dict, List, Set
from fastapi import WebSocket, WebSocketDisconnect


class ConnectionManager:
    """Manages WebSocket connections and broadcasts task events."""

    def __init__(self):
        self._connections: Dict[WebSocket, asyncio.Queue] = {}

    async def connect(self, ws: WebSocket):
        """Accept a new WebSocket connection."""
        await ws.accept()
        self._connections[ws] = asyncio.Queue(maxsize=256)

    def disconnect(self, ws: WebSocket):
        """Remove a disconnected client."""
        self._connections.pop(ws, None)

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        """Push an event to all connected clients."""
        payload = json.dumps({"type": event_type, **data})
        stale: List[WebSocket] = []
        for ws, queue in self._connections.items():
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)

    async def drain_queues(self):
        """Background task: drain per-client queues and send to WebSockets."""
        while True:
            stale: List[WebSocket] = []
            for ws, queue in list(self._connections.items()):
                try:
                    payload = queue.get_nowait()
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        stale.append(ws)
                except asyncio.QueueEmpty:
                    pass
            for ws in stale:
                self.disconnect(ws)
            await asyncio.sleep(0.05)

    def session_count(self) -> int:
        return len(self._connections)


# Singleton manager
manager = ConnectionManager()


# ── Event emission helpers (called from TaskControl hooks) ──────────────────

def emit_task_progress(guid: str, gid: str, state: int, done: int, total: int, title: str = ""):
    """Emit a task_progress event."""
    asyncio.ensure_future(manager.broadcast("task_progress", {
        "guid": guid,
        "gid": gid,
        "state": state,
        "done": done,
        "total": total,
        "title": title,
    }))


def emit_task_state_change(guid: str, new_state: int, top_status: int):
    """Emit a task_state_change event."""
    asyncio.ensure_future(manager.broadcast("task_state_change", {
        "guid": guid,
        "new_state": new_state,
        "top_status": top_status,
    }))


def emit_task_completed(guid: str, gid: str, state: str, error: str | None = None):
    """Emit a task_completed event (finished or failed)."""
    asyncio.ensure_future(manager.broadcast("task_completed", {
        "guid": guid,
        "gid": gid,
        "state": state,
        "error": error,
    }))


def emit_system_status(waiting: int, processing: int, processed: int):
    """Emit a system_status event."""
    asyncio.ensure_future(manager.broadcast("system_status", {
        "waiting": waiting,
        "processing": processing,
        "processed": processed,
    }))
