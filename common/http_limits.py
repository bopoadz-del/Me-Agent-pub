"""Request size bound shared by the agent and hive HTTP apps.

FastAPI reads a request's whole body before any dependency (the bearer check)
runs, so an unbounded body is held in memory even when the caller has no
credentials. BodySizeLimit refuses a body over the cap with 413: from its
Content-Length before reading anything, or as soon as a streamed body passes
the cap. It applies to every HTTP route of the app it wraps.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

#: Largest request body either service reads. Every legitimate body (a
#: mission, a command, a block upload, a proof) is far below it.
REQUEST_BODY_MAX_BYTES = 1024 * 1024

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class _BodyTooLarge(Exception):
    pass


class BodySizeLimit:
    """ASGI middleware: 413 for any HTTP request body over ``max_bytes``."""

    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int = REQUEST_BODY_MAX_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _refuse(self, send: Send) -> None:
        body = b'{"detail":"request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.max_bytes:
                    await self._refuse(send)
                    return

        seen = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLarge:
            if not started:
                await self._refuse(send)
