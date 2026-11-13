"""Event-loop selection for the web dashboard.

On Windows the default asyncio Proactor loop can lose its accept coroutine
when a client resets a connection (``WinError 64``). The process keeps running
and already-open SSE streams stay alive, but every new HTTP request is refused,
which the browser reports as ``Failed to fetch``.

The selector loop handles an aborted accept without tearing down the listener.
aqm does not use asyncio subprocesses, so it is a safe replacement here.
"""

from __future__ import annotations

import asyncio
import sys


def selector_loop() -> asyncio.AbstractEventLoop:
    """Loop factory passed to uvicorn via ``--loop aqm.web.loops:selector_loop``."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
