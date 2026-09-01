# -*- coding: utf-8 -*-
"""Collaborative document HTTP API routes (Batch 1)."""

from flask import jsonify, request
from core import app
from db import query_one, get_current_session_no
from auth import login_required, current_user, get_tab_token_from_request, get_user_group_id
from services.collaborative_service import (
    get_or_create_document, get_document_meta, save_snapshot,
    set_document_status
)
from services.collaborative_permissions import get_document_permission, can_edit_document
from services.collaboration_secret import secret_fingerprint
from services.group_discussion_runtime_service import (
    enter_group_discussion_stage,
    get_group_discussion_runtime,
    group_discussion_timer_payload,
    is_group_discussion_write_closed,
)

MAX_REQUEST_BODY_BYTES = 1048576 * 2  # 2 MB for snapshot requests


def _json_err(msg, code):
    return jsonify({"error": msg}), code


def _current_document_response(user, session_ctx, group_runtime, *, document=None, permission=None, waiting=False):
    timer_fields = group_discussion_timer_payload(group_runtime)
    return {
        "ok": True,
        "document": document,
        "permission": permission,
        "display_name": user.get("display_name"),
        "participant_code": user.get("participant_code"),
        "group_discussion": group_runtime,
        "waiting": bool(waiting),
        **timer_fields,
        "session": {
            "session_id": session_ctx.get("session_id"),
            "session_no": session_ctx.get("session_no"),
            "task_id": session_ctx.get("task_id"),
            # Legacy metadata only; discussion countdown fields live at group scope.
            "time_limit_minutes": session_ctx.get("time_limit_minutes"),
            "server_time": session_ctx.get("server_time"),
        },
    }


def _waiting_for_task_response(user, session_ctx=None):
    session_ctx = session_ctx or {}
    return {
        "ok": True,
        "document": None,
        "permission": None,
        "display_name": user.get("display_name"),
        "participant_code": user.get("participant_code"),
        "group_discussion": None,
        "waiting": True,
        "waiting_reason": "waiting_task",
        "message": "等待教师发布任务",
        **group_discussion_timer_payload(None),
        "session": {
            "session_id": session_ctx.get("session_id"),
            "session_no": session_ctx.get("session_no"),
            "task_id": session_ctx.get("task_id"),
            "time_limit_minutes": session_ctx.get("time_limit_minutes"),
            "server_time": session_ctx.get("server_time"),
        },
    }


def _find_current_document(group_id, session_ctx):
    if not session_ctx or not session_ctx.get("task_id"):
        return None
    doc = query_one(
        """
        SELECT id, group_id, task_id, session_no, session_id, title, status,
               state_revision, state_size_bytes, created_by, created_at,
               updated_at, submitted_at
        FROM collaborative_documents
        WHERE group_id=?
          AND (
            session_id=?
            OR (session_id IS NULL AND task_id=? AND session_no=?)
          )
        ORDER BY CASE WHEN session_id=? THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (
            group_id,
            session_ctx["session_id"],
            session_ctx["task_id"],
            session_ctx["session_no"],
            session_ctx["session_id"],
        ),
    )
    return dict(doc) if doc else None


def _discussion_enter_response(user, group_id, session_ctx):
    task_id = session_ctx["task_id"]
    session_no = session_ctx["session_no"]
    group_runtime = enter_group_discussion_stage(session_ctx["session_id"], group_id, user["id"])
    if group_runtime["status"] == "waiting":
        response = _current_document_response(user, session_ctx, group_runtime, waiting=True)
        response["entered"] = True
        return jsonify(response), 200

    doc = get_or_create_document(
        group_id,
        task_id,
        session_no,
        user["id"],
        session_id=session_ctx["session_id"],
    )
    if not doc:
        return _json_err("failed to get/create document", 500)
    perm = get_document_permission(user["id"], doc["id"])
    response = _current_document_response(
        user,
        session_ctx,
        group_runtime,
        document=doc,
        permission=perm,
        waiting=False,
    )
    response["entered"] = True
    return jsonify(response), 200


@app.route("/api/discussion/enter", methods=["POST"])
@login_required("student")
def api_discussion_enter():
    """Mark this student ready for discussion and resolve the group document."""
    user = current_user()
    group_id = get_user_group_id(user["id"])
    if not group_id:
        return _json_err("no group assignment", 403)
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify(_waiting_for_task_response(user)), 200
    if not session_ctx["task_id"]:
        return jsonify(_waiting_for_task_response(user, session_ctx)), 200
    return _discussion_enter_response(user, group_id, session_ctx)


@app.route("/api/collaborative-documents/<int:document_id>/submit/prepare-status", methods=["GET"])
@login_required("student")
def api_collab_document_prepare_status(document_id):
    """Query the status of a prepare operation.
    
    Used for timeout resolution and page re-entry recovery.
    Returns whether a prepare record exists and if it's committed.
    Does NOT check the collaboration server room state.
    """
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)
    from db import query_one
    row = query_one(
        "SELECT freeze_id, state_revision, committed, created_by, created_at "
        "FROM submission_prepares WHERE document_id=? ORDER BY id DESC LIMIT 1",
        (document_id,)
    )
    if not row:
        return jsonify({"prepared": False, "committed": False}), 200
    if row["committed"]:
        return jsonify({
            "prepared": True,
            "committed": True,
            "freeze_id": row["freeze_id"],
            "state_revision": row["state_revision"],
            "created_at": row["created_at"],
        }), 200
    return jsonify({
        "prepared": True,
        "committed": False,
        "freeze_id": row["freeze_id"],
        "state_revision": row["state_revision"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }), 200


@app.route("/api/collaborative-documents/<int:document_id>/submit/commit-status", methods=["GET"])
@login_required("student")
def api_collab_document_commit_status(document_id):
    """Query the status of a commit operation.
    
    Used for timeout resolution and page re-entry recovery.
    Returns whether the document has been submitted.
    """
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)
    from db import query_one
    doc = query_one(
        "SELECT status, submitted_at, state_revision FROM collaborative_documents WHERE id=?",
        (document_id,)
    )
    if not doc:
        return _json_err("not found", 404)
    if doc["status"] == "submitted":
        return jsonify({
            "submitted": True,
            "status": doc["status"],
            "submitted_at": doc["submitted_at"],
            "state_revision": doc["state_revision"],
        }), 200
    return jsonify({
        "submitted": False,
        "status": doc["status"],
    }), 200


@app.route("/api/collaborative-documents/current", methods=["GET"])
@login_required("student")
def api_collab_document_current():
    """Read current discussion/document state without marking ready."""
    user = current_user()
    group_id = get_user_group_id(user["id"])
    if not group_id:
        return _json_err("no group assignment", 403)
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify(_waiting_for_task_response(user)), 200
    task_id = session_ctx["task_id"]
    if not task_id:
        return jsonify(_waiting_for_task_response(user, session_ctx)), 200

    group_runtime = get_group_discussion_runtime(session_ctx["session_id"], group_id)
    if not group_runtime or group_runtime["status"] == "waiting":
        return jsonify(_current_document_response(user, session_ctx, group_runtime, waiting=True)), 200

    doc = _find_current_document(group_id, session_ctx)
    perm = get_document_permission(user["id"], doc["id"]) if doc else None
    return jsonify(_current_document_response(
        user,
        session_ctx,
        group_runtime,
        document=doc,
        permission=perm,
        waiting=False,
    )), 200


@app.route("/api/collaborative-documents/<int:document_id>", methods=["GET"])
@login_required()
def api_collab_document_meta(document_id):
    """Get document metadata."""
    meta = get_document_meta(document_id)
    if not meta:
        return _json_err("not found", 404)
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    perm = get_document_permission(user["id"], document_id)
    if not perm:
        return _json_err("access denied", 403)
    return jsonify({"meta": meta, "permission": perm}), 200


@app.route("/api/collaborative-documents/<int:document_id>/state", methods=["GET"])
@login_required()
def api_collab_document_state(document_id):
    """Get document y_state (binary) and revision info."""
    doc = query_one(
        "SELECT id,y_state,state_revision,state_size_bytes,content_json,content_html,content_text FROM collaborative_documents WHERE id=?",
        (document_id,)
    )
    if not doc:
        return _json_err("not found", 404)
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    if not get_document_permission(user["id"], document_id):
        return _json_err("access denied", 403)
    import base64
    y_b64 = base64.b64encode(doc["y_state"]).decode("ascii") if doc["y_state"] else None
    return jsonify({
        "document_id": doc["id"],
        "y_state_base64": y_b64,
        "state_revision": doc["state_revision"],
        "state_size_bytes": doc["state_size_bytes"],
        "content_json": doc["content_json"],
        "content_html": doc["content_html"],
        "content_text": doc["content_text"],
    }), 200


@app.route("/api/collaborative-documents/<int:document_id>/snapshot", methods=["POST"])
@login_required()
def api_collab_document_snapshot(document_id):
    """Save display snapshot (content fields). Requires edit permission."""
    meta = get_document_meta(document_id)
    if not meta:
        return _json_err("not found", 404)
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    if not can_edit_document(user["id"], document_id):
        return _json_err("access denied", 403)
    if request.content_length and request.content_length > MAX_REQUEST_BODY_BYTES:
        return _json_err("request too large", 413)
    data = request.get_json(force=True) or {}
    content_json = data.get("content_json", "") or ""
    content_html = data.get("content_html", "") or ""
    content_text = data.get("content_text", "") or ""
    # Batch 3: Only save derived data (content_json/html/text).
    # Yjs state is saved exclusively by the WebSocket server.
    # y_state_base64 from the client is no longer accepted to avoid
    # competing writes with the server's periodic save loop.
    try:
        save_snapshot(document_id, content_json, content_html, content_text)
        doc = query_one("SELECT state_revision,updated_at FROM collaborative_documents WHERE id=?", (document_id,))
        return jsonify({"ok": True, "state_revision": doc["state_revision"], "updated_at": doc["updated_at"]}), 200
    except ValueError as e:
        return _json_err(str(e), 413)
    except Exception as e:
        return _json_err("snapshot failed: " + str(e), 500)



@app.route("/api/collaborative-documents/<int:document_id>/submit", methods=["POST"])
@login_required("student")
def api_collab_document_submit(document_id):
    """Submit collaborative document (legacy single-phase, kept for compatibility)."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    from services.collaborative_permissions import can_edit_document
    if not can_edit_document(user["id"], document_id):
        return _json_err("access denied", 403)
    doc = query_one("SELECT * FROM collaborative_documents WHERE id=?", (document_id,))
    if not doc:
        return _json_err("not found", 404)
    if doc["status"] == "submitted":
        return _json_err("already submitted", 409)
    data = request.get_json(force=True) or {}
    content_text = data.get("content_text", doc["content_text"] or "")
    content_html = data.get("content_html", doc["content_html"] or "")
    content_json = data.get("content_json", doc["content_json"] or "")
    from services.collaborative_service import save_snapshot, set_document_status
    try:
        save_snapshot(document_id, content_json, content_html, content_text)
    except ValueError as e:
        return _json_err(str(e), 413)
    from db import create_submission, now_str, execute
    sid = create_submission(
        group_id=doc["group_id"],
        user_id=user["id"],
        content=content_text,
        submission_mode="text",
        task_id=doc["task_id"],
        session_no=doc["session_no"],
    )
    set_document_status(document_id, "submitted", user["id"])
    execute(
        "UPDATE collaborative_documents SET content_json=?,content_html=?,content_text=?,updated_at=? WHERE id=?",
        (content_json, content_html, content_text, now_str(), document_id)
    )
    return jsonify({
        "ok": True,
        "submission_id": sid,
        "content_preview": content_text[:300] if content_text else "",
        "status": "submitted",
    }), 200


@app.route("/api/collaborative-documents/<int:document_id>/submit/prepare", methods=["POST"])
@login_required("student")
def api_collab_document_prepare(document_id):
    """Two-phase commit: prepare (freeze + flush).
    
    1. Validate user and edit permission
    2. Call internal API to freeze the room
    3. Call internal API to flush (get final state_revision)
    4. Generate freeze_id and store prepare record
    5. Return freeze_id and state_revision
    """
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    meta = get_document_meta(document_id)
    if not meta:
        return _json_err("not found", 404)
    if meta["status"] == "submitted":
        return _json_err("already submitted", 409)
    if meta["status"] == "locked":
        return _json_err("document locked", 423)

    if not can_edit_document(user["id"], document_id):
        return _json_err("access denied", 403)
    from services.collaborative_internal import freeze_document, flush_document, unfreeze_document

    # Step 1: Freeze the room
    freeze_result = freeze_document(document_id)
    if not freeze_result.get("ok"):
        return _json_err("freeze failed: " + freeze_result.get("error", "unknown"), 500)

    # Step 2: Flush to get final state_revision
    flush_result = flush_document(document_id)
    if not flush_result.get("ok"):
        # Try to unfreeze on flush failure
        unfreeze_document(document_id)
        return _json_err("flush failed: " + flush_result.get("error", "unknown"), 500)

    state_revision = flush_result["state_revision"]

    # Step 3: Create prepare record
    from services.collaborative_service import create_prepare, cleanup_old_prepares
    cleanup_old_prepares(document_id)  # Clean old/failed prepares
    prepare = create_prepare(document_id, state_revision, user["id"])
    if not prepare:
        unfreeze_document(document_id)
        return _json_err("prepare record failed", 500)

    return jsonify({
        "ok": True,
        "freeze_id": prepare["freeze_id"],
        "state_revision": prepare["state_revision"],
        "document_id": document_id,
    }), 200


@app.route("/api/collaborative-documents/<int:document_id>/submit/commit", methods=["POST"])
@login_required("student")
def api_collab_document_commit(document_id):
    """Two-phase commit: commit (submit and close)."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)

    data = request.get_json(force=True) or {}
    freeze_id = data.get("freeze_id", "")
    client_state_revision = data.get("state_revision", 0)
    content_text = data.get("content_text", "")
    content_html = data.get("content_html", "")
    content_json = data.get("content_json", "")

    doc = query_one("SELECT * FROM collaborative_documents WHERE id=?", (document_id,))
    if not doc:
        return _json_err("not found", 404)
    if doc["status"] == "submitted":
        return _json_err("already submitted", 409)

    if not can_edit_document(user["id"], document_id):
        return _json_err("access denied", 403)
    from services.collaborative_service import verify_prepare, mark_prepare_committed, cleanup_old_prepares

    prepare_info = verify_prepare(document_id, freeze_id, client_state_revision, user["id"])
    if not prepare_info:
        return _json_err("invalid freeze_id or state_revision mismatch", 409)

    from db import create_submission, now_str, execute
    from services.collaborative_service import set_document_status
    from services.collaborative_internal import close_document, unfreeze_document

    # Use execute() for each write (separate connections, no long-lived transaction lock)
    # Each execute() call opens a connection, writes, commits, and closes.
    try:
        from db import execute
        now = now_str()
        
        now = now_str()
        
        # 1. Update document content and status
        execute(
            "UPDATE collaborative_documents SET content_json=?, content_html=?, content_text=?, "
            "status='submitted', submitted_at=CASE WHEN submitted_at IS NULL THEN ? ELSE submitted_at END, "
            "updated_at=? WHERE id=? AND status!='submitted'",
            (content_json, content_html, content_text, now, now, document_id)
        )
        
        # 2. Get updated doc for checkpoint
        updated_doc = query_one(
            "SELECT * FROM collaborative_documents WHERE id=?",
            (document_id,)
        )
        
        if not updated_doc or updated_doc["status"] != "submitted":
            unfreeze_document(document_id)
            return _json_err("document not in submitted state after update", 500)
        
        # 3. Create submitted checkpoint with y_state
        y_state_to_store = doc["y_state"]
        execute(
            "INSERT INTO collaborative_document_checkpoints(document_id, state_revision, reason, "
            "y_state, content_json, content_html, content_text, created_by, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (document_id, updated_doc["state_revision"], "submitted",
             y_state_to_store, content_json, content_html, content_text, user["id"], now)
        )
        
        # 4. Create submission record
        from db import record_process_event
        sid = execute(
            "INSERT INTO submissions(group_id, user_id, task_id, session_no, content, "
            "file_name, stored_file_name, file_path, file_size, submission_mode, "
            "submitted_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc["group_id"], user["id"], doc["task_id"], doc["session_no"], content_text,
             None, None, None, 0, "text", now, now)
        )
        if not sid:
            raise Exception("submission creation returned no ID")
        
        # 5. Mark prepare as committed
        execute(
            "UPDATE submission_prepares SET committed=1 WHERE document_id=? AND freeze_id=?",
            (document_id, freeze_id)
        )
        
        # 6. Record process event
        record_process_event(
            "submission",
            group_id=doc["group_id"],
            user_id=user["id"],
            related_table="submissions",
            related_id=sid,
            payload={"task_id": doc["task_id"], "session_no": doc["session_no"], "mode": "text"},
        )
        
        # 7. Close room on success (best-effort)
        close_document(document_id)

        try:
            from services.collaboration_state_finalization_service import safe_request_finalization_for_document

            safe_request_finalization_for_document(document_id, "student_submit")
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to request state finalization for submitted document %s",
                document_id,
                exc_info=True,
            )
        
        return jsonify({
            "ok": True,
            "submission_id": sid,
            "content_preview": content_text[:300] if content_text else "",
            "status": "submitted",
        }), 200
        
    except Exception as e:
        # Since we use execute() (auto-commit per call), we can"t rollback.
        # But the status is only changed in the first execute() call.
        # Handle recovery for specific known failure modes.
        unfreeze_document(document_id)
        return _json_err("commit failed: " + str(e), 500)
@app.route("/api/collaborative-documents/<int:document_id>/submit/unfreeze", methods=["POST"])
@login_required("student")
def api_collab_document_unfreeze(document_id):
    """Unfreeze a document after failed commit."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    meta = get_document_meta(document_id)
    if not meta:
        return _json_err("not found", 404)
    if meta["status"] == "submitted":
        return _json_err("already submitted", 409)
    from services.collaborative_permissions import can_edit_document
    if not can_edit_document(user["id"], document_id):
        return _json_err("access denied", 403)
    from services.collaborative_service import cleanup_old_prepares
    from services.collaborative_internal import unfreeze_document
    cleanup_old_prepares(document_id)
    result = unfreeze_document(document_id)
    return jsonify({
        "ok": result.get("ok", False),
        "state": result.get("state", "unknown"),
        "document_id": document_id,
    }), 200


@app.route("/api/collaborative-documents/<int:document_id>/submit/auto-timeout", methods=["POST"])
@login_required("student")
def api_collab_document_auto_timeout(document_id):
    """Auto-submit one document after its group discussion runtime times out."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)

    doc = query_one(
        "SELECT id, session_id, group_id, task_id, session_no, status FROM collaborative_documents WHERE id=?",
        (document_id,),
    )
    if not doc:
        return _json_err("not found", 404)
    if doc["status"] == "submitted":
        return jsonify({"submitted": True, "status": "submitted", "already_submitted": True}), 200

    session_id = doc["session_id"]
    if not session_id:
        session = query_one(
            "SELECT id FROM experiment_sessions WHERE task_id=? AND session_no=? ORDER BY id DESC LIMIT 1",
            (doc["task_id"], doc["session_no"]),
        )
        session_id = session["id"] if session else None
    if not session_id:
        return _json_err("missing group discussion session", 409)

    runtime = get_group_discussion_runtime(session_id, doc["group_id"])
    if not runtime:
        return _json_err("missing group discussion runtime", 409)
    if not is_group_discussion_write_closed(session_id, doc["group_id"]):
        return jsonify({
            "submitted": False,
            "reason": "deadline_not_reached",
            **group_discussion_timer_payload(runtime),
        }), 409

    data = request.get_json(silent=True) or {}
    final_content = {
        key: data[key]
        for key in ("content_json", "content_html", "content_text")
        if key in data
    }
    from services.auto_submit_service import perform_auto_submit
    result = perform_auto_submit(document_id, {
        "session_id": session_id,
        "group_discussion_id": runtime.get("id"),
        "group_id": doc["group_id"],
        "task_id": doc["task_id"],
        "session_no": doc["session_no"],
        "deadline": runtime.get("deadline"),
    }, final_content=final_content)
    if result.get("ok"):
        return jsonify({
            "submitted": True,
            "status": "submitted",
            **group_discussion_timer_payload(runtime),
            **result,
        }), 200
    if result.get("reason") == "already_submitted":
        return jsonify({"submitted": True, "status": "submitted", "already_submitted": True}), 200
    return jsonify({"submitted": False, **result}), 409


@app.route("/api/collaborative-documents/<int:document_id>/return", methods=["POST"])
@login_required("teacher")
def api_collab_document_return(document_id):
    """Teacher returns a submitted document for revision.
    
    1. Verify teacher has view permission
    2. Get current document state
    3. Create returned checkpoint
    4. Set status=returned
    5. Record return event
    """
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    
    data = request.get_json(force=True) or {}
    reason = data.get("reason", "")
    
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)
    
    doc = query_one("SELECT * FROM collaborative_documents WHERE id=?", (document_id,))
    if not doc:
        return _json_err("not found", 404)
    # Allow returning from editing (accidental return) or submitted
    if doc["status"] not in ("submitted", "editing"):
        return _json_err("document is not in a returnable state", 409)
    
    from services.collaborative_service import set_document_status
    from db import now_str
    
    # Check if room exists and unfreeze if so (to allow editing)
    from services.collaborative_internal import get_document_status as room_status
    status_info = room_status(document_id)
    if status_info.get("ok") and status_info.get("active"):
        from services.collaborative_internal import unfreeze_document
        unfreeze_document(document_id)
    
    # Set status to returned (creates returned checkpoint)
    result = set_document_status(document_id, "returned", user["id"])
    if not result:
        return _json_err("failed to update status", 500)
    
    # Record return event
    from db import record_process_event
    record_process_event(
        "teacher_return",
        group_id=doc["group_id"],
        user_id=user["id"],
        related_table="collaborative_documents",
        related_id=document_id,
        payload={"reason": reason},
    )
    
    return jsonify({
        "ok": True,
        "status": "returned",
        "document_id": document_id,
        "reason": reason,
    }), 200


@app.route("/api/collaborative-documents/<int:document_id>/teacher-state", methods=["GET"])
@login_required("teacher")
def api_collab_document_teacher_state(document_id):
    """Teacher read-only document state (no WebSocket needed).
    
    Returns y_state + metadata for read-only viewing.
    """
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)
    
    doc = query_one(
        "SELECT id, group_id, status, state_revision, state_size_bytes, "
        "y_state, content_json, content_html, content_text, "
        "submitted_at, created_at, updated_at "
        "FROM collaborative_documents WHERE id=?",
        (document_id,)
    )
    if not doc:
        return _json_err("not found", 404)
    
    import base64
    y_b64 = base64.b64encode(doc["y_state"]).decode("ascii") if doc["y_state"] else None
    
    # Get group info
    group = query_one("SELECT name, group_code FROM groups WHERE id=?", (doc["group_id"],))
    
    return jsonify({
        "document_id": doc["id"],
        "group_id": doc["group_id"],
        "group_name": group["name"] if group else "",
        "group_code": group["group_code"] if group else "",
        "status": doc["status"],
        "state_revision": doc["state_revision"],
        "state_size_bytes": doc["state_size_bytes"],
        "y_state_base64": y_b64,
        "content_json": doc["content_json"],
        "content_html": doc["content_html"],
        "content_text": doc["content_text"],
        "submitted_at": doc["submitted_at"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }), 200



@app.route("/api/collaborative-documents/<int:document_id>/teacher-ticket", methods=["POST"])
@login_required("teacher")
def api_collab_document_teacher_ticket(document_id):
    """Issue a view-only collaboration ticket for teacher WebSocket connection."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    from services.collaborative_permissions import can_view_document
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)

    meta = get_document_meta(document_id)
    if not meta:
        return _json_err("not found", 404)

    from core import app as flask_app
    secret_key = flask_app.secret_key
    from services.collaborative_token import issue_ticket
    from config import COLLAB_WS_EXTERNAL_URL, COLLAB_TOKEN_TTL

    token = issue_ticket(user["id"], document_id, meta["group_id"], "view", secret_key, display_name=user.get("display_name"))
    import logging
    _logger = logging.getLogger(__name__)
    from services.collaboration_secret import secret_fingerprint
    _secret_fp = secret_fingerprint(secret_key)
    _logger.info("[collab-ticket] user_id=%s doc_id=%s perm=%s ws_url=%s secret_fp=%s token_len=%s",
                 user["id"], document_id, "view", COLLAB_WS_EXTERNAL_URL,
                 _secret_fp, len(token))
    return jsonify({
        "token": token,
        "document_id": document_id,
        "group_id": meta["group_id"],
        "permission": "view",
        "ttl_seconds": COLLAB_TOKEN_TTL,
        "ws_url": COLLAB_WS_EXTERNAL_URL,
    }), 200

@app.route("/api/collaborative-documents/<int:document_id>/checkpoints", methods=["GET"])
@login_required()
def api_collab_document_checkpoints(document_id):
    """Get checkpoints for a document."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    from services.collaborative_permissions import can_view_document
    if not can_view_document(user["id"], document_id):
        return _json_err("access denied", 403)
    
    reason = request.args.get("reason")
    limit = int(request.args.get("limit", "50"))
    
    from services.collaborative_service import get_checkpoints
    checkpoints = get_checkpoints(document_id, limit=limit, reason=reason)
    
    return jsonify({
        "ok": True,
        "checkpoints": checkpoints,
        "count": len(checkpoints),
    }), 200

@app.route("/api/collaborative-documents/<int:document_id>/ticket", methods=["POST"])
@login_required()
def api_collab_document_ticket(document_id):
    """Issue a short-term collaboration ticket."""
    user = current_user()
    if not user:
        return _json_err("unauthorized", 401)
    perm = get_document_permission(user["id"], document_id)
    if not perm:
        return _json_err("access denied", 403)
    if perm != "edit":
        return _json_err("insufficient permission", 403)
    meta = get_document_meta(document_id)
    if not meta:
        return _json_err("not found", 404)
    from core import app as flask_app
    secret_key = flask_app.secret_key
    from services.collaborative_token import issue_ticket
    from config import COLLAB_WS_EXTERNAL_URL, COLLAB_TOKEN_TTL
    ttl = COLLAB_TOKEN_TTL
    token = issue_ticket(user["id"], document_id, meta["group_id"], "edit", secret_key, participant_code=user.get("participant_code"), display_name=user.get("display_name"))
    import logging
    _logger = logging.getLogger(__name__)
    from services.collaboration_secret import secret_fingerprint
    _secret_fp = secret_fingerprint(secret_key)
    _logger.info("[collab-ticket] user_id=%s doc_id=%s perm=%s ws_url=%s secret_fp=%s token_len=%s",
                 user["id"], document_id, "edit", COLLAB_WS_EXTERNAL_URL,
                 _secret_fp, len(token))
    return jsonify({
        "token": token,
        "document_id": document_id,
        "group_id": meta["group_id"],
        "permission": "edit",
        "ttl_seconds": ttl,
        "ws_url": COLLAB_WS_EXTERNAL_URL,
    }), 200
