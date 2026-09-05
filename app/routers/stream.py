"""
RevGuard — SSE Stream Router

GET /stream  — Server-Sent Events endpoint for the live dashboard.
GET /stream/stats — Current batch run statistics snapshot.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.sse import stream_events

router = APIRouter()


@router.get("/stream", tags=["sse"])
async def sse_stream(request: Request):
    """
    Server-Sent Events endpoint.  The dashboard EventSource connects here
    and receives TraceEvent JSON objects as the batch runner processes events.

    CORS is handled by the app-level CORSMiddleware — no special SSE config needed.
    """
    client_id = str(uuid.uuid4())

    return StreamingResponse(
        stream_events(client_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # Disable Nginx buffering on Render
            "Connection": "keep-alive",
        },
    )
