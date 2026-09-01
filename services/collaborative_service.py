# -*- coding: utf-8 -*-
"""Collaborative document business service (Batch 1)."""

from db import db, execute, query_one, query_all, now_str
from html import escape as html_escape
import re
import hashlib

# Input size limits
MAX_CONTENT_TEXT = 100000
MAX_CONTENT_HTML = 500000
MAX_CONTENT_JSON = 500000
MAX_Y_STATE_BYTES = 1048576
# Checkpoint retention limits
MAX_MANUAL_CHECKPOINTS = 10  # max manual checkpoints per document

def enforce_checkpoint_limits(document_id):
    """Enforce checkpoint retention policy.
    
    Rules:
    - submitted checkpoints: always kept
    - returned checkpoints: always kept
    - manual checkpoints: keep only the newest MAX_MANUAL_CHECKPOINTS
    
    Never deletes:
    - submitted/returned checkpoints
    - checkpoints referenced by submission records
    """
    conn = db()
    try:
        # Count current manual checkpoints
        manual_cps = conn.execute(
            "SELECT id FROM collaborative_document_checkpoints "
            "WHERE document_id=? AND reason='manual' "
            "ORDER BY state_revision DESC, id DESC",
            (document_id,)
        ).fetchall()
        
        if len(manual_cps) <= MAX_MANUAL_CHECKPOINTS:
            return 0
        
        # Keep the newest MAX_MANUAL_CHECKPOINTS
        keep_ids = set(cp["id"] for cp in manual_cps[:MAX_MANUAL_CHECKPOINTS])
        delete_ids = [cp["id"] for cp in manual_cps if cp["id"] not in keep_ids]
        
        for cpid in delete_ids:
            conn.execute(
                "DELETE FROM collaborative_document_checkpoints WHERE id=? AND reason='manual'",
                (cpid,)
            )
        conn.commit()
        deleted = len(delete_ids)
        if deleted:
            import logging
            logging.getLogger(__name__).info(
                "Cleaned %d manual checkpoints for doc %s (limit %d)",
                deleted, document_id, MAX_MANUAL_CHECKPOINTS
            )
        return deleted
    finally:
        conn.close()



# XSS and content sanitization
ALLOWED_PROTOCOLS = {"http:", "https:", "mailto:"}

def sanitize_html_content(html_text):
    """Sanitize HTML content to prevent XSS."""
    if not html_text:
        return ""
    html_text = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'<iframe[^>]*>.*?</iframe>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'<object[^>]*>.*?</object>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'<embed[^>]*>.*?</embed>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'\son\w+\s*=\s*"[^"]*"', '', html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"\son\w+\s*=\s*'[^']*'", '', html_text, flags=re.IGNORECASE)
    html_text = re.sub(r'\son\w+\s*=\s*[^\s>]+', '', html_text, flags=re.IGNORECASE)
    html_text = re.sub(r'href\s*=\s*"javascript:', 'href="#"', html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"href\s*=\s*'javascript:", "href='#'", html_text, flags=re.IGNORECASE)
    return html_text

def sanitize_json_content(json_text):
    """Validate JSON content is valid."""
    if not json_text:
        return "{}"
    import json as json_mod
    try:
        parsed = json_mod.loads(json_text)
        if isinstance(parsed, (dict, list)):
            return json_text
        return "{}"
    except (json_mod.JSONDecodeError, ValueError):
        return "{}"

def validate_link_protocol(url):
    """Validate URL uses allowed protocol."""
    if not url:
        return True
    for proto in ALLOWED_PROTOCOLS:
        if url.lower().startswith(proto):
            return True
    return False




# === Batch 3: Content validation and hash helpers ===
_MIN_BASE64_SEQ = 500
_MAX_BASE64_SEQ = 5000

def _has_suspicious_base64(text):
    """Detect long Base64-like sequences in text."""
    if not text:
        return False, 0, ""
    matches = re.finditer(r"[A-Za-z0-9+/=]{100,}", text)
    for m in matches:
        length = len(m.group())
        if length >= _MIN_BASE64_SEQ:
            return True, length, m.group()[:30]
    return False, 0, ""

def validate_content(content_text, content_html, content_json):
    """Validate content before saving. Raises ValueError on invalid."""
    if len(content_text or "") > MAX_CONTENT_TEXT:
        raise ValueError("content_text exceeds %d chars" % MAX_CONTENT_TEXT)
    if len(content_html or "") > MAX_CONTENT_HTML:
        raise ValueError("content_html exceeds %d chars" % MAX_CONTENT_HTML)
    if len(content_json or "") > MAX_CONTENT_JSON:
        raise ValueError("content_json exceeds %d chars" % MAX_CONTENT_JSON)
    suspicious, length, prefix = _has_suspicious_base64(content_text or "")
    if suspicious and length > _MAX_BASE64_SEQ:
        raise ValueError("suspicious Base64 in content_text: %d chars" % length)
    return True

def compute_y_state_hash(y_state_bytes):
    if not y_state_bytes:
        return ""
    return hashlib.sha256(y_state_bytes).hexdigest()




def _has_suspicious_base64(text):
    """Detect long Base64-like sequences in text."""
    if not text:
        return False, 0, ""
    matches = re.finditer(r"[A-Za-z0-9+/=]{100,}", text)
    for m in matches:
        length = len(m.group())
        if length >= _MIN_BASE64_SEQ:
            return True, length, m.group()[:30]
    return False, 0, ""

def validate_content(content_text, content_html, content_json):
    """Validate content before saving. Raises ValueError on invalid."""
    if len(content_text or "") > MAX_CONTENT_TEXT:
        raise ValueError("content_text exceeds %d chars" % MAX_CONTENT_TEXT)
    if len(content_html or "") > MAX_CONTENT_HTML:
        raise ValueError("content_html exceeds %d chars" % MAX_CONTENT_HTML)
    if len(content_json or "") > MAX_CONTENT_JSON:
        raise ValueError("content_json exceeds %d chars" % MAX_CONTENT_JSON)
    suspicious, length, prefix = _has_suspicious_base64(content_text or "")
    if suspicious and length > _MAX_BASE64_SEQ:
        raise ValueError("suspicious Base64 in content_text: %d chars" % length)
    return True

def compute_y_state_hash(y_state_bytes):
    if not y_state_bytes:
        return ""
    return hashlib.sha256(y_state_bytes).hexdigest()

def get_or_create_document(group_id, task_id, session_no, created_by, session_id=None):
    """Get or create a session-bound collaborative document.

    ``session_id`` is optional for backward compatibility with older callers.
    When it is available, existing legacy rows are repaired in place so later
    lifecycle and research-export code can use an unambiguous session scope.
    """
    doc = query_one(
        "SELECT id,session_id,group_id,task_id,session_no,title,status,state_revision,state_size_bytes,created_by,created_at,updated_at,submitted_at FROM collaborative_documents WHERE group_id=? AND task_id=? AND session_no=?",
        (group_id, task_id, session_no)
    )
    if doc:
        if session_id and doc["session_id"] is None:
            execute(
                "UPDATE collaborative_documents SET session_id=? WHERE id=? AND session_id IS NULL",
                (int(session_id), doc["id"]),
            )
            doc = query_one(
                "SELECT id,session_id,group_id,task_id,session_no,title,status,state_revision,state_size_bytes,created_by,created_at,updated_at,submitted_at FROM collaborative_documents WHERE id=?",
                (doc["id"],),
            )
        return dict(doc)
    now = now_str()
    execute(
        "INSERT OR IGNORE INTO collaborative_documents(session_id,group_id,task_id,session_no,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (session_id, group_id, task_id, session_no, created_by, now, now)
    )
    doc = query_one(
        "SELECT id,session_id,group_id,task_id,session_no,title,status,state_revision,state_size_bytes,created_by,created_at,updated_at,submitted_at FROM collaborative_documents WHERE group_id=? AND task_id=? AND session_no=?",
        (group_id, task_id, session_no)
    )
    if doc and session_id and doc["session_id"] is None:
        execute(
            "UPDATE collaborative_documents SET session_id=? WHERE id=? AND session_id IS NULL",
            (int(session_id), doc["id"]),
        )
        doc = query_one(
            "SELECT id,session_id,group_id,task_id,session_no,title,status,state_revision,state_size_bytes,created_by,created_at,updated_at,submitted_at FROM collaborative_documents WHERE id=?",
            (doc["id"],),
        )
    return dict(doc) if doc else None


def get_document_meta(document_id):
    """Get document metadata without content fields."""
    doc = query_one(
        "SELECT id,group_id,task_id,session_no,title,status,state_revision,state_size_bytes,created_by,created_at,updated_at,submitted_at FROM collaborative_documents WHERE id=?",
        (document_id,)
    )
    return dict(doc) if doc else None


def save_snapshot(document_id, content_json, content_html, content_text):
    """Save display snapshot (content fields). Does NOT update y_state."""
    validate_content(content_text, content_html, content_json)
    content_html = sanitize_html_content(content_html)
    content_json = sanitize_json_content(content_json)
    conn = db()
    try:
        conn.execute(
            "UPDATE collaborative_documents SET content_json=?,content_html=?,content_text=?,updated_at=? WHERE id=?",
            (content_json, content_html, content_text, now_str(), document_id)
        )
        conn.commit()
    finally:
        conn.close()


def save_y_state(document_id, y_state, content_json="", content_html="", content_text="", known_revision=None):
    """Save Yjs binary state, increment revision, update content and size."""
    if y_state is not None and len(y_state) > MAX_Y_STATE_BYTES:
        raise ValueError(f"y_state exceeds {MAX_Y_STATE_BYTES} bytes")
    if len(content_text or "") > MAX_CONTENT_TEXT:
        raise ValueError(f"content_text exceeds {MAX_CONTENT_TEXT} chars")
    state_hash = compute_y_state_hash(y_state)
    conn = db()
    try:
        size = len(y_state) if y_state else 0
        if known_revision is not None:
            conn.execute(
                "UPDATE collaborative_documents SET y_state=?,content_json=?,content_html=?,content_text=?,state_revision=state_revision+1,state_size_bytes=?,y_state_hash=?,updated_at=? WHERE id=? AND state_revision=?",
                (y_state, content_json, content_html, content_text, size, state_hash, now_str(), document_id, known_revision)
            )
        else:
            conn.execute(
                "UPDATE collaborative_documents SET y_state=?,content_json=?,content_html=?,content_text=?,state_revision=state_revision+1,state_size_bytes=?,y_state_hash=?,updated_at=? WHERE id=?",
                (y_state, content_json, content_html, content_text, size, state_hash, now_str(), document_id)
            )
        if conn.total_changes == 0:
            import logging
            logging.getLogger(__name__).warning("save_y_state: no rows updated for doc %s (stale?)", document_id)
            return None
        conn.commit()
        row = conn.execute("SELECT state_revision,state_size_bytes FROM collaborative_documents WHERE id=?", (document_id,)).fetchone()
        return {"state_revision": row["state_revision"], "state_size_bytes": row["state_size_bytes"]} if row else None
    finally:
        conn.close()


def set_document_status(document_id, new_status, user_id=None):
    """Change document status. Creates a checkpoint if submitted or returned."""
    valid_statuses = {"editing", "returned", "submitted", "locked"}
    if new_status not in valid_statuses:
        raise ValueError(f"Invalid status: {new_status}. Must be one of {valid_statuses}")
    conn = db()
    try:
        doc = conn.execute(
            "SELECT id,status,state_revision,y_state,content_json,content_html,content_text FROM collaborative_documents WHERE id=?",
            (document_id,)
        ).fetchone()
        if not doc:
            return None
        now = now_str()
        conn.execute(
            "UPDATE collaborative_documents SET status=?,updated_at=?,submitted_at=CASE WHEN ? IN ('submitted','locked') AND submitted_at IS NULL THEN ? ELSE submitted_at END WHERE id=?",
            (new_status, now, new_status, now, document_id)
        )
        # Create checkpoint for important transitions
        if new_status in ("submitted", "returned") and user_id:
            conn.execute(
                "INSERT INTO collaborative_document_checkpoints(document_id,state_revision,reason,y_state,content_json,content_html,content_text,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (document_id, doc["state_revision"], new_status, doc["y_state"], doc["content_json"], doc["content_html"], doc["content_text"], user_id, now)
            )
        conn.commit()
        result = conn.execute("SELECT status,updated_at,submitted_at FROM collaborative_documents WHERE id=?", (document_id,)).fetchone()
        result_dict = dict(result) if result else None
    finally:
        conn.close()
    if new_status in {"submitted", "locked"}:
        try:
            from services.collaboration_state_finalization_service import safe_request_finalization_for_document

            safe_request_finalization_for_document(
                document_id,
                "student_submit" if new_status == "submitted" else "room_freeze",
            )
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to request state finalization for document %s",
                document_id,
                exc_info=True,
            )
    return result_dict


def create_checkpoint(document_id, reason, user_id, y_state=None, content_json="", content_html="", content_text=""):
    """Create an explicit checkpoint. reason must be: submitted, returned, or manual."""
    if reason not in ("submitted", "returned", "manual"):
        raise ValueError(f"Invalid checkpoint reason: {reason}")
    doc = query_one("SELECT state_revision FROM collaborative_documents WHERE id=?", (document_id,))
    if not doc:
        return None
    cid = execute(
        "INSERT INTO collaborative_document_checkpoints(document_id,state_revision,reason,y_state,content_json,content_html,content_text,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (document_id, doc["state_revision"], reason, y_state, content_json, content_html, content_text, user_id, now_str())
    )
    # Enforce manual checkpoint limits after creation
    if reason == "manual":
        try:
            enforce_checkpoint_limits(document_id)
        except Exception:
            pass
    return cid


def get_checkpoints(document_id, limit=50, reason=None):
    """Query checkpoints for a document, newest first."""
    if reason:
        rows = query_all(
            "SELECT * FROM collaborative_document_checkpoints WHERE document_id=? AND reason=? ORDER BY state_revision DESC, id DESC LIMIT ?",
            (document_id, reason, limit)
        )
    else:
        rows = query_all(
            "SELECT * FROM collaborative_document_checkpoints WHERE document_id=? ORDER BY state_revision DESC, id DESC LIMIT ?",
            (document_id, limit)
        )
    return [dict(r) for r in rows]


# ============================================================
# Batch 4: Two-phase commit helpers
# ============================================================

def create_prepare(document_id, state_revision, user_id):
    """Record a prepare (freeze) operation with a unique freeze_id.
    
    Args:
        document_id: The collaborative document ID.
        state_revision: The flushed state revision.
        user_id: The user preparing the submit.
        
    Returns:
        Dict with freeze_id on success, or None on failure.
    """
    import uuid
    freeze_id = uuid.uuid4().hex
    now = now_str()
    execute(
        "INSERT INTO submission_prepares(document_id, freeze_id, state_revision, created_by, created_at) VALUES(?,?,?,?,?)",
        (document_id, freeze_id, state_revision, user_id, now)
    )
    return {"freeze_id": freeze_id, "state_revision": state_revision, "document_id": document_id}


def verify_prepare(document_id, freeze_id, state_revision, user_id):
    """Verify a prepare record exists and is valid."""
    row = query_one(
        "SELECT * FROM submission_prepares WHERE document_id=? AND freeze_id=?",
        (document_id, freeze_id)
    )
    if not row:
        return None
    if row["state_revision"] != state_revision:
        return None
    if row["created_by"] != user_id:
        return None
    if row["committed"]:
        return None
    return dict(row)


def mark_prepare_committed(document_id, freeze_id):
    """Mark a prepare record as committed."""
    execute(
        "UPDATE submission_prepares SET committed=1 WHERE document_id=? AND freeze_id=?",
        (document_id, freeze_id)
    )


def cleanup_old_prepares(document_id, keep_freeze_id=None):
    """Clean up old/failed prepare records for a document."""
    if keep_freeze_id:
        execute(
            "DELETE FROM submission_prepares WHERE document_id=? AND freeze_id!=?",
            (document_id, keep_freeze_id)
        )
    else:
        execute(
            "DELETE FROM submission_prepares WHERE document_id=?",
            (document_id,)
        )
