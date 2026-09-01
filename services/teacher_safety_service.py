# -*- coding: utf-8 -*-
"""T8: Teacher safety pause / resume service.

Provides agent-level and session-level pause/resume for groups.
Writes to group_session_controls, safety_signals, and audit_logs.

All SQL is parameterised.
"""
import json
from db import db, query_one, query_all, execute, now_str, write_audit_log, get_active_session_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_or_create_control(group_id, session_id):
    """Return the group_session_controls row, creating one if missing."""
    row = query_one(
        "SELECT * FROM group_session_controls WHERE group_id=? AND session_id=?",
        (group_id, session_id),
    )
    if row:
        return dict(row)
    now = now_str()
    cid = execute(
        "INSERT INTO group_session_controls(group_id, session_id) VALUES(?,?)",
        (group_id, session_id),
    )
    row = query_one("SELECT * FROM group_session_controls WHERE id=?", (cid,))
    return dict(row) if row else None


def _write_safety_signal(group_id, session_id, signal_type, severity, operator_id, reason, control_id=None):
    """Write a safety_signals entry."""
    execute(
        """INSERT INTO safety_signals
               (group_id, member_id, session_id, signal_type, severity,
                handled_by, resolution, group_session_control_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (group_id, None, session_id, signal_type, severity,
         operator_id, reason, control_id, now_str()),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pause_agent(*, group_id, session_id, operator_id, reason="Teacher paused agent"):
    """Pause agent for a group in a session.

    Returns the updated control dict with paused status.
    """
    control = _get_or_create_control(group_id, session_id)
    if control.get("agent_paused"):
        return {"ok": True, "status": "already_paused", "control": control}

    now = now_str()
    execute(
        "UPDATE group_session_controls SET agent_paused=1, pause_reason=?, paused_by=?, paused_at=? WHERE id=?",
        (reason, operator_id, now, control["id"]),
    )
    control["agent_paused"] = 1
    control["pause_reason"] = reason
    control["paused_by"] = operator_id

    _write_safety_signal(
        group_id, session_id,
        signal_type="agent_paused",
        severity="warning",
        operator_id=operator_id,
        reason=reason,
        control_id=control["id"],
    )
    write_audit_log(
        operator_id=operator_id,
        action_type="safety.pause_agent",
        target_type="group_session_controls",
        target_id=control["id"],
        after_value=json.dumps({"group_id": group_id, "session_id": session_id}, ensure_ascii=False),
        reason=reason,
    )
    return {"ok": True, "status": "paused", "control": control}


def resume_agent(*, group_id, session_id, operator_id, reason="Teacher resumed agent"):
    """Resume agent for a group in a session.

    Returns the updated control dict with resumed status.
    """
    control = _get_or_create_control(group_id, session_id)
    if not control.get("agent_paused"):
        return {"ok": True, "status": "already_active", "control": control}

    now = now_str()
    execute(
        "UPDATE group_session_controls SET agent_paused=0, resumed_by=?, resumed_at=? WHERE id=?",
        (operator_id, now, control["id"]),
    )
    control["agent_paused"] = 0
    control["resumed_by"] = operator_id
    control["resumed_at"] = now

    _write_safety_signal(
        group_id, session_id,
        signal_type="agent_resumed",
        severity="info",
        operator_id=operator_id,
        reason=reason,
        control_id=control["id"],
    )
    write_audit_log(
        operator_id=operator_id,
        action_type="safety.resume_agent",
        target_type="group_session_controls",
        target_id=control["id"],
        after_value=json.dumps({"group_id": group_id, "session_id": session_id}, ensure_ascii=False),
        reason=reason,
    )
    return {"ok": True, "status": "active", "control": control}


def pause_session(*, group_id, session_id, operator_id, reason="Teacher paused session"):
    """Pause the entire session for a group.

    Returns the updated control dict with paused status.
    """
    control = _get_or_create_control(group_id, session_id)
    if control.get("session_paused"):
        return {"ok": True, "status": "already_paused", "control": control}

    now = now_str()
    execute(
        "UPDATE group_session_controls SET session_paused=1, pause_reason=?, paused_by=?, paused_at=? WHERE id=?",
        (reason, operator_id, now, control["id"]),
    )
    control["session_paused"] = 1
    control["pause_reason"] = reason
    control["paused_by"] = operator_id

    _write_safety_signal(
        group_id, session_id,
        signal_type="session_paused",
        severity="critical",
        operator_id=operator_id,
        reason=reason,
        control_id=control["id"],
    )
    write_audit_log(
        operator_id=operator_id,
        action_type="safety.pause_session",
        target_type="group_session_controls",
        target_id=control["id"],
        after_value=json.dumps({"group_id": group_id, "session_id": session_id}, ensure_ascii=False),
        reason=reason,
    )
    return {"ok": True, "status": "paused", "control": control}


def resume_session(*, group_id, session_id, operator_id, reason="Teacher resumed session"):
    """Resume the session for a group.

    Returns the updated control dict with resumed status.
    """
    control = _get_or_create_control(group_id, session_id)
    if not control.get("session_paused"):
        return {"ok": True, "status": "already_active", "control": control}

    now = now_str()
    execute(
        "UPDATE group_session_controls SET session_paused=0, resumed_by=?, resumed_at=? WHERE id=?",
        (operator_id, now, control["id"]),
    )
    control["session_paused"] = 0
    control["resumed_by"] = operator_id
    control["resumed_at"] = now

    _write_safety_signal(
        group_id, session_id,
        signal_type="session_resumed",
        severity="info",
        operator_id=operator_id,
        reason=reason,
        control_id=control["id"],
    )
    write_audit_log(
        operator_id=operator_id,
        action_type="safety.resume_session",
        target_type="group_session_controls",
        target_id=control["id"],
        after_value=json.dumps({"group_id": group_id, "session_id": session_id}, ensure_ascii=False),
        reason=reason,
    )
    return {"ok": True, "status": "active", "control": control}


def should_group_agent_paused(group_id, session_id):
    """Return True if the agent is paused for the given group/session.

    Intended for future student-side consumption.  Currently unused by student
    endpoints but implemented for the interface contract.
    """
    row = query_one(
        """SELECT agent_paused FROM group_session_controls
            WHERE group_id=? AND session_id=?
            ORDER BY id DESC LIMIT 1""",
        (group_id, session_id),
    )
    if row and row["agent_paused"]:
        return True
    return False


def get_control_status(group_id, session_id):
    """Return the current control status for a group/session."""
    row = query_one(
        "SELECT * FROM group_session_controls WHERE group_id=? AND session_id=?",
        (group_id, session_id),
    )
    if not row:
        return {
            "group_id": group_id,
            "session_id": session_id,
            "agent_paused": False,
            "session_paused": False,
        }
    d = dict(row)
    d["agent_paused"] = bool(d.get("agent_paused", 0))
    d["session_paused"] = bool(d.get("session_paused", 0))
    return d


def get_all_groups_safety(session_id):
    """Return all groups with their agent/session control status for the safety modal.

    Returns a list of dicts with:
      group_id, group_code, agent_paused, session_paused,
      latest_signal (dict or None), latest_action_at (str or None)
    """
    groups = query_all("SELECT id, group_code, name FROM groups ORDER BY id ASC")
    result = []
    for g in groups:
        control = get_control_status(g["id"], session_id)
        # Get latest safety signal for this group/session
        latest_signal = query_one(
            "SELECT signal_type, severity, resolution, created_at FROM safety_signals WHERE group_id=? AND session_id=? ORDER BY id DESC LIMIT 1",
            (g["id"], session_id),
        )
        # Get latest operation timestamp
        control_row = query_one(
            "SELECT paused_at, resumed_at FROM group_session_controls WHERE group_id=? AND session_id=?",
            (g["id"], session_id),
        )
        latest_action_at = None
        if control_row:
            cr = dict(control_row)
            latest_action_at = cr.get("resumed_at") or cr.get("paused_at")

        result.append({
            "group_id": g["id"],
            "group_code": g["group_code"] or "Group %s" % g["id"],
            "agent_paused": control["agent_paused"],
            "session_paused": control["session_paused"],
            "latest_signal": dict(latest_signal) if latest_signal else None,
            "latest_action_at": latest_action_at,
        })
    return result
