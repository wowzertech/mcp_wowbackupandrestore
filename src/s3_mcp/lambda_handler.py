"""AWS Lambda entrypoint.

Mangum adapts the same ASGI application uvicorn serves locally, so there is one
server and no second code path to keep in step.

The lifespan needs care. Mangum's own `lifespan="on"` runs startup and shutdown
around *every* invocation, and the MCP transport's session manager refuses to
start twice — the second request in a warm container dies with "can only be
called once per instance". So the lifespan is run here once, at cold start, and
left running for the life of the execution environment; Mangum is then told not
to touch it.

Both must share one event loop: the session manager's task group is bound to
the loop it started on, and Mangum dispatches requests with
`asyncio.get_event_loop().run_until_complete(...)`. Setting the loop as current
before Mangum is constructed is what keeps them together.
"""

from __future__ import annotations

import asyncio
import logging

from mangum import Mangum

from .app import app

logger = logging.getLogger(__name__)

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

# Held at module scope so the lifespan task is not garbage collected while the
# execution environment is warm.
_lifespan_task: asyncio.Task | None = None


async def _start_lifespan(application) -> asyncio.Task:
    """Send the ASGI startup event and return once the app reports ready.

    The task is deliberately left running: shutdown is never sent, because the
    execution environment is frozen between invocations rather than torn down,
    and a shutdown would stop the session manager the next request needs."""
    ready = asyncio.Event()
    failure: list[str] = []
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})

    async def receive() -> dict:
        return await inbox.get()

    async def send(message: dict) -> None:
        if message["type"] == "lifespan.startup.complete":
            ready.set()
        elif message["type"] == "lifespan.startup.failed":
            failure.append(message.get("message", "unknown error"))
            ready.set()

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    task = asyncio.ensure_future(application(scope, receive, send))

    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
    except TimeoutError:
        raise RuntimeError("application lifespan did not start within 20s") from None
    if failure:
        raise RuntimeError(f"application lifespan failed: {failure[0]}")
    return task


_lifespan_task = _loop.run_until_complete(_start_lifespan(app))
logger.info("lifespan started; MCP transport ready")

handler = Mangum(app, lifespan="off")
