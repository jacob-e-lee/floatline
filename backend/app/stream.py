"""Server-Sent Events (SSE) broadcasting.

Uses a registry of per-client ``asyncio.Queue`` objects (in-memory, no Redis)
so that multiple tabs / browser windows each receive live events.  This honors
the project constraint of native ``asyncio.Queue`` SSE without a separate
state-management library.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

# Registry of connected SSE clients.  Each client owns its own asyncio.Queue
# so no single consumer can "steal" events from another tab/refresh.
connected_clients: set[asyncio.Queue] = set()


def new_client_queue() -> asyncio.Queue:
    """Create and register a new client queue."""
    q: asyncio.Queue = asyncio.Queue()
    connected_clients.add(q)
    return q


def remove_client_queue(q: asyncio.Queue) -> None:
    """Unregister a client queue (e.g. on disconnect)."""
    connected_clients.discard(q)


async def broadcast_event(event: dict[str, Any]) -> None:
    """Push a serializable event dict to every connected client queue.

    We snapshot the client set so a client disconnecting mid-broadcast does
    not corrupt iteration.  Per-client enqueue failures are swallowed.
    """
    payload = json.dumps(event, default=str)
    for q in list(connected_clients):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:  # pragma: no cover - unbounded by default
            pass


def format_sse(data: str) -> str:
    """Format a string payload as a single SSE event line."""
    return f"data: {data}\n\n"
