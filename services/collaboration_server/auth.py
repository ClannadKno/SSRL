# -*- coding: utf-8 -*-
"""Authentication for collaboration server WebSocket connections.

Verifies the signed ticket passed as a query parameter.
"""
from __future__ import annotations

import os
import logging
import re
import time
from collections import defaultdict

from itsdangerous import SignatureExpired, BadSignature

logger = logging.getLogger(__name__)

from services.collaborative_token import verify_ticket
from services.collaboration_secret import ensure_collab_secret, secret_fingerprint


def get_secret_key():
    """Get the shared collaboration secret."""
    return ensure_collab_secret()


# --- Rate limiting ---
_connection_attempts: dict = {}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_RATE_LIMIT_WINDOW = _env_int("COLLAB_WS_RATE_LIMIT_WINDOW_SECONDS", 10)
_RATE_LIMIT_MAX = _env_int("COLLAB_WS_RATE_LIMIT_MAX", 10)


def check_rate_limit(ip_addr):
    """Check if this IP is within rate limits. Returns True if allowed."""
    now = time.time()
    if ip_addr not in _connection_attempts:
        _connection_attempts[ip_addr] = []
    attempts = _connection_attempts[ip_addr]
    _connection_attempts[ip_addr] = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    if len(_connection_attempts[ip_addr]) >= _RATE_LIMIT_MAX:
        logger.warning("Rate limit exceeded for IP %s", ip_addr)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] rate limited: IP %s", ip_addr)
        return False
    _connection_attempts[ip_addr].append(now)
    return True


def validate_document_id_format(doc_id):
    """Validate that a document_id is a positive integer."""
    if doc_id is None:
        return False
    if isinstance(doc_id, int) and doc_id > 0:
        return True
    return False


def sanitize_for_log(data):
    """Remove sensitive information from log strings."""
    sanitized = re.sub(r'[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{2,}){2,}', '[TOKEN]', str(data))
    sanitized = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[EMAIL]', sanitized)
    sanitized = re.sub(r'\b(sk-[A-Za-z0-9]{20,}|secret[A-Za-z0-9]{8,})\b', '[SECRET]', sanitized)
    return sanitized


def check_origin(scope):
    """Validate WebSocket Origin header."""
    headers = dict(scope.get("headers", []))
    origin = headers.get(b"origin", b"").decode("utf-8", errors="replace")
    if not origin:
        return True
    allowed = os.environ.get("COLLAB_WS_ALLOWED_ORIGINS", "").strip()
    if allowed:
        allowed_list = [o.strip() for o in allowed.split(",")]
        if origin in allowed_list:
            return True
        logger.warning("Origin %s not in allowed list", origin)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] origin rejected: %s", origin)
        return False
    return True


def validate_message_size(message, max_size=512000):
    """Validate WebSocket message size."""
    if len(message) > max_size:
        logger.warning("WS message exceeds size limit: %s bytes (max %s)", len(message), max_size)
        return False
    return True


def verify_ws_connection(scope):
    """Verify WebSocket connection using ticket in query string.

    Args:
        scope: ASGI scope dict with 'query_string' bytes.

    Returns:
        Dict with user info if valid, None if invalid.
    """
    query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
    params = {}
    if query_string:
        for part in query_string.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v

    # Log sanitization - never log full token
    token_for_log = params.get("token", "")[:20] + "..." if params.get("token") else ""
    if token_for_log:
        logger.debug("WS connection token prefix: %s", token_for_log)

    token = params.get("token", "")
    if not token:
        logger.warning("WS connection without token")
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] missing token")
        return None

    # Extract document_id from path: /ws/doc-<id> or similar
    path = scope.get("path", "")
    doc_id = None
    if path.startswith("/ws/doc-"):
        try:
            doc_id = int(path[len("/ws/doc-"):])
        except (ValueError, IndexError):
            pass

    # Validate document_id format
    if not validate_document_id_format(doc_id):
        logger.warning("Invalid document_id format in path=%s", path)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] document_id not valid: path=%s", path)
        return None

    # Rate limit by IP
    client_ip = scope.get("client", ("", 0))[0]
    if not check_rate_limit(client_ip):
        logger.warning("Rate limited WS connection from %s", client_ip)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] rate limited WS: %s", client_ip)
        return None

    secret_key = get_secret_key()
    fp = secret_fingerprint(secret_key)
    query_keys = sorted(params.keys())
    logger.info("[collab-auth] path=%s query_keys=%s secret_fp=%s result=checking",
                path, query_keys, fp)
    logger.info("WS verify: doc_id=%s secret_fp=%s", doc_id, fp)
    
    payload = verify_ticket(token, secret_key, expected_doc_id=doc_id)
    if payload is None:
        logger.warning("[collab-auth] path=%s query_keys=%s secret_fp=%s result=REJECTED",
                       path, query_keys, fp)
        logger.warning("WS ticket verification FAILED for path=%s secret_fp=%s expected_doc_id=%s",
                       path, fp, doc_id)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] ticket verify failed: path=%s secret_fp=%s doc_id=%s",
                       path, fp, doc_id)
        return None

    logger.info("WS ticket OK: doc_id=%s user_id=%s perm=%s secret_fp=%s",
                doc_id, payload.get("user_id"), payload.get("permission"), fp)
    return payload

def is_view_permission(payload):
    """Check if a ticket payload has view-only permission.
    
    Args:
        payload: Decoded ticket payload dict.
    
    Returns:
        True if permission is 'view'.
    """
    return payload is not None and payload.get("permission") == "view"


def is_edit_permission(payload):
    """Check if a ticket payload has edit permission.
    
    Args:
        payload: Decoded ticket payload dict.
    
    Returns:
        True if permission is 'edit'.
    """
    return payload is not None and payload.get("permission") == "edit"
