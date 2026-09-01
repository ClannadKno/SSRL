# -*- coding: utf-8 -*-
"""Internal API client for collaborating server management (Batch 4).

Calls internal endpoints on the ASGI collaboration server
with X-Internal-Secret header authentication.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import COLLAB_WS_HOST, COLLAB_WS_PORT
from services.collaboration_secret import ensure_collab_internal_secret
COLLAB_INTERNAL_SECRET = ensure_collab_internal_secret()

logger = logging.getLogger(__name__)

_INTERNAL_BASE = f"http://{COLLAB_WS_HOST}:{COLLAB_WS_PORT}"

# Timeout for internal API calls (freeze/flush should be fast)
_INTERNAL_TIMEOUT = 10.0


def _headers() -> dict:
    return {"X-Internal-Secret": COLLAB_INTERNAL_SECRET}


def _url(document_id: int, action: str) -> str:
    return f"{_INTERNAL_BASE}/internal/documents/{document_id}/{action}"


def freeze_document(document_id: int) -> dict:
    """Freeze a collaborative room, blocking new modifications.

    Returns dict with 'ok' and 'state' fields.
    """
    try:
        r = httpx.post(
            _url(document_id, "freeze"),
            headers=_headers(),
            timeout=_INTERNAL_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.RequestError as e:
        logger.error("Internal freeze call failed for doc %s: %s", document_id, e)
        return {"ok": False, "error": str(e)}


def flush_document(document_id: int) -> dict:
    """Flush y_state to DB and return state revision.

    Returns dict with 'ok', 'state_revision' fields.
    """
    try:
        r = httpx.post(
            _url(document_id, "flush"),
            headers=_headers(),
            timeout=_INTERNAL_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.RequestError as e:
        logger.error("Internal flush call failed for doc %s: %s", document_id, e)
        return {"ok": False, "error": str(e)}


def unfreeze_document(document_id: int) -> dict:
    """Unfreeze a previously frozen room, allowing edits again.

    Returns dict with 'ok' field.
    """
    try:
        r = httpx.post(
            _url(document_id, "unfreeze"),
            headers=_headers(),
            timeout=_INTERNAL_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.RequestError as e:
        logger.error("Internal unfreeze call failed for doc %s: %s", document_id, e)
        return {"ok": False, "error": str(e)}


def close_document(document_id: int) -> dict:
    """Close all connections to a room and recycle it.

    Returns dict with 'ok' field.
    """
    try:
        r = httpx.post(
            _url(document_id, "close"),
            headers=_headers(),
            timeout=_INTERNAL_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.RequestError as e:
        logger.error("Internal close call failed for doc %s: %s", document_id, e)
        return {"ok": False, "error": str(e)}


def get_document_status(document_id: int) -> dict:
    """Get room status: frozen, connections, revision, dirty.

    Returns dict with status info or {'ok': False} on failure.
    """
    try:
        r = httpx.get(
            _url(document_id, "status"),
            headers=_headers(),
            timeout=_INTERNAL_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.RequestError as e:
        logger.error("Internal status call failed for doc %s: %s", document_id, e)
        return {"ok": False, "error": str(e)}
