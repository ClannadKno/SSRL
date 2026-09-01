# -*- coding: utf-8 -*-
"""Teacher console API routes for experiment session management."""

from flask import current_app, jsonify, request
from core import app
from auth import login_required, current_user
from db import get_learning_task, query_one, query_all
from services.teacher_session_service import (
    create_session,
    update_session,
    update_session_agent_config,
    start_session,
    end_session,
    archive_session,
    delete_session,
    assign_task,
    list_sessions,
    get_session_status,
    get_session_by_id,
    get_current_status,
)
from services.teacher_participation_service import get_participation_summary, get_participation_timeline
from services.teacher_emotion_trend_service import get_emotion_trend
from services.teacher_emotion_review_service import get_emotion_review
from services.emotion_feedback_record_service import get_session_agent_records

@app.route("/api/teacher/status/current", methods=["GET"])
@login_required("teacher")
def api_teacher_status_current():
    """Return comprehensive T0 global status bar data (extended format)."""
    status = get_current_status()
    return jsonify(status)

@app.route("/api/teacher/session/status", methods=["GET"])
@login_required("teacher")
def api_teacher_session_status():
    """Alias for /api/teacher/status/current (dashboard T0 compatibility)."""
    status = get_current_status()
    return jsonify(status)

@app.route("/api/teacher/sessions", methods=["GET"])
@login_required("teacher")
def api_teacher_list_sessions():
    """List all experiment sessions, optionally filtered by ?status=..."""
    status = request.args.get("status")
    sessions = list_sessions(status=status)
    return jsonify({"sessions": sessions})


@app.route(
    "/api/teacher/session/<int:session_id>/emotion-feedbacks", methods=["GET"]
)
@login_required("teacher")
def api_teacher_session_emotion_feedbacks(session_id):
    """Return emotion feedback and canonical state records in separate lists."""
    group_id = request.args.get("group_id")
    limit = request.args.get("limit", 200)
    try:
        if group_id not in (None, ""):
            group_id = int(group_id)
            if group_id < 1:
                raise ValueError("group_id must be a positive integer")
        else:
            group_id = None
        limit = int(limit)
        result = get_session_agent_records(
            session_id, group_id=group_id, limit=limit
        )
        return jsonify(result)
    except (TypeError, ValueError) as exc:
        message = str(exc)
        status = 404 if message == "session not found" else 400
        return jsonify({"error": message}), status


@app.route("/api/teacher/groups", methods=["GET"])
@login_required("teacher")
def api_teacher_groups():
    """Return all groups for filter dropdowns."""
    rows = query_all("SELECT id, name, group_code FROM groups ORDER BY id")
    groups = [{"group_id": r['id'], "group_name": r['name'], "group_code": r['group_code']} for r in rows]
    return jsonify({"groups": groups})

@app.route("/api/teacher/session/create", methods=["POST"])
@login_required("teacher")
def api_teacher_session_create():
    """Create a new draft experiment session."""
    user = current_user()
    data = request.get_json(force=True)
    session_no = data.get("session_no")
    task_id = data.get("task_id")

    if not session_no:
        return jsonify({"error": "session_no is required"}), 400
    if not task_id:
        return jsonify({"error": "task_id is required. Create a task first, then create a session."}), 400

    strategy_agent_enabled = data.get("strategy_agent_enabled")
    emotion_agent_enabled = data.get("emotion_agent_enabled")
    research_state_monitoring_enabled = data.get(
        "research_state_monitoring_enabled", False
    )
    for field, value in (
        ("strategy_agent_enabled", strategy_agent_enabled),
        ("emotion_agent_enabled", emotion_agent_enabled),
        (
            "research_state_monitoring_enabled",
            research_state_monitoring_enabled,
        ),
    ):
        if value is not None and not isinstance(value, bool):
            return jsonify({"error": f"{field} must be a boolean"}), 400

    try:
        session = create_session(
            operator_id=user["id"],
            session_no=int(session_no),
            task_id=int(task_id),
            session_role=data.get("session_role"),
            title=data.get("title", ""),
            description=data.get("description", ""),
            agent_mode=data.get("agent_mode"),
            strategy_agent_enabled=strategy_agent_enabled,
            emotion_agent_enabled=emotion_agent_enabled,
            research_state_monitoring_enabled=research_state_monitoring_enabled,
            time_limit_minutes=data.get("time_limit_minutes"),
            questionnaire_set_id=data.get("questionnaire_set_id"),
        )
        return jsonify({"ok": True, "session": session}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/session/<int:session_id>/agent-config", methods=["PUT"])
@login_required("teacher")
def api_teacher_session_update_agent_config(session_id):
    """Update the mutually-exclusive Agent mode for a session.

    Only draft status sessions can be modified. Returns 400 for non-draft sessions.
    """
    user = current_user()
    data = request.get_json(force=True)
    agent_mode = data.get("agent_mode")
    strategy_agent_enabled = data.get("strategy_agent_enabled")
    emotion_agent_enabled = data.get("emotion_agent_enabled")
    research_state_monitoring_enabled = data.get(
        "research_state_monitoring_enabled"
    )

    # Require at least one field
    if (
        agent_mode is None
        and strategy_agent_enabled is None
        and emotion_agent_enabled is None
        and research_state_monitoring_enabled is None
    ):
        return jsonify({"error": "agent_mode is required"}), 400

    # Strict boolean validation
    if strategy_agent_enabled is not None and not isinstance(strategy_agent_enabled, bool):
        return jsonify({"error": "strategy_agent_enabled must be a boolean"}), 400
    if emotion_agent_enabled is not None and not isinstance(emotion_agent_enabled, bool):
        return jsonify({"error": "emotion_agent_enabled must be a boolean"}), 400
    if (
        research_state_monitoring_enabled is not None
        and not isinstance(research_state_monitoring_enabled, bool)
    ):
        return jsonify(
            {"error": "research_state_monitoring_enabled must be a boolean"}
        ), 400

    try:
        session = update_session_agent_config(
            session_id=session_id,
            operator_id=user["id"],
            agent_mode=agent_mode,
            strategy_agent_enabled=strategy_agent_enabled,
            emotion_agent_enabled=emotion_agent_enabled,
            research_state_monitoring_enabled=research_state_monitoring_enabled,
        )
        return jsonify({"ok": True, "session": session})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400



@app.route("/api/teacher/session/<int:session_id>", methods=["PUT"])
@login_required("teacher")
def api_teacher_session_update(session_id):
    """Update a draft experiment session's settings.
    
    Only draft status sessions can be modified.
    """
    user = current_user()
    data = request.get_json(force=True) or {}

    try:
        kwargs = {"session_id": session_id, "operator_id": user["id"]}
        if "title" in data:
            kwargs["title"] = str(data["title"])
        if "description" in data:
            kwargs["description"] = str(data["description"])
        if "task_id" in data:
            kwargs["task_id"] = int(data["task_id"])
        if "agent_mode" in data:
            kwargs["agent_mode"] = data["agent_mode"]
        if "strategy_agent_enabled" in data:
            if not isinstance(data["strategy_agent_enabled"], bool):
                return jsonify({"error": "strategy_agent_enabled must be a boolean"}), 400
            kwargs["strategy_agent_enabled"] = data["strategy_agent_enabled"]
        if "emotion_agent_enabled" in data:
            if not isinstance(data["emotion_agent_enabled"], bool):
                return jsonify({"error": "emotion_agent_enabled must be a boolean"}), 400
            kwargs["emotion_agent_enabled"] = data["emotion_agent_enabled"]
        if "research_state_monitoring_enabled" in data:
            if not isinstance(data["research_state_monitoring_enabled"], bool):
                return jsonify(
                    {"error": "research_state_monitoring_enabled must be a boolean"}
                ), 400
            kwargs["research_state_monitoring_enabled"] = data[
                "research_state_monitoring_enabled"
            ]
        if "questionnaire_set_id" in data:
            kwargs["questionnaire_set_id"] = data.get("questionnaire_set_id")

        session = update_session(**kwargs)
        return jsonify({"ok": True, "session": session})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/session/start", methods=["POST"])
@login_required("teacher")
def api_teacher_session_start():
    """Start a draft session (draft -> running)."""
    user = current_user()
    data = request.get_json(force=True)
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        session = start_session(
            session_id=int(session_id),
            operator_id=user["id"],
        )
        return jsonify({"ok": True, "session": session})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/teacher/session/<int:session_id>", methods=["DELETE"])
@login_required("teacher")
def api_teacher_session_delete(session_id):
    """Delete a draft experiment session."""
    user = current_user()
    try:
        delete_session(
            session_id=session_id,
            operator_id=user["id"],
        )
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/teacher/session/end", methods=["POST"])
@login_required("teacher")
def api_teacher_session_end():
    """End a running session (running -> ended)."""
    user = current_user()
    data = request.get_json(force=True)
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        session = end_session(
            session_id=int(session_id),
            operator_id=user["id"],
        )
        return jsonify({"ok": True, "session": session})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/teacher/session/archive", methods=["POST"])
@login_required("teacher")
def api_teacher_session_archive():
    """Archive an ended session (ended -> archived)."""
    user = current_user()
    data = request.get_json(force=True)
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        session = archive_session(
            session_id=int(session_id),
            operator_id=user["id"],
        )
        return jsonify({"ok": True, "session": session})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/teacher/task/assign", methods=["POST"])
@login_required("teacher")
def api_teacher_task_assign():
    """Assign a task to a session."""
    user = current_user()
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    task_id = data.get("task_id")
    reason = data.get("reason")

    if not session_id or task_id is None:
        return jsonify({"error": "session_id and task_id are required"}), 400

    # Validate task exists
    task = get_learning_task(int(task_id))
    if not task:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    try:
        session = assign_task(
            session_id=int(session_id),
            task_id=int(task_id),
            operator_id=user["id"],
            reason=reason,
        )
        return jsonify({"ok": True, "session": session}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
@app.route("/api/teacher/group/<int:group_id>/participation-summary", methods=["GET"])
@login_required("teacher")
def api_teacher_participation_summary(group_id):
    """T3: Return member text participation summary for a group."""
    session_id = request.args.get("session_id", type=int)
    window = request.args.get("window", "session")
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")

    try:
        result = get_participation_summary(
            group_id=group_id,
            session_id=session_id,
            window=window,
            start_time=start_time,
            end_time=end_time,
        )
        if "error" in result:
            return jsonify(result), 404
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/teacher/group/<int:group_id>/participation-timeline', methods=['GET'])
@login_required('teacher')
def api_teacher_participation_timeline(group_id):
    """T3: Return time-bucketed participation timeline for a group."""
    session_id = request.args.get('session_id', type=int)
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    window_minutes = request.args.get('window_minutes', default=3, type=int)

    try:
        result = get_participation_timeline(
            group_id=group_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            window_minutes=window_minutes,
        )
        if 'error' in result:
            return jsonify(result), 404
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route("/api/teacher/group/<int:group_id>/emotion-trend", methods=["GET"])
@login_required("teacher")
def api_teacher_emotion_trend(group_id):
    """T4: Return group-level collaboration emotion trend snapshots."""
    session_id = request.args.get("session_id", type=int)
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")
    window_minutes = request.args.get("window_minutes", default=3, type=int)
    include_legacy_scope = str(
        request.args.get("include_legacy_scope", "")
    ).strip().lower() in {"1", "true", "yes", "on"}

    try:
        result = get_emotion_trend(
            group_id=group_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            window_minutes=window_minutes,
            include_legacy_scope=include_legacy_scope,
        )
        if "error" in result:
            return jsonify(result), 404
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/teacher/group/<int:group_id>/emotion-review", methods=["GET"])
@login_required("teacher")
def api_teacher_emotion_review(group_id):
    """T4: Return timeline-aligned messages, states, and interventions."""
    session_id = request.args.get("session_id", type=int)
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")
    window_minutes = request.args.get("window_minutes", default=1, type=int)
    include_legacy_scope = str(
        request.args.get("include_legacy_scope", "")
    ).strip().lower() in {"1", "true", "yes", "on"}

    try:
        result = get_emotion_review(
            group_id=group_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            window_minutes=window_minutes,
            include_legacy_scope=include_legacy_scope,
        )
        if "error" in result:
            return jsonify(result), 404
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

# ============================================================
# Batch 7: T5 Agent Audit API endpoints
# ============================================================

from services.teacher_agent_audit_service import (
    get_agent_audit,
    record_manual_uptake,
    record_unblind,
    list_groups_with_sessions,
)
from services.teacher_history_service import query_history

from services.teacher_safety_service import (
    pause_agent,
    resume_agent,
    pause_session,
    resume_session,
    should_group_agent_paused,
    get_control_status,
    get_all_groups_safety,
)

from services.intervention_pipeline_v2.strategy_service import StrategyService

@app.route("/api/teacher/strategies", methods=["GET"])
@login_required("teacher")
def api_teacher_strategies():
    """Return all built-in intervention strategies with metadata."""
    try:
        strategies = StrategyService.get_all_strategies()
        result = []
        for s in strategies:
            result.append({
                "id": s["id"],
                "version": s["version"],
                "strategy_type": s["strategy_type"],
                "sub_category": s["sub_category"],
                "display_name": s["display_name"],
                "applicable_states": s["applicable_states"],
                "goal": s["goal"],
                "cooldown_seconds": s["cooldown_seconds"],
                "max_chars": s["max_chars"],
            })
        return jsonify({"ok": True, "strategies": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/audit/groups", methods=["GET"])
@login_required("teacher")
def api_teacher_audit_groups():
    """Return list of groups with their session selectors."""
    try:
        result = list_groups_with_sessions()
        return jsonify({"groups": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/group/<int:group_id>/agent-audit", methods=["GET"])
@login_required("teacher")
def api_teacher_agent_audit(group_id):
    """T5: Return full agent audit chain for a group/session.

    Query params:
      session_id (int, required)
      blinded (str, \"true\"|\"false\", default \"false\")
    """
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    blinded = request.args.get("blinded", "false").lower() == "true"

    try:
        result = get_agent_audit(
            group_id=group_id,
            session_id=session_id,
            blinded=blinded,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route(
    "/api/teacher/group/<int:group_id>/state-suite-audit",
    methods=["GET"],
)
@login_required("teacher")
def api_teacher_state_suite_audit(group_id):
    """Return a privacy-minimised DB audit on isolated test servers only."""
    from config import SSRL_ENABLE_STATE_SUITE_AUDIT
    from services.state_suite_audit_service import get_state_suite_audit

    if not (
        current_app.testing
        or SSRL_ENABLE_STATE_SUITE_AUDIT
    ):
        return jsonify({"error": "not found"}), 404

    session_id = request.args.get("session_id", type=int)
    discussion_id = request.args.get("discussion_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    try:
        return jsonify(
            get_state_suite_audit(
                group_id=group_id,
                session_id=session_id,
                discussion_id=discussion_id,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/teacher/intervention/<int:intervention_id>/manual-uptake", methods=["POST"])
@login_required("teacher")
def api_teacher_manual_uptake(intervention_id):
    """Record manual uptake correction for an intervention.

    Body:
      manual_uptake_type (str): one of ignored/acknowledged/discussed/adopted/adapted/rejected
      reason (str, optional)
    """
    user = current_user()
    data = request.get_json(force=True) or {}
    uptake_type = data.get("manual_uptake_type")
    reason = data.get("reason")

    if not uptake_type:
        return jsonify({"error": "manual_uptake_type is required"}), 400
    if not reason:
        return jsonify({"error": "reason is required for manual uptake correction"}), 400

    try:
        result = record_manual_uptake(
            intervention_log_id=intervention_id,
            manual_uptake_type=uptake_type,
            corrected_by=user["id"],
            reason=reason,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/audit/unblind", methods=["POST"])
@login_required("teacher")
def api_teacher_audit_unblind():
    """Record non-blind audit view with reason.

    Body:
      reason (str, required)
    """
    user = current_user()
    data = request.get_json(force=True) or {}
    reason = data.get("reason")
    if not reason:
        return jsonify({"error": "reason is required for unblinding"}), 400

    result = record_unblind(operator_id=user["id"], reason=reason)
    return jsonify(result)

# ============================================================
# Batch 7: T8 Safety Pause / Resume API endpoints
# ============================================================

@app.route("/api/teacher/group/<int:group_id>/pause-agent", methods=["POST"])
@login_required("teacher")
def api_teacher_pause_agent(group_id):
    """Pause agent for a group."""
    user = current_user()
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    reason = data.get("reason", "Teacher paused agent via API")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        result = pause_agent(
            group_id=group_id,
            session_id=session_id,
            operator_id=user["id"],
            reason=reason,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/group/<int:group_id>/resume-agent", methods=["POST"])
@login_required("teacher")
def api_teacher_resume_agent(group_id):
    """Resume agent for a group."""
    user = current_user()
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    reason = data.get("reason", "Teacher resumed agent via API")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        result = resume_agent(
            group_id=group_id,
            session_id=session_id,
            operator_id=user["id"],
            reason=reason,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/group/<int:group_id>/pause-session", methods=["POST"])
@login_required("teacher")
def api_teacher_pause_session(group_id):
    """Pause the session for a group."""
    user = current_user()
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    reason = data.get("reason", "Teacher paused session via API")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        result = pause_session(
            group_id=group_id,
            session_id=session_id,
            operator_id=user["id"],
            reason=reason,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/group/<int:group_id>/resume-session", methods=["POST"])
@login_required("teacher")
def api_teacher_resume_session(group_id):
    """Resume the session for a group."""
    user = current_user()
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    reason = data.get("reason", "Teacher resumed session via API")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    try:
        result = resume_session(
            group_id=group_id,
            session_id=session_id,
            operator_id=user["id"],
            reason=reason,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/group/<int:group_id>/control-status", methods=["GET"])
@login_required("teacher")
def api_teacher_control_status(group_id):
    """Return current agent/session pause status for a group."""
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    result = get_control_status(group_id=group_id, session_id=session_id)
    return jsonify(result)

@app.route("/api/teacher/safety/overview", methods=["GET"])
@login_required("teacher")
def api_teacher_safety_overview():
    """Return all groups with their safety control status for the specified session."""
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    try:
        result = get_all_groups_safety(session_id)
        return jsonify({"groups": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ============================================================
# T7: History Query API endpoint
# ============================================================

@app.route("/api/teacher/history", methods=["GET"])
@login_required("teacher")
def api_teacher_history():
    """Query historical data without triggering LLM or new detection.

    Query params:
      group_id (int, optional)
      session_id (int, optional)
      task_id (int, optional)
      start_time (str, optional, ISO datetime)
      end_time (str, optional, ISO datetime)
      data_type (str, default "messages"): one of messages/detector_outputs/
                 interventions/uptake/ssrl_events/deliverables/scores/surveys/audit_logs
      blind (str, "true"/"false", default "true")
    """
    return jsonify({"error": "history API has been retired; use export endpoints"}), 404
    group_id = request.args.get("group_id", type=int)
    session_id = request.args.get("session_id", type=int)
    task_id = request.args.get("task_id", type=int)
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")
    data_type = request.args.get("data_type", "messages")
    blind = request.args.get("blind", "true").lower() != "false"

    try:
        result = query_history(
            group_id=group_id,
            session_id=session_id,
            task_id=task_id,
            start_time=start_time,
            end_time=end_time,
            data_type=data_type,
            blind=blind,
        )
        if isinstance(result, dict) and "error" in result:
            return jsonify(result), 400
        return jsonify({"rows": result, "count": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
