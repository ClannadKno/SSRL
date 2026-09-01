# -*- coding: utf-8 -*-
"""YRoom exception handler for graceful client disconnection."""
from __future__ import annotations

import logging


def yroom_exception_handler(exception: Exception, log: logging.Logger) -> bool:
    """Handle YRoom exceptions: swallow ClientDisconnected gracefully.

    ClientDisconnected is a normal WebSocket lifecycle event that occurs when
    a browser tab closes or navigates away. Without handling it, YRoom re-raises
    the exception, causing WebsocketServer.serve() to enter cleanup paths that
    try to send awareness updates on the already-closed connection, resulting in
    wsproto LocalProtocolError cascades.
    """
    from uvicorn.protocols.utils import ClientDisconnected
    if isinstance(exception, ClientDisconnected):
        log.warning("Client disconnected (ignored): %s", exception)
        return True
    return False
