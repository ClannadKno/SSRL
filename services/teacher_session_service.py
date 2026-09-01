# -*- coding: utf-8 -*-
"""
Teacher session service - experimental session state machine.

Handles CRUD + state transitions for experiment_sessions, derives
agent flags from session_role, enforces condition_frozen checks,
and writes audit_logs for every control operation.
"""
import json
from datetime import datetime

from db import (
    db, execute, now_str, query_all, query_one, get_setting, set_setting,
    write_audit_log, get_learning_task,
    get_active_experiment_session, get_active_session_id,
    delete_questionnaire_publication, expand_questionnaire_set_for_session,
)
from services.agent_mode_service import (
    INVALID_AGENT_CONFIGURATION,
    InvalidAgentConfiguration,
    agent_config_from_session,
    agent_mode_from_legacy_flags,
    flags_for_agent_mode,
    resolve_session_agent_mode,
    validate_agent_mode,
)


VALID_SESSION_ROLES = frozenset()  # deprecated, kept for compat

VALID_TRANSITIONS = {
    "draft": frozenset({"running"}),
    "running": frozenset({"ended"}),
    "ended": frozenset({"archived"}),
    "archived": frozenset(),
}

_UNSET = object()


def _current_mode_for_audit(session):
    if session and session.get("agent_configuration_error"):
        return INVALID_AGENT_CONFIGURATION
    try:
        return resolve_session_agent_mode(session)
    except InvalidAgentConfiguration:
        return INVALID_AGENT_CONFIGURATION


def _requested_agent_mode(
    *,
    agent_mode=None,
    strategy_agent_enabled=None,
    emotion_agent_enabled=None,
    current_session=None,
    default_mode="strategy",
):
    """Resolve new and legacy API inputs to exactly one canonical mode."""
    legacy_supplied = (
        strategy_agent_enabled is not None or emotion_agent_enabled is not None
    )
    if agent_mode is not None:
        if legacy_supplied:
            raise ValueError(
                "agent_mode cannot be combined with legacy Agent switches"
            )
        return validate_agent_mode(agent_mode)
    if not legacy_supplied:
        if current_session is not None:
            try:
                return resolve_session_agent_mode(current_session)
            except InvalidAgentConfiguration as exc:
                raise ValueError(
                    f"{INVALID_AGENT_CONFIGURATION}: set agent_mode explicitly"
                ) from exc
        return validate_agent_mode(default_mode)

    if current_session is not None:
        try:
            current_mode = resolve_session_agent_mode(current_session)
        except InvalidAgentConfiguration as exc:
            raise ValueError(
                f"{INVALID_AGENT_CONFIGURATION}: set agent_mode explicitly"
            ) from exc
        current_flags = flags_for_agent_mode(current_mode)
        strategy = current_flags["strategy_agent_enabled"]
        emotion = current_flags["emotion_agent_enabled"]
    else:
        strategy = False
        emotion = False
    if strategy_agent_enabled is not None:
        strategy = bool(strategy_agent_enabled)
    if emotion_agent_enabled is not None:
        emotion = bool(emotion_agent_enabled)
    return agent_mode_from_legacy_flags(strategy, emotion)


def _session_payload(row):
    if not row:
        return None
    payload = dict(row)
    config = agent_config_from_session(payload)
    payload.update(config)
    payload["agent_configuration_error"] = config.get("configuration_error")
    return payload


def _normalize_session_no(session_no):
    try:
        value = int(session_no)
    except (TypeError, ValueError):
        raise ValueError("session_no must be an integer")
    if value <= 0:
        raise ValueError("session_no must be a positive integer")
    return value


def _session_no_conflicts(session_no, exclude_session_id=None):
    params = [int(session_no)]
    extra = ""
    if exclude_session_id is not None:
        extra = " AND id!=?"
        params.append(int(exclude_session_id))
    rows = query_all(
        "SELECT id, session_no, status, title FROM experiment_sessions "
        "WHERE session_no=?" + extra + " ORDER BY id ASC",
        tuple(params),
    )
    return [dict(row) for row in rows]


def _ensure_unique_session_no(session_no, exclude_session_id=None):
    conflicts = _session_no_conflicts(session_no, exclude_session_id=exclude_session_id)
    if conflicts:
        ids = ", ".join(str(row["id"]) for row in conflicts)
        raise ValueError(f"课时 {session_no} 已存在（课次ID: {ids}），课时编号必须唯一")


def _get_task_time_limit(task_id):
    if not task_id:
        return None
    task = get_learning_task(int(task_id))
    if not task:
        return None
    try:
        limit = int(task.get("time_limit_minutes") or 0)
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


def _normalize_optional_int(value, field_name):
    if value is _UNSET:
        return _UNSET
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")


def _validate_questionnaire_set(set_id):
    if set_id is None:
        return
    qset = query_one("SELECT id FROM questionnaire_sets WHERE id=? AND active=1", (set_id,))
    if not qset:
        raise ValueError(f"Questionnaire set {set_id} not found or inactive")


def _sync_questionnaire_set_publications(session_id, questionnaire_set_id):
    if questionnaire_set_id is None:
        stale_rows = query_all(
            "SELECT id FROM questionnaire_publications "
            "WHERE session_id=? AND questionnaire_set_id IS NOT NULL",
            (session_id,),
        )
        for row in stale_rows:
            try:
                delete_questionnaire_publication(row["id"])
            except ValueError:
                pass
        return []

    publication_ids = expand_questionnaire_set_for_session(questionnaire_set_id, session_id)
    stale_rows = query_all(
        "SELECT id FROM questionnaire_publications "
        "WHERE session_id=? AND questionnaire_set_id IS NOT NULL AND questionnaire_set_id!=?",
        (session_id, questionnaire_set_id),
    )
    for row in stale_rows:
        try:
            delete_questionnaire_publication(row["id"])
        except ValueError:
            pass
    return publication_ids


def _validate_transition(session, target_status):
    current = session["status"]
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    if target_status not in allowed:
        raise ValueError(
            f"Cannot transition session {session['id']} from '{current}' to '{target_status}'"
        )


def create_session(*, operator_id, session_no, task_id,
                   session_role=None, title=None, description=None,
                   agent_mode=None,
                   strategy_agent_enabled=None, emotion_agent_enabled=None,
                   research_state_monitoring_enabled=False,
                   time_limit_minutes=None, questionnaire_set_id=None):
    """Create a new experiment session in draft status.

    Args:
        operator_id: User ID creating the session.
        session_no: Session number.
        task_id: Required - the task to bind to this session.
        session_role: Deprecated, kept for backward compatibility.
        title: Optional session title.
        description: Optional session description.
        agent_mode: One of ``none``, ``strategy`` or ``emotion``.
        strategy_agent_enabled/emotion_agent_enabled: Deprecated compatibility
            inputs. They are converted to one mode and may not both be true.
    Returns the created session dict.
    """
    now = now_str()
    session_no = _normalize_session_no(session_no)
    _ensure_unique_session_no(session_no)
    questionnaire_set_id = _normalize_optional_int(questionnaire_set_id, "questionnaire_set_id")
    _validate_questionnaire_set(questionnaire_set_id)
    mode = _requested_agent_mode(
        agent_mode=agent_mode,
        strategy_agent_enabled=strategy_agent_enabled,
        emotion_agent_enabled=emotion_agent_enabled,
        default_mode="strategy",
    )
    mode_flags = flags_for_agent_mode(
        mode,
        research_state_monitoring_enabled=research_state_monitoring_enabled,
    )
    if time_limit_minutes in (None, "", 0, "0"):
        session_time_limit = None
    else:
        try:
            session_time_limit = int(time_limit_minutes)
        except (TypeError, ValueError):
            raise ValueError("time_limit_minutes must be an integer")
        if session_time_limit <= 0:
            session_time_limit = None

    sid = execute(
           """INSERT INTO experiment_sessions
            (session_no, session_role, task_id, status, created_by,
              agent_detection_enabled, agent_intervention_enabled,
              strategy_agent_enabled, emotion_agent_enabled, agent_mode,
              research_state_monitoring_enabled,
              created_at, updated_at, title, description, questionnaire_set_id,
              time_limit_minutes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            session_no, session_role or "", task_id, "draft", operator_id,
            1,  # agent_detection_enabled always 1
            1 if mode_flags["agent_intervention_enabled"] else 0,
            1 if mode_flags["strategy_agent_enabled"] else 0,
            1 if mode_flags["emotion_agent_enabled"] else 0,
            mode,
            1 if mode_flags["research_state_monitoring_enabled"] else 0,
            now, now,
            title or "", description or "", questionnaire_set_id,
            session_time_limit,
        ),
    )
    publication_ids = _sync_questionnaire_set_publications(sid, questionnaire_set_id)

    write_audit_log(
        operator_id=operator_id,
        action_type="session.create",
        target_type="experiment_session",
        target_id=sid,
        after_value=json.dumps({
            "session_no": session_no,
            "task_id": task_id,
            "old_agent_mode": None,
            "new_agent_mode": mode,
            "changed_at": now,
            "strategy_agent_enabled": mode_flags["strategy_agent_enabled"],
            "emotion_agent_enabled": mode_flags["emotion_agent_enabled"],
            "research_state_monitoring_enabled": mode_flags[
                "research_state_monitoring_enabled"
            ],
            "time_limit_minutes": session_time_limit,
            "questionnaire_set_id": questionnaire_set_id,
            "questionnaire_publication_ids": publication_ids,
        }, ensure_ascii=False),
        reason=f"Create draft session (no.{session_no})",
    )

    return _get_session(sid)


def start_session(*, session_id, operator_id):
    """Transition a session from draft -> running.

    Raises ValueError on illegal transition or constraint violation.
    """
    session = _get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    _validate_transition(session, "running")
    if session.get("agent_configuration_error"):
        raise ValueError(
            f"{INVALID_AGENT_CONFIGURATION}: choose one Agent mode before starting"
        )
    try:
        mode = resolve_session_agent_mode(session)
    except InvalidAgentConfiguration as exc:
        raise ValueError(
            f"{INVALID_AGENT_CONFIGURATION}: choose one Agent mode before starting"
        ) from exc
    conflicts = _session_no_conflicts(session["session_no"], exclude_session_id=session_id)
    if conflicts:
        ids = ", ".join(str(row["id"]) for row in conflicts)
        raise ValueError(
            f"课时 {session['session_no']} 存在重复课次（课次ID: {ids}），"
            "请先删除或归档重复课次后再开始"
        )

    # Refuse if another running session exists
    existing = query_one(
        "SELECT id FROM experiment_sessions WHERE status='running' AND id!=? LIMIT 1",
        (session_id,),
    )
    if existing:
        raise ValueError(
            f"Another session ({existing['id']}) is already running. "
            "Only one active session allowed at a time."
        )

    # Session role condition check removed - agent config is now explicit

    now = now_str()
    before = session["status"]
    execute(
        "UPDATE experiment_sessions SET status='running', start_time=?, "
        "deadline=NULL, updated_at=? WHERE id=?",
        (now, now, session_id),
    )
    set_setting("current_session_id", str(session_id))
    set_setting("current_session_no", str(session["session_no"]))
    if session.get("task_id"):
        set_setting("current_task_id", str(session["task_id"]))

    write_audit_log(
        operator_id=operator_id,
        action_type="session.start",
        target_type="experiment_session",
        target_id=session_id,
        before_value=before,
        after_value=json.dumps({
            "status": "running",
            "agent_mode": mode,
            "deadline": None,
            "discussion_timer": "not_started",
        }, ensure_ascii=False),
        reason=f"Start session {session_id} (role={session['session_role']})",
    )

    return _get_session(session_id)


def update_session_agent_config(*, session_id, operator_id,
                                 agent_mode=None,
                                 strategy_agent_enabled=None, emotion_agent_enabled=None,
                                 research_state_monitoring_enabled=None):
    """Update agent config for a draft experiment session.

    Only draft sessions can be modified. Legacy booleans are accepted only as
    compatibility inputs and are immediately collapsed into one mode.
    """
    session = _get_session(session_id)
    if not session:
        raise ValueError("Session %s not found" % session_id)

    if session["status"] != "draft":
        raise ValueError(
            "Cannot update agent config for session %s with status '%s'. "
            "Only draft sessions can be modified." % (session_id, session["status"])
        )

    if (
        agent_mode is None
        and strategy_agent_enabled is None
        and emotion_agent_enabled is None
        and research_state_monitoring_enabled is None
    ):
        return session  # Nothing to update

    now = now_str()
    mode = _requested_agent_mode(
        agent_mode=agent_mode,
        strategy_agent_enabled=strategy_agent_enabled,
        emotion_agent_enabled=emotion_agent_enabled,
        current_session=session,
    )
    research_enabled = (
        bool(session.get("research_state_monitoring_enabled"))
        if research_state_monitoring_enabled is None
        else bool(research_state_monitoring_enabled)
    )
    flags = flags_for_agent_mode(
        mode,
        research_state_monitoring_enabled=research_enabled,
    )
    old_mode = _current_mode_for_audit(session)
    execute(
        """UPDATE experiment_sessions
           SET agent_mode=?, strategy_agent_enabled=?, emotion_agent_enabled=?,
                agent_intervention_enabled=?, research_state_monitoring_enabled=?,
                updated_at=?
           WHERE id=?""",
        (
            mode,
            1 if flags["strategy_agent_enabled"] else 0,
            1 if flags["emotion_agent_enabled"] else 0,
            1 if flags["agent_intervention_enabled"] else 0,
            1 if flags["research_state_monitoring_enabled"] else 0,
            now,
            session_id,
        ),
    )

    write_audit_log(
        operator_id=operator_id,
        action_type="session.update_agent_config",
        target_type="experiment_session",
        target_id=session_id,
        before_value=json.dumps({
            "old_agent_mode": old_mode,
            "changed_at": now,
        }, ensure_ascii=False),
        after_value=json.dumps({
            "old_agent_mode": old_mode,
            "new_agent_mode": mode,
            "changed_at": now,
            "strategy_agent_enabled": flags["strategy_agent_enabled"],
            "emotion_agent_enabled": flags["emotion_agent_enabled"],
            "research_state_monitoring_enabled": flags[
                "research_state_monitoring_enabled"
            ],
        }, ensure_ascii=False),
        reason="Update agent config for session %s" % session_id,
    )

    return _get_session(session_id)


def end_session(*, session_id, operator_id):
    """Transition a session from running -> ended."""
    session = _get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    _validate_transition(session, "ended")

    # Freeze and persist unfinished group documents while the session is still
    # running.  Refuse the state transition on a persistence error so the
    # documents remain eligible for a safe retry.
    try:
        from services.auto_submit_service import submit_session_documents

        document_submission = submit_session_documents(session_id, operator_id=operator_id)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception(
            "Failed to submit collaborative documents for session %s", session_id
        )
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

    # Capture active session BEFORE the status update
    old_active_id = get_active_session_id()
    now = now_str()
    before = session["status"]
    execute(
        "UPDATE experiment_sessions SET status='ended', end_time=?, updated_at=? WHERE id=?",
        (now, now, session_id),
    )
    # Clear current_session_id if this was the active session
    if old_active_id == session_id:
        set_setting("current_session_id", "")

    write_audit_log(
        operator_id=operator_id,
        action_type="session.end",
        target_type="experiment_session",
        target_id=session_id,
        before_value=before,
        after_value="ended",
        reason=f"End session {session_id}",
    )

    try:
        from services.collaboration_state_finalization_service import safe_request_finalization_for_session_groups

        safe_request_finalization_for_session_groups(session_id, "teacher_close")
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to request state finalization for session %s",
            session_id,
            exc_info=True,
        )

    result = _get_session(session_id)
    result["document_submission"] = document_submission
    return result


def archive_session(*, session_id, operator_id):
    """Transition a session from ended -> archived."""
    session = _get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    _validate_transition(session, "archived")

    now = now_str()
    before = session["status"]
    execute(
        "UPDATE experiment_sessions SET status='archived', archived_at=?, updated_at=? WHERE id=?",
        (now, now, session_id),
    )

    write_audit_log(
        operator_id=operator_id,
        action_type="session.archive",
        target_type="experiment_session",
        target_id=session_id,
        before_value=before,
        after_value="archived",
        reason=f"Archive session {session_id}",
    )

    return _get_session(session_id)


def assign_task(*, session_id, task_id, operator_id, reason=None):
    """Assign a task to a session.

    For running sessions, requires an explicit reason to change task_id.
    """
    session = _get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session["status"] == "running" and session.get("task_id") is not None:
        if not reason:
            raise ValueError(
                f"Session {session_id} is running and already has task_id={session['task_id']}. "
                "Provide a 'reason' to change the task."
            )

    before = session.get("task_id")
    now = now_str()
    time_limit_minutes = _get_task_time_limit(task_id)
    execute(
        "UPDATE experiment_sessions SET task_id=?, time_limit_minutes=?, deadline=NULL, updated_at=? WHERE id=?",
        (task_id, time_limit_minutes, now, session_id),
    )

    write_audit_log(
        operator_id=operator_id,
        action_type="session.assign_task",
        target_type="experiment_session",
        target_id=session_id,
        before_value=str(before) if before else None,
        after_value=json.dumps({
            "task_id": task_id,
            "time_limit_minutes": time_limit_minutes,
            "deadline": None,
        }, ensure_ascii=False),
        reason=reason or f"Assign task {task_id} to session {session_id}",
    )

    return _get_session(session_id)


def list_sessions(status=None):
    """List all experiment sessions, optionally filtered by status."""
    if status:
        rows = query_all("""
            SELECT s.*, t.title AS task_title, t.question AS task_question,
                   t.time_limit_minutes AS task_time_limit
            FROM experiment_sessions s
            LEFT JOIN learning_tasks t ON s.task_id = t.id
            WHERE s.status=? ORDER BY s.session_no ASC
        """, (status,))
    else:
        rows = query_all("""
            SELECT s.*, t.title AS task_title, t.question AS task_question,
                   t.time_limit_minutes AS task_time_limit
            FROM experiment_sessions s
            LEFT JOIN learning_tasks t ON s.task_id = t.id
            ORDER BY s.session_no ASC
        """)
    sessions = [_session_payload(r) for r in rows]
    for session in sessions:
        _attach_questionnaire_publication_summary(session)
        session["group_discussions"] = _list_group_discussion_status(session["id"])
    return sessions


def _attach_questionnaire_publication_summary(session):
    rows = query_all("""
        SELECT qp.id AS publication_id,
               qp.questionnaire_id,
               qp.response_stage,
               qp.status,
               qp.group_id,
               qp.user_id,
               qp.questionnaire_set_id,
               q.title AS questionnaire_title,
               q.code AS questionnaire_code,
               q.timing AS questionnaire_timing
        FROM questionnaire_publications qp
        JOIN questionnaires q ON q.id = qp.questionnaire_id
        WHERE qp.session_id=?
        ORDER BY qp.response_stage ASC, q.sort_order ASC, q.id ASC, qp.id ASC
    """, (session["id"],))
    publications = [dict(r) for r in rows]
    session["questionnaire_publications"] = publications
    session["questionnaire_summary"] = {
        "pre": [p for p in publications if p["response_stage"] == "pre"],
        "post": [p for p in publications if p["response_stage"] == "post"],
    }


def _get_data_quality_warning_count():
    """Return count of data quality checks with non-ok status for active session."""
    from db import get_active_experiment_session
    session = get_active_experiment_session()
    if not session:
        return 0
    try:
        row = query_one(
            "SELECT COUNT(*) AS c FROM data_quality_checks "
            "WHERE session_id=? AND overall_status!='ok' AND overall_status!='unknown'",
            (session["id"],),
        )
        return int(row["c"]) if row else 0
    except Exception:
        return 0


def _get_safety_event_count():
    """Return count of safety signals entries."""
    row = query_one("SELECT COUNT(*) AS c FROM safety_signals")
    return int(row["c"]) if row else 0




def _is_condition_frozen():
    """Check whether all groups have their conditions frozen."""
    groups = query_all("SELECT id FROM groups")
    if not groups:
        return False
    frozen = query_one("SELECT COUNT(DISTINCT group_id) AS c FROM baseline_assignments WHERE allocation_locked=1")
    frozen_count = int(frozen["c"]) if frozen else 0
    total_groups = len(groups)
    return frozen_count >= total_groups


def _list_group_discussion_status(session_id, include_members=False):
    try:
        from services.group_discussion_runtime_service import group_discussion_timer_payload
        rows = query_all(
            """
            SELECT gsd.*, g.name AS group_name, g.group_code
            FROM group_session_discussions gsd
            LEFT JOIN groups g ON g.id = gsd.group_id
            WHERE gsd.session_id=?
            ORDER BY gsd.group_id ASC
            """,
            (session_id,),
        )
        result = []
        for row in rows or []:
            item = dict(row)
            item.update(group_discussion_timer_payload(item))
            if include_members:
                try:
                    member_rows = query_all(
                        """
                        SELECT gde.student_id, u.real_name, u.username
                        FROM group_discussion_entries gde
                        LEFT JOIN users u ON u.id = gde.student_id
                        WHERE gde.group_discussion_id=? AND gde.ready_at IS NOT NULL
                        ORDER BY gde.ready_at ASC, gde.id ASC
                        """,
                        (item["id"],),
                    )
                except Exception:
                    # Keep the existing status/count payload if the optional
                    # member lookup is unavailable on an older database.
                    member_rows = []
                item["ready_students"] = [
                    {
                        "student_id": member["student_id"],
                        "name": (
                            member["real_name"]
                            or member["username"]
                            or f"学生 #{member['student_id']}"
                        ),
                    }
                    for member in member_rows or []
                ]
            result.append(item)
        return result
    except Exception:
        return []


def get_session_status():
    """Return the T0 global status bar data."""
    server_now = now_str()
    session = get_active_experiment_session()
    if not session:
        return {
            "current_session": None,
            "session_role": None,
            "task": None,
            "detection_enabled": False,
            "intervention_enabled": False,
            "strategy_agent_enabled": False,
            "emotion_agent_enabled": False,
            "agent_mode": "none",
            "started_at": None,
            "deadline": None,
            "time_limit_minutes": None,
            "server_now": server_now,
            "elapsed_seconds": 0,
            "remaining_seconds": None,
            "remaining_minutes": None,
            "is_timeout": False,
            "group_discussions": [],
            "condition_frozen": _is_condition_frozen(),
            "data_quality_warning_count": _get_data_quality_warning_count(),
            "safety_event_count": _get_safety_event_count(),
        }

    task = None
    if session.get("task_id"):
        task = get_learning_task(session["task_id"])

    elapsed = 0
    is_timeout = False
    if session.get("start_time"):
        try:
            started = datetime.strptime(session["start_time"], "%Y-%m-%d %H:%M:%S")
            elapsed = int((datetime.now() - started).total_seconds())
        except (ValueError, TypeError):
            pass

    session = _session_payload(session)
    return {
        "current_session": session,
        "session_role": session.get("session_role", ""),
        "task": task,
        "detection_enabled": bool(session.get("agent_detection_enabled", True)),
        "intervention_enabled": bool(session.get("strategy_agent_enabled", False)),
        "strategy_agent_enabled": bool(session.get("strategy_agent_enabled", False)),
        "emotion_agent_enabled": bool(session.get("emotion_agent_enabled", False)),
        "agent_mode": session.get("agent_mode"),
        "started_at": session.get("start_time"),
        "deadline": None,
        "time_limit_minutes": session.get("time_limit_minutes"),
        "server_now": server_now,
        "elapsed_seconds": elapsed,
        "remaining_seconds": None,
        "remaining_minutes": None,
        "is_timeout": is_timeout,
        "group_discussions": _list_group_discussion_status(session["id"], include_members=True),
        "condition_frozen": _is_condition_frozen(),
        "data_quality_warning_count": _get_data_quality_warning_count(),
        "safety_event_count": _get_safety_event_count(),
    }


def get_current_status():
    """Return comprehensive T0 global status data (extended format with agent_flags, data_quality, safety blocks)."""
    base = get_session_status()
    session = base.get("current_session")

    # Build agent_flags block
    agent_flags = {
        "detection_enabled": base.get("detection_enabled", False),
        "intervention_enabled": base.get("intervention_enabled", False),
        "strategy_agent_enabled": base.get("strategy_agent_enabled", False),
        "emotion_agent_enabled": base.get("emotion_agent_enabled", False),
        "agent_mode": base.get("agent_mode", "none"),
        "readonly_reason": "standard",
    }

    # Build condition block
    condition = {
        "frozen": base.get("condition_frozen", False),
        "frozen_at": None,
    }
    if session and base.get("condition_frozen"):
        # Try to find frozen_at from baseline_assignments
        row = query_one(
            "SELECT frozen_at FROM baseline_assignments WHERE allocation_locked=1 ORDER BY frozen_at DESC LIMIT 1"
        )
        if row and row.get("frozen_at"):
            condition["frozen_at"] = row["frozen_at"]

    # Build data_quality block
    dq_warnings = _get_data_quality_warning_count()
    # Count severe (critical) checks separately
    severe_count = 0
    if session:
        try:
            sev_row = query_one(
                "SELECT COUNT(*) AS c FROM data_quality_checks "
                "WHERE session_id=? AND overall_status='critical'",
                (session["id"],),
            )
            severe_count = int(sev_row["c"]) if sev_row else 0
        except Exception:
            pass

    data_quality = {
        "warning_count": dq_warnings,
        "severe_count": severe_count,
    }

    # Build safety block
    event_count = _get_safety_event_count()
    unresolved_count = query_one("SELECT COUNT(*) AS c FROM safety_signals WHERE resolution IS NULL OR resolution=''")
    unresolved = int(unresolved_count["c"]) if unresolved_count else 0

    safety = {
        "event_count": event_count,
        "unresolved_count": unresolved,
    }

    return {
        "ok": True,
        "current_session": session,
        "session_role": base.get("session_role", ""),
        "task": base.get("task"),
        "detection_enabled": agent_flags["detection_enabled"],
        "intervention_enabled": agent_flags["intervention_enabled"],
        "strategy_agent_enabled": agent_flags["strategy_agent_enabled"],
        "emotion_agent_enabled": agent_flags["emotion_agent_enabled"],
        "agent_mode": agent_flags["agent_mode"],
        "started_at": session.get("start_time") if session else None,
        "deadline": None,
        "time_limit_minutes": base.get("time_limit_minutes"),
        "server_now": base.get("server_now"),
        "elapsed_seconds": base.get("elapsed_seconds", 0),
        "remaining_seconds": None,
        "remaining_minutes": None,
        "is_timeout": False,
        "group_discussions": base.get("group_discussions", []),
        "condition_frozen": condition["frozen"],
        "data_quality_warning_count": data_quality["warning_count"],
        "safety_event_count": safety["event_count"],
        "agent_flags": agent_flags,
        "condition": condition,
        "data_quality": data_quality,
        "safety": safety,
    }



def _get_session(session_id):
    row = query_one("SELECT * FROM experiment_sessions WHERE id=?", (session_id,))
    return _session_payload(row)


def get_session_by_id(session_id):
    """Public accessor for a single session row."""
    return _get_session(session_id)


def delete_session(*, session_id, operator_id):
    """Delete a draft experiment session.

    Only draft sessions can be deleted to avoid affecting student-side data.
    Raises ValueError if the session is not found or not in draft status.
    """
    session = _get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session["status"] != "draft":
        raise ValueError(
            f"Cannot delete session {session_id} with status '{session['status']}'. "
            "Only draft sessions can be deleted."
        )

    execute("DELETE FROM experiment_sessions WHERE id=?", (session_id,))

    write_audit_log(
        operator_id=operator_id,
        action_type="session.delete",
        target_type="experiment_session",
        target_id=session_id,
        before_value=session["status"],
        reason=f"Delete draft session {session_id} (no.{session['session_no']}, role={session['session_role']})",
    )


def update_session(*, session_id, operator_id, title=None, description=None,
                   task_id=None,
                    agent_mode=None,
                   strategy_agent_enabled=None, emotion_agent_enabled=None,
                    research_state_monitoring_enabled=None,
                    questionnaire_set_id=_UNSET):
    """Update a draft experiment session.

    Only draft sessions can be modified. Returns the updated session dict.
    Raises ValueError if the session is not found or not in draft status.
    """
    session = _get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session["status"] != "draft":
        raise ValueError(
            f"Cannot update session {session_id} with status '{session['status']}'. "
            "Only draft sessions can be modified."
        )

    now = now_str()
    updates = []
    params = []
    before_value = {}
    questionnaire_set_changed = False

    questionnaire_set_id = _normalize_optional_int(questionnaire_set_id, "questionnaire_set_id")
    if questionnaire_set_id is not _UNSET:
        _validate_questionnaire_set(questionnaire_set_id)

    if title is not None:
        updates.append("title=?")
        params.append(title)
        before_value["title"] = session.get("title", "")

    if description is not None:
        updates.append("description=?")
        params.append(description)
        before_value["description"] = session.get("description", "")

    if task_id is not None:
        updates.append("task_id=?")
        params.append(task_id)
        new_time_limit = _get_task_time_limit(task_id)
        updates.append("time_limit_minutes=?")
        params.append(new_time_limit)
        updates.append("deadline=?")
        params.append(None)
        before_value["task_id"] = session.get("task_id")
        before_value["time_limit_minutes"] = session.get("time_limit_minutes")

    requested_mode = None
    if (
        agent_mode is not None
        or strategy_agent_enabled is not None
        or emotion_agent_enabled is not None
        or research_state_monitoring_enabled is not None
    ):
        requested_mode = _requested_agent_mode(
            agent_mode=agent_mode,
            strategy_agent_enabled=strategy_agent_enabled,
            emotion_agent_enabled=emotion_agent_enabled,
            current_session=session,
        )
        research_enabled = (
            bool(session.get("research_state_monitoring_enabled"))
            if research_state_monitoring_enabled is None
            else bool(research_state_monitoring_enabled)
        )
        mode_flags = flags_for_agent_mode(
            requested_mode,
            research_state_monitoring_enabled=research_enabled,
        )
        updates.extend((
            "agent_mode=?",
            "strategy_agent_enabled=?",
            "emotion_agent_enabled=?",
            "agent_intervention_enabled=?",
            "research_state_monitoring_enabled=?",
        ))
        params.extend((
            requested_mode,
            1 if mode_flags["strategy_agent_enabled"] else 0,
            1 if mode_flags["emotion_agent_enabled"] else 0,
            1 if mode_flags["agent_intervention_enabled"] else 0,
            1 if mode_flags["research_state_monitoring_enabled"] else 0,
        ))
        before_value["old_agent_mode"] = _current_mode_for_audit(session)

    if questionnaire_set_id is not _UNSET:
        updates.append("questionnaire_set_id=?")
        params.append(questionnaire_set_id)
        before_value["questionnaire_set_id"] = session.get("questionnaire_set_id")
        questionnaire_set_changed = questionnaire_set_id != session.get("questionnaire_set_id")

    if not updates:
        return session  # Nothing to update

    updates.append("updated_at=?")
    params.append(now)
    params.append(session_id)

    execute(
        "UPDATE experiment_sessions SET " + ", ".join(updates) + " WHERE id=?",
        tuple(params),
    )
    publication_ids = []
    if questionnaire_set_changed:
        publication_ids = _sync_questionnaire_set_publications(session_id, questionnaire_set_id)

    write_audit_log(
        operator_id=operator_id,
        action_type="session.update",
        target_type="experiment_session",
        target_id=session_id,
        before_value=json.dumps(before_value, ensure_ascii=False) if before_value else None,
        after_value=json.dumps({
            "title": title if title is not None else session.get("title", ""),
            "description": description if description is not None else session.get("description", ""),
            "task_id": task_id if task_id is not None else session.get("task_id"),
            "old_agent_mode": before_value.get("old_agent_mode"),
            "new_agent_mode": requested_mode if requested_mode is not None else session.get("agent_mode"),
            "changed_at": now if requested_mode is not None else None,
            "questionnaire_set_id": questionnaire_set_id if questionnaire_set_id is not _UNSET else session.get("questionnaire_set_id"),
            "questionnaire_publication_ids": publication_ids,
        }, ensure_ascii=False),
        reason=f"Update draft session {session_id}",
    )

    return _get_session(session_id)
