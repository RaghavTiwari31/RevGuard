"""
RevGuard — SSE Event Broadcaster

Manages an in-process asyncio queue that feeds the /stream SSE endpoint.
The simulate batch runner and webhook handler push TraceEvent dicts into
this queue; the SSE endpoint reads and forwards them to connected browsers.

Design:
  - Single asyncio.Queue per app instance (single Uvicorn worker as per spec)
  - Multiple SSE clients each get their own sub-queue via a fan-out registry
  - Events are JSON-serialized per the fixed SSE contract from §3
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from app.logging_config import get_logger

logger = get_logger(__name__)

# ── Fan-out registry ──────────────────────────────────────────────────────────
# Maps client_id → asyncio.Queue  (added on connect, removed on disconnect)
_subscribers: dict[str, asyncio.Queue] = {}


def subscribe(client_id: str, maxsize: int = 200) -> asyncio.Queue:
    """Register a new SSE client. Returns its dedicated queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _subscribers[client_id] = q
    logger.info("sse.client_connected", extra={"client_id": client_id, "total": len(_subscribers)})
    return q


def unsubscribe(client_id: str) -> None:
    """Remove a disconnected SSE client."""
    _subscribers.pop(client_id, None)
    logger.info("sse.client_disconnected", extra={"client_id": client_id, "remaining": len(_subscribers)})


async def broadcast(event: dict) -> None:
    """
    Fan-out an event dict to all connected SSE clients.
    If a client's queue is full (slow consumer), the event is dropped for
    that client only — we never block the broadcaster.
    """
    if not _subscribers:
        return

    payload = json.dumps(event, default=str)
    dropped = 0
    for client_id, q in list(_subscribers.items()):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dropped += 1

    if dropped:
        logger.warning("sse.events_dropped", extra={"dropped": dropped})


async def stream_events(client_id: str) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted strings for one client.
    Sends a heartbeat every 15 seconds to keep the connection alive.
    """
    q = subscribe(client_id)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                # Heartbeat comment keeps the connection alive through proxies
                yield ": heartbeat\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        unsubscribe(client_id)
