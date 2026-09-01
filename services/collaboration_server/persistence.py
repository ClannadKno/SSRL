# -*- coding: utf-8 -*-
"""Persistence layer for the collaboration server.

Saves/loads y_state BLOB to/from the main SSRL database with
hash-based deduplication and revision-coordination support.

Key design decisions (Batch 3):
  - The WebSocket server is the sole authority for Yjs state persistence.
  - Each save records a SHA-256 hash of the y_state for dedup.
  - The caller (ManagedRoom) provides the known_revision; if it does
    not match the DB revision, the save is rejected (stale write guard).
  - Periodic saves from the server skip unchanged state (hash match).
  - Frontend snapshot endpoint no longer carries y_state_base64.

Each process uses its own SQLite connection.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)
BUSY_TIMEOUT_MS = 5000
MAX_SNAPSHOT_BYTES = 1048576


def get_db_path():
    """Get SSRL database path from environment or default."""
    return os.environ.get("SSRL_ESP_DB_PATH",
                          os.path.join(os.path.dirname(__file__), "..", "..", "ssrl_esp.db"))



def _get_conn(db_path=None):
    """Create a new SQLite connection for this process."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    return conn


def _compute_hash(y_state_bytes):
    """Return the SHA-256 hex digest of y_state bytes."""
    if not y_state_bytes:
        return ""
    return hashlib.sha256(y_state_bytes).hexdigest()


def get_document_revision(document_id, db_path=None):
    """Return the current state_revision for a document, or None if not found."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT state_revision FROM collaborative_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        return row["state_revision"] if row else None
    finally:
        conn.close()


def get_document_metadata(document_id, db_path=None):
    """Return full metadata row for a document, or None if not found.

    Returns dict with keys: document_id, y_state, state_revision,
    state_size_bytes, status, group_id, task_id, session_no, y_state_hash.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, y_state, state_revision, state_size_bytes, status, "
            "group_id, task_id, session_no, y_state_hash "
            "FROM collaborative_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "document_id": row["id"],
            "y_state": row["y_state"],
            "state_revision": row["state_revision"],
            "state_size_bytes": row["state_size_bytes"],
            "status": row["status"],
            "group_id": row["group_id"],
            "task_id": row["task_id"],
            "session_no": row["session_no"],
            "y_state_hash": row["y_state_hash"],
        }
    finally:
        conn.close()


def load_document_state(document_id, db_path=None):
    """Load y_state and metadata from collaborative_documents."""
    return get_document_metadata(document_id, db_path)


def save_document_state(document_id, y_state_bytes, db_path=None, known_revision=None):
    """Save y_state as full snapshot with hash, increment revision.

    Args:
        document_id: The document ID.
        y_state_bytes: Full Yjs state as bytes.
        db_path: Optional database path override.
        known_revision: If provided, save only if DB revision matches.

    Returns:
        Dict with 'state_revision' and 'state_size_bytes', or None on failure.
    """
    conn = _get_conn(db_path)
    try:
        size = len(y_state_bytes) if y_state_bytes else 0
        state_hash = _compute_hash(y_state_bytes)
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        if known_revision is not None:
            conn.execute(
                "UPDATE collaborative_documents SET "
                "y_state=?, state_revision=state_revision+1, "
                "state_size_bytes=?, y_state_hash=?, updated_at=? "
                "WHERE id=? AND state_revision=?",
                (y_state_bytes, size, state_hash, now, document_id, known_revision),
            )
        else:
            conn.execute(
                "UPDATE collaborative_documents SET "
                "y_state=?, state_revision=state_revision+1, "
                "state_size_bytes=?, y_state_hash=?, updated_at=? "
                "WHERE id=?",
                (y_state_bytes, size, state_hash, now, document_id),
            )
        if conn.total_changes == 0:
            logger.warning("save_document_state: no rows updated for doc %s (stale revision?)",
                           document_id)
            return None

        conn.commit()
        row = conn.execute(
            "SELECT state_revision, state_size_bytes FROM collaborative_documents WHERE id=?",
            (document_id,)
        ).fetchone()
        if row:
            return {"state_revision": row["state_revision"],
                    "state_size_bytes": row["state_size_bytes"]}
        return None
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
        logger.error("SQLite error saving y_state for doc %s: %s", document_id, e)
        return None
    finally:
        conn.close()


def verify_document_status(document_id, db_path=None):
    """Check document status from DB (fresh read, not trusting cache).

    Returns:
        Dict with 'status' and 'group_id', or None if document not found.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, status, group_id FROM collaborative_documents WHERE id=?",
            (document_id,)
        ).fetchone()
        if row is None:
            return None
        return {"status": row["status"], "group_id": row["group_id"]}
    finally:
        conn.close()


def count_idle_writes(document_id, since_revision, db_path=None):
    """Count how many times a document was saved with the same revision (idle writes)."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT state_revision FROM collaborative_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if not row:
            return 0
        return max(0, row["state_revision"] - since_revision)
    finally:
        conn.close()


def get_recent_snapshot_stats(document_id, minutes=60, db_path=None):
    """Get snapshot statistics for a document."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT state_revision, y_state_hash, updated_at "
            "FROM collaborative_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if not row:
            return {"total_saves": 0, "revision_start": 0, "revision_end": 0, "hash_changes": 0}
        return {
            "total_saves": 1,
            "revision_start": row["state_revision"],
            "revision_end": row["state_revision"],
            "hash_changes": 0,
        }
    finally:
        conn.close()
