"""A size limit for request bodies, checked before the body is read.

Pydantic checks a URL's length only after FastAPI has read and parsed the whole
body. On its own, that would let one 200 MB request fill the server's memory
first. This middleware refuses oversized bodies at the door with
413 Content Too Large (NASA/JPL rule 3: every input has a maximum size).
"""

from typing import Final

from fastapi import HTTPException, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The largest valid body is a 2048-character URL with every character written
# as a 6-byte JSON escape such as \u0061, plus an alias: about 12.5 KB.
MAX_BODY_BYTES: Final = 16 * 1024
TOO_LARGE: Final = "Request body is too large"


class LimitBodySize:
    """ASGI middleware: `app.add_middleware(LimitBodySize)`."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            # The client told us the size up front: refuse without reading anything.
            response = JSONResponse({"detail": TOO_LARGE}, status.HTTP_413_CONTENT_TOO_LARGE)
            await response(scope, receive, send)
            return
        # No (or a small) Content-Length, e.g. a chunked upload: count as we read.
        await self.app(scope, _CountingReceive(receive, self.max_bytes), send)


class _CountingReceive:
    """Wraps ASGI `receive` and stops reading once the body passes the limit."""

    def __init__(self, receive: Receive, max_bytes: int) -> None:
        self.receive = receive
        self.remaining = max_bytes

    async def __call__(self) -> Message:
        message = await self.receive()
        if message["type"] == "http.request":
            self.remaining -= len(message.get("body", b""))
            if self.remaining < 0:
                # FastAPI passes an HTTPException raised while reading the body
                # through unchanged, so the client gets a 413, not a 400.
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LARGE)
        return message
