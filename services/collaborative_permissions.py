# -*- coding: utf-8 -*-
"""Collaborative document permission service (Batch 1)."""

from db import query_one


def _resolve_document_session_id(doc):
    session_id = doc.get("session_id")
    if session_id:
        return session_id
    row = query_one(
        """
        SELECT id FROM experiment_sessions
        WHERE task_id=? AND session_no=?
        ORDER BY id DESC LIMIT 1
        """,
        (doc.get("task_id"), doc.get("session_no")),
    )
    return row["id"] if row else None


def _group_discussion_blocks_edit(doc):
    session_id = _resolve_document_session_id(doc)
    if not session_id:
        return False
    try:
        from services.group_discussion_runtime_service import is_group_discussion_write_closed
        return is_group_discussion_write_closed(session_id, doc["group_id"])
    except Exception:
        return False


def _document_lifecycle_blocks_edit(doc, document_id, user_id):
    session_id = _resolve_document_session_id(doc)
    if not session_id:
        return False
    try:
        from services.session_lifecycle import is_document_writable
        return not is_document_writable(session_id, doc["group_id"], document_id, user_id)
    except Exception:
        return _group_discussion_blocks_edit(doc)


def get_document_permission(user_id, document_id):
    """Determine a user's permission level for a collaborative document.

    Args:
        user_id: The user's ID (authenticated via session/tab_token).
        document_id: The collaborative document's ID.

    Returns:
        "edit"  - user can read and write the document.
        "view"  - user can only read the document.
        None    - user has no access.
    """
    if not user_id or not document_id:
        return None
    doc = query_one(
        "SELECT group_id, task_id, session_no, session_id, status FROM collaborative_documents WHERE id=?",
        (document_id,)
    )
    if not doc:
        return None
    user = query_one("SELECT role FROM users WHERE id=?", (user_id,))
    if not user:
        return None
    role = user["role"]
    doc_group_id = doc["group_id"]
    status = doc["status"]
    if role in ("teacher", "agent"):
        return "view"
    if role == "student":
        membership = query_one(
            "SELECT group_id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        if not membership or membership["group_id"] != doc_group_id:
            return None
        if status in ("editing", "returned") and not _document_lifecycle_blocks_edit(dict(doc), document_id, user_id):
            return "edit"
        else:
            return "view"
    return None


def can_view_document(user_id, document_id):
    """Check if a user can view (read) a document."""
    return get_document_permission(user_id, document_id) is not None


def can_edit_document(user_id, document_id):
    """Check if a user can edit (write) a document."""
    return get_document_permission(user_id, document_id) == "edit"
