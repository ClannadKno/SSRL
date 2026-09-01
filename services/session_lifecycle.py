# -*- coding: utf-8 -*-
"""Unified session lifecycle & context helpers for SSRL-ESP multi-session isolation.
Provides:
- ``check_agent_allowed`` -- used by agent entry points to gate intervention.
- ``get_session_context`` -- full session context with writability flags.
- ``get_student_accessible_session_context`` -- student-facing session context.
- ``is_session_running`` / ``is_session_expired`` -- session state predicates.
- ``is_discussion_writable`` / ``is_help_request_allowed`` / ``is_document_writable``
  -- unified writability checks that depend on session status before group state.
Usage::
    allowed, reason = check_agent_allowed(group_id, session_id, ...)
    if not allowed:
        logger.info("Agent blocked: %s", reason)
        return {"status": "skipped", "reason": reason}
    ctx = get_session_context(session_id, group_id=group_id)
    if ctx and ctx["discussion_writable"]:
        # allow writing
        ...
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from db import (
    execute,
    query_one,
    query_all,
    now_str,
    parse_dt,
    get_current_running_session_context as _db_get_current_running_session_context,
)
logger = logging.getLogger(__name__)
# =====================================================================
# Internal helpers
# =====================================================================
def _get_session_row(session_id):
    """Fetch a single experiment_sessions row as dict, or None."""
    row = query_one("SELECT * FROM experiment_sessions WHERE id=?", (session_id,))
    return dict(row) if row else None
def _is_deadline_passed(session: dict, now_dt: Optional[datetime] = None) -> bool:
    """Legacy compatibility shim.

    Discussion timeout is now owned by group_session_discussions, not by
    experiment_sessions. Until the group runtime service is wired in, a session
    row's deadline/time_limit must not lock discussion, help, documents, agents,
    or close the teacher-controlled session.
    """
    return False
def _is_group_closed(group_id: int) -> bool:
    """Check if a group's state is CLOSED (backward-compat check)."""
    row = query_one("SELECT state FROM groups WHERE id=?", (group_id,))
    return bool(row and row["state"] == "CLOSED")


def _is_group_discussion_write_closed(
    session_id: Optional[int],
    group_id: Optional[int],
    now_dt: Optional[datetime] = None,
) -> bool:
    if not session_id or not group_id:
        return False
    try:
        from services.group_discussion_runtime_service import is_group_discussion_write_closed
        return is_group_discussion_write_closed(session_id, group_id, now_dt=now_dt)
    except Exception as exc:
        logger.warning("group discussion write check failed session=%s group=%s: %s", session_id, group_id, exc)
        return False
def _format_session_name(session: dict) -> str:
    """Produce a human-readable session name from a session row dict."""
    title = (session.get("title") or "").strip()
    if title:
        return title
    session_no = session.get("session_no", "")
    return f"第{session_no}课次"
# =====================================================================
# Writable helpers
# =====================================================================
def is_session_running(session_id: int) -> bool:
    """Return True if the session status is 'running'."""
    row = query_one("SELECT status FROM experiment_sessions WHERE id=?", (session_id,))
    return bool(row and row["status"] == "running")
def is_session_expired(session_id: int, now_dt: Optional[datetime] = None) -> bool:
    """Return whether the teacher-controlled session is expired.

    Session-level expiration is disabled. Discussion expiration will be
    evaluated by group discussion runtime state in later batches.
    """
    row = _get_session_row(session_id)
    if not row:
        return False
    return _is_deadline_passed(row, now_dt)
def is_discussion_writable(
    session_id: Optional[int],
    group_id: int,
    user_id: Optional[int] = None,
) -> bool:
    """Check whether the discussion area is writable for this group/session.
    **Primary** -- session must be ``running``.
    **Secondary** -- group.state is checked only when no running session exists
    (backward compatibility).  A new running session overrides a stale
    ``CLOSED`` group state.
    """
    if not group_id:
        return False
    if not session_id:
        # No session -> fall back to legacy group.state check
        return not _is_group_closed(group_id)
    session = _get_session_row(session_id)
    if not session:
        return not _is_group_closed(group_id)
    if session["status"] != "running":
        return False
    return not _is_group_discussion_write_closed(session_id, group_id)
def is_help_request_allowed(
    session_id: Optional[int],
    group_id: int,
    user_id: Optional[int] = None,
) -> bool:
    """Check whether a help request may be submitted.
    Same logic as :func:`is_discussion_writable` -- session must be
    ``running``. Group-level timeout is enforced separately.
    """
    return is_discussion_writable(session_id, group_id, user_id)
def is_document_writable(
    session_id: Optional[int],
    group_id: int,
    document_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> bool:
    """Check whether the collaborative document is writable.
    In addition to the session ``running`` check, the
    document status must be ``editing`` or ``returned``.
    When ``document_id`` is not provided the **latest** document for
    this group is inspected.
    """
    if not group_id:
        return False
    # Session-level check first
    if session_id:
        session = _get_session_row(session_id)
        if not session or session["status"] != "running":
            return False
    else:
        # No session provided -> not writable
        return False
    # Document-level check
    if document_id:
        doc = query_one(
            "SELECT status FROM collaborative_documents WHERE id=?",
            (document_id,),
        )
    else:
        doc = query_one(
            "SELECT status FROM collaborative_documents "
            "WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (group_id,),
        )
    if not doc:
        # No document yet -> can be created, treat as writable
        return True
    writable_statuses = {"editing", "returned"}
    return doc["status"] in writable_statuses
# =====================================================================
# Session context builders
# =====================================================================
def get_session_context(
    session_id: int,
    group_id: Optional[int] = None,
    now_dt: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the unified session context dict for *session_id*.
    When ``group_id`` is provided the writability flags (discussion,
    help, document) are scoped to that group.  Without ``group_id``
    the writable flags default to the session status alone.
    Returns ``None`` when the session does not exist.
    """
    session = _get_session_row(session_id)
    if not session:
        return None
    _now = now_dt if now_dt else datetime.now()
    is_running = session["status"] == "running"
    is_expired = _is_deadline_passed(session, _now)
    posttest_available = session["status"] in ("ended", "archived")
    if group_id:
        discussion_writable = is_discussion_writable(session_id, group_id)
        help_writable = is_help_request_allowed(session_id, group_id)
        document_writable = is_document_writable(session_id, group_id)
    else:
        discussion_writable = is_running
        help_writable = is_running
        document_writable = is_running
    return {
        "session_id": session["id"],
        "session_name": _format_session_name(session),
        "session_no": session["session_no"],
        "status": session["status"],
        "task_id": session.get("task_id"),
        "group_id": group_id,
        "deadline": None,
        "server_time": now_str(),
        "is_running": is_running,
        "is_expired": is_expired,
        "discussion_writable": discussion_writable,
        "help_writable": help_writable,
        "document_writable": document_writable,
        "posttest_available": posttest_available,
    }
def get_student_accessible_session_context(
    user_id: int,
    now_dt: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the session context visible to a student.

    First tries the currently running session. If none is running, falls
    back to the most recently ended/archived session for the student's group
    (searched via messages AND collaborative_documents tables).
    This allows posttest to bind to the just-ended session.
    Returns `None` if the student has no group or no session exists.
    """
    member = query_one(
        "SELECT group_id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if not member:
        return None
    _group_id = member["group_id"]
    ctx = _db_get_current_running_session_context()
    if ctx:
        session_id = ctx.get("session_id")
        if session_id:
            return get_session_context(session_id, group_id=_group_id, now_dt=now_dt)
    # No running session -> fall back to most recent ended session for this group
    try:
        last_end = query_one(
            """SELECT id FROM experiment_sessions
                WHERE id IN (
                  SELECT DISTINCT session_id FROM messages WHERE group_id=?
                  UNION
                  SELECT DISTINCT session_id FROM collaborative_documents WHERE group_id=?
                )
                AND status IN ('ended','archived')
                ORDER BY end_time DESC, id DESC LIMIT 1""",
            (_group_id, _group_id,),
        )
        if last_end:
            last_session = get_session_context(last_end["id"], group_id=_group_id, now_dt=now_dt)
            if last_session:
                last_session["posttest_available"] = True
                return last_session
    except Exception:
        pass
    return None

def get_current_running_session_context():
    """Wrapper for db.get_current_running_session_context."""
    return _db_get_current_running_session_context()
# =====================================================================
# Agent gating (existing, unchanged signature)
# =====================================================================
def check_agent_allowed(
    group_id: int,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    session_no: Optional[int] = None,
    now: Optional[datetime] = None,
    agent_type: Optional[str] = None,
) -> Tuple[bool, str]:
    """Check whether the session is still in a state that permits agent intervention.
    Returns ``(True, "active")`` if allowed, or ``(False, <reason>)``
    with one of the following reasons:
    - ``session_not_found`` -- the experiment session does not exist.
    - ``session_not_active`` -- session status is not ``running``.
    - ``document_submitted`` -- the group's collaborative document already has
      a non-NULL ``submitted_at``.
    - ``document_locked``    -- the document ``status`` is one of
      ``submitted`` / ``locked`` / ``frozen`` / ``closed`` / ``submitting``.
    - ``agent_disabled``     -- the relevant agent toggle is off for this session.
    - ``group_closed``       -- the group state is ``CLOSED``.
    - ``agent_paused``       -- the group-level agent pause flag is set.
    """
    now_str_val = now_str() if now is None else now.strftime("%Y-%m-%d %H:%M:%S")
    now_dt = now if now else datetime.now()
    # ---- 1. Resolve session if not provided ----
    if not session_id or not task_id or session_no is None:
        ctx = _resolve_session_context(group_id)
        if not ctx:
            pass
        else:
            session_id = session_id or ctx.get("session_id") or ctx.get("id")
            task_id = task_id or ctx.get("task_id")
            session_no = session_no if session_no is not None else ctx.get("session_no")
    # ---- 2. Check session exists and is active ----
    if session_id:
        session = _get_session_row(session_id)
        if not session:
            return False, "session_not_found"
        if session["status"] != "running":
            return False, "session_not_active"
        # ---- 3. Check canonical Agent mode ----
        from services.agent_mode_service import agent_config_from_session
        agent_config = agent_config_from_session(session)
        if agent_config.get("configuration_error"):
            return False, "invalid_agent_configuration"
        if agent_type == "emotion":
            if agent_config.get("agent_mode") != "emotion":
                return False, "agent_disabled"
        elif agent_type == "strategy":
            if agent_config.get("agent_mode") != "strategy":
                return False, "agent_disabled"
    # ---- 5. Check group state ----
    group = query_one("SELECT id, state, auto_intervention_enabled FROM groups WHERE id=?", (group_id,))
    if not group:
        return False, "group_not_found"
    if group["state"] == "CLOSED":
        return False, "group_closed"
    # ---- 6. Check group-level agent pause ----
    if session_id:
        control = query_one(
            "SELECT agent_paused FROM group_session_controls "
            "WHERE group_id=? AND session_id=?",
            (group_id, session_id),
        )
        if control and bool(control.get("agent_paused", False)):
            return False, "agent_paused"
    # ---- 7. Check collaborative document status ----
    if task_id and session_no is not None:
        doc = query_one(
            "SELECT id, status, submitted_at FROM collaborative_documents "
            "WHERE group_id=? AND task_id=? AND session_no=? "
            "ORDER BY id DESC LIMIT 1",
            (group_id, task_id, session_no),
        )
    else:
        doc = query_one(
            "SELECT id, status, submitted_at FROM collaborative_documents "
            "WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (group_id,),
        )
    if doc:
        if doc["submitted_at"] is not None:
            return False, "document_submitted"
        locked_statuses = {"submitted", "locked", "frozen", "closed", "submitting"}
        if doc["status"] in locked_statuses:
            return False, "document_locked"
    if session_id and _is_group_discussion_write_closed(session_id, group_id, now_dt=now_dt):
        return False, "group_discussion_closed"
    # ---- All checks passed ----
    return True, "active"
def _resolve_session_context(group_id: int) -> Optional[dict]:
    """Try to find the current running session for this group.
    First checks the DB-level "current running session" helper,
    then falls back to looking up the most recent running session
    linked to this group.
    """
    try:
        ctx = get_current_running_session_context()
        if ctx:
            return ctx
    except Exception:
        pass
    try:
        from db import get_current_running_session_context
        ctx = get_current_running_session_context()
        return ctx
    except Exception:
        pass
    return None



# =====================================================================
# Unified teacher-controlled session close logic
# =====================================================================


def close_session(
    session_id: int,
    reason: str = "manual",
    operator_id: Optional[int] = None,
) -> dict:
    """Unified session close function.

    Used by teacher/admin close paths. The function:
    1. Loads the session; raises ``ValueError`` if missing or already ended.
    2. Atomically sets ``status='ended'``, ``end_time=now``.
    3. Clears the ``current_session_id`` setting **only** if this session
       is still the active one (so that a new session can start cleanly).
    4. Writes an audit log.
    5. Returns the updated session dict.

    Notes:
    - Do **not** clear ``current_session_no`` or ``current_task_id`` because
      the post-test questionnaire needs the session context to bind responses.
    - Do **not** set ``group.state='CLOSED'`` so that the next session is not
      affected by a stale ``CLOSED`` state.
    - The function is **idempotent**: calling it a second time on an already
      closed session returns the existing ended-session dict without writing.
    """
    session = _get_session_row(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    # Idempotency: already ended/archived -> return as-is
    if session["status"] in ("ended", "archived"):
        logger.info("close_session(%s) idempotent skip: status=%s", session_id, session["status"])
        return session

    try:
        from services.auto_submit_service import submit_session_documents

        document_submission = submit_session_documents(session_id, operator_id=operator_id)
    except Exception as exc:
        logger.exception("close_session(%s) document submission failed", session_id)
        document_submission = {
            "ok": False,
            "session_id": session_id,
            "documents_found": 0,
            "submitted": 0,
            "errors": 1,
            "error": str(exc),
        }
    if not document_submission.get("ok"):
        raise ValueError(
            "COLLABORATIVE_DOCUMENT_SUBMISSION_FAILED: "
            f"{document_submission.get('errors', 1)} document(s) could not be finalized"
        )

    old_active_id = None
    try:
        ctx = _db_get_current_running_session_context()
        old_active_id = ctx.get("session_id") if ctx else None
    except Exception:
        pass

    now = now_str()
    execute(
        "UPDATE experiment_sessions SET status='ended', end_time=?, updated_at=? WHERE id=?",
        (now, now, session_id),
    )

    # Clear current_session_id setting only if this session was the active one
    if old_active_id == session_id:
        from db import set_setting
        set_setting("current_session_id", "")
        logger.info("close_session(%s): cleared current_session_id", session_id)

    try:
        from services.three_stage_coordination import cancel_active_runs_for_session_end

        cancelled = cancel_active_runs_for_session_end(session_id)
        if cancelled:
            logger.info(
                "close_session(%s): cancelled active strategy runs %s",
                session_id,
                cancelled,
            )
    except Exception as exc:
        logger.warning("close_session(%s) strategy cancellation failed: %s", session_id, exc)

    try:
        from services.three_stage_observation import mark_session_observations_finalization_only

        finalized_observations = mark_session_observations_finalization_only(
            session_id,
            reason=reason or "session_end",
        )
        if finalized_observations.get("updated"):
            logger.info(
                "close_session(%s): finalized post-intervention observations %s",
                session_id,
                finalized_observations,
            )
    except Exception as exc:
        logger.warning(
            "close_session(%s) post-intervention observation finalization failed: %s",
            session_id,
            exc,
        )

    # Audit
    try:
        audit_reason = {
            "manual": f"Teacher end session {session_id}",
            "timeout": f"Deprecated timeout close requested for session {session_id}",
        }.get(reason, f"End session {session_id} (reason={reason})")
        from db import write_audit_log
        write_audit_log(
            operator_id=operator_id or 0,
            action_type="session.end",
            target_type="experiment_session",
            target_id=session_id,
            before_value=session["status"],
            after_value="ended",
            reason=audit_reason,
        )
    except Exception as exc:
        logger.warning("close_session(%s) audit log failed: %s", session_id, exc)

    try:
        from services.collaboration_state_finalization_service import safe_request_finalization_for_session_groups

        safe_request_finalization_for_session_groups(session_id, "session_end")
    except Exception as exc:
        logger.warning("close_session(%s) state finalization request failed: %s", session_id, exc)

    logger.info("close_session(%s) done reason=%s", session_id, reason)
    result = _get_session_row(session_id)
    result["document_submission"] = document_submission
    return result


def end_session(
    session_id: int,
    reason: str = "manual",
    operator_id: Optional[int] = None,
) -> dict:
    """Convenience alias for ``close_session(session_id, reason='manual')``.
    Maintains backward compatibility with ``teacher_session_service.end_session``.
    """
    return close_session(session_id, reason=reason, operator_id=operator_id)


def auto_end_expired_sessions(max_sessions: int = 10) -> dict:
    """Compatibility no-op for the removed session-level timeout scanner."""
    return {
        "scanned": 0,
        "ended": 0,
        "submitted_docs": 0,
        "errors": [],
        "disabled": "session_timeout_removed",
    }


def is_help_request_allowed(
    session_id: Optional[int],
    group_id: int,
    user_id: Optional[int] = None,
) -> bool:
    """Check whether a help request may be submitted.
    Same logic as :func:`is_discussion_writable` -- session must be
    ``running``. Group-level timeout is enforced separately.
    """
    return is_discussion_writable(session_id, group_id, user_id)


def is_document_writable(
    session_id: Optional[int],
    group_id: int,
    document_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> bool:
    """Check whether the collaborative document is writable.
    In addition to the session ``running`` check, the
    document status must be ``editing`` or ``returned``.
    When ``document_id`` is not provided the **latest** document for
    this group is inspected.
    """
    if not group_id:
        return False

    # Session-level check first
    if session_id:
        session = _get_session_row(session_id)
        if not session or session["status"] != "running":
            return False
    else:
        return False

    if _is_group_discussion_write_closed(session_id, group_id):
        return False

    # Document-level check
    if document_id:
        doc = query_one(
            "SELECT status FROM collaborative_documents WHERE id=?",
            (document_id,),
        )
    else:
        doc = query_one(
            "SELECT status FROM collaborative_documents "
            "WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (group_id,),
        )
    if not doc:
        return True
    writable_statuses = {"editing", "returned"}
    return doc["status"] in writable_statuses

