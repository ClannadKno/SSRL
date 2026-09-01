# -*- coding: utf-8 -*-
"""Short-term collaboration ticket (Batch 3 - enhanced).

Uses itsdangerous.URLSafeTimedSerializer for signed tickets.
Provides enhanced error diagnostics to differentiate
SignatureExpired, BadSignature, and other errors.
"""

import logging

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

DEFAULT_TICKET_TTL_SECONDS = 900  # 15 minutes (increased from 300)


def _make_serializer(secret_key):
    return URLSafeTimedSerializer(secret_key, salt="collab-ticket")


def issue_ticket(user_id, document_id, group_id, permission, secret_key, participant_code=None, display_name=None):
    """Issue a short-term collaboration ticket.

    Args:
        user_id: User ID.
        document_id: Collaborative document ID.
        group_id: Group ID.
        permission: 'edit' or 'view'.
        secret_key: Flask SECRET_KEY or server secret.

    Returns:
        Signed token string.
    """
    serializer = _make_serializer(secret_key)
    payload = {
        "user_id": user_id,
        "document_id": document_id,
        "group_id": group_id,
        "permission": permission,
    }
    # Optional display fields (Batch 6: participant auth adaptation)
    if participant_code is not None:
        payload["participant_code"] = participant_code
    if display_name is not None:
        payload["display_name"] = display_name
    return serializer.dumps(payload)


def verify_ticket(token, secret_key, max_age=DEFAULT_TICKET_TTL_SECONDS, expected_doc_id=None):
    """Verify and decode a collaboration ticket.

    Args:
        token: The signed token string.
        secret_key: Server secret key.
        max_age: Maximum age in seconds.
        expected_doc_id: If set, the document_id in the ticket must match.

    Returns:
        Dict with payload fields on success, or None on failure.
        On failure, logs the specific error type (SignatureExpired vs BadSignature).
    """
    serializer = _make_serializer(secret_key)
    try:
        payload = serializer.loads(token, max_age=max_age)
    except SignatureExpired:
        logger.warning("Ticket verification failed: SignatureExpired (token TTL exceeded)")
        return None
    except BadSignature:
        logger.warning("Ticket verification failed: BadSignature (secret mismatch or tampered token)")
        return None
    except Exception as e:
        logger.warning("Ticket verification failed: %s: %s", type(e).__name__, e)
        return None
    if expected_doc_id is not None and payload.get("document_id") != expected_doc_id:
        logger.warning("Ticket verification failed: doc_id mismatch (expected=%s, got=%s)",
                       expected_doc_id, payload.get("document_id"))
        return None
    return payload
