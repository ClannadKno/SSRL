# -*- coding: utf-8 -*-
"""Minimal API routes for SSRL-ESP."""
import os, json, threading, uuid
from datetime import datetime, timedelta
from flask import jsonify, request
from core import app
from config import *
from db import *
from db import _normalize_questionnaire_items, _replace_questionnaire_items, is_fixed_questionnaire, create_questionnaire_publication, list_questionnaire_publications, update_questionnaire_publication, delete_questionnaire_publication, list_questionnaire_completion, list_fixed_questionnaires, list_published_questionnaires_for_student, create_questionnaire_submission, _sanitize_student_item
from auth import *
from agent import *
from services.intervention_execution import clean_assistant_message_content
from services.student_help_service import extract_student_help_request
from config import DISCUSSION_PIPELINE_V2_ENABLED


def _safe_int(v, d=0, min_value=None, max_value=None):
    """
    Safe integer conversion with optional min/max bounds.
    The parameter names match the keyword argument names used by callers.
    """
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = d
    if min_value is not None:
        v = max(min_value, v)
    if max_value is not None:
        v = min(max_value, v)
    return v


def _safe_json_loads(value, fallback=None):
    if not value:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _read_latest_state_assessment(
    group_id,
    assessment_id=None,
    session_id=None,
):
    if assessment_id:
        row = query_one(
            """
            SELECT id, group_id, session_id, task_id, session_no, discussion_id,
                   rule_state_code, llm_state_code, fused_state_code,
                   fused_state_label, assessment_status, confidence,
                   risk_level, risk_label, should_intervene,
                   self_regulation_detected, evidence_summary,
                   rule_assessment_json, llm_assessment_json, fusion_json,
                   created_at
            FROM state_assessments
            WHERE id=? AND group_id=?
            """,
            (assessment_id, group_id),
        )
    else:
        session_filter = ""
        params = [group_id]
        if session_id is not None:
            session_filter = " AND session_id=?"
            params.append(session_id)
        row = query_one(
            f"""
            SELECT id, group_id, session_id, task_id, session_no, discussion_id,
                   rule_state_code, llm_state_code, fused_state_code,
                   fused_state_label, assessment_status, confidence,
                   risk_level, risk_label, should_intervene,
                   self_regulation_detected, evidence_summary,
                   rule_assessment_json, llm_assessment_json, fusion_json,
                   created_at
            FROM state_assessments
            WHERE group_id=?{session_filter}
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(params),
        )
    if not row:
        return None
    data = dict(row)
    data["should_intervene"] = bool(data.get("should_intervene"))
    data["self_regulation_detected"] = bool(data.get("self_regulation_detected"))
    data["rule_assessment"] = _safe_json_loads(data.pop("rule_assessment_json", None), {})
    data["llm_assessment"] = _safe_json_loads(data.pop("llm_assessment_json", None), None)
    data["fusion"] = _safe_json_loads(data.pop("fusion_json", None), {})
    return data


def _read_group_state_v2(group_id, session_id=None):
    """Read the canonical teacher state and retain Stage 1 only as audit data."""

    from services.teacher_emotion_trend_service import (
        get_current_canonical_state,
    )

    session_filter = ""
    params = [group_id]
    if session_id is not None:
        session_filter = " AND session_id=?"
        params.append(session_id)
    state_row = query_one(
        f"""
        SELECT id, group_id, state_code, state_label, risk_level, risk_label,
               evidence, task_id, session_no, session_id, discussion_id,
               context_json, feature_json,
               state_score, rule_assessment_json, state_assessment_id,
               assessment_status, confirmed_windows, confirmation_status,
               llm_state_code, fusion_json, created_at
        FROM group_states
        WHERE group_id=?{session_filter}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    )
    state = dict(state_row) if state_row else {}
    latest_assessment = _read_latest_state_assessment(
        group_id,
        state.get("state_assessment_id"),
        session_id=session_id,
    )
    canonical = get_current_canonical_state(group_id, session_id=session_id)
    if canonical.get("error"):
        canonical = {
            "state_code": "unclassified",
            "state_label": "未分类",
            "final_sub_state_code": None,
            "final_sub_state_label": None,
            "assessment_status": "unclassified",
            "assignment_source": "canonical_read_error",
            "confidence": None,
            "source": "canonical_state_read_model",
            "read_only": True,
        }
    elif (
        not canonical.get("final_sub_state_code")
        and (state or latest_assessment)
        and not (
            canonical.get("assessment_status") == "observing"
            and canonical.get("assignment_source")
            == "post_intervention_observation"
        )
    ):
        canonical["state_code"] = "unclassified"
        canonical["state_label"] = "未分类"
        canonical["assessment_status"] = "unclassified"
        canonical["assignment_source"] = "legacy_coarse_without_canonical"
    coarse_code = (
        state.get("state_code")
        or (latest_assessment or {}).get("fused_state_code")
        or "unknown"
    )
    coarse_label = (
        state.get("state_label")
        or (latest_assessment or {}).get("fused_state_label")
        or "未知"
    )
    coarse_confidence = (
        state.get("state_score")
        if state.get("state_score") is not None
        else (latest_assessment or {}).get("confidence")
    )
    return {
        "group_id": group_id,
        # Deprecated compatibility aliases now point to the canonical/process
        # display contract and never expose a Stage 1 code as the final state.
        "state_code": canonical.get("state_code") or "unclassified",
        "state_label": canonical.get("state_label") or "未分类",
        "final_sub_state_code": canonical.get("final_sub_state_code"),
        "final_sub_state_label": canonical.get("final_sub_state_label"),
        "display_state_code": (
            canonical.get("display_state_code")
            or canonical.get("state_code")
            or "unclassified"
        ),
        "display_state_label": (
            canonical.get("display_state_label")
            or canonical.get("state_label")
            or "未分类"
        ),
        "assessment_status": canonical.get("assessment_status") or "unclassified",
        "assignment_source": canonical.get("assignment_source"),
        "inferred": bool(canonical.get("inferred")),
        "confidence": canonical.get("confidence"),
        "segment_id": canonical.get("segment_id"),
        "source_batch_id": canonical.get("source_batch_id"),
        "error_code": canonical.get("error_code"),
        "active_silence": canonical.get("active_silence") or {"active": False},
        "coarse_state_code": coarse_code,
        "coarse_state_label": coarse_label,
        "legacy_state_code": coarse_code,
        "coarse_confidence": coarse_confidence,
        "coarse_risk_level": int(
            state.get("risk_level")
            or (latest_assessment or {}).get("risk_level")
            or 0
        ),
        "coarse_risk_label": (
            state.get("risk_label")
            or (latest_assessment or {}).get("risk_label")
            or ""
        ),
        # Risk remains a Stage 1 compatibility signal, not the final state.
        "risk_level": int(
            state.get("risk_level")
            or (latest_assessment or {}).get("risk_level")
            or 0
        ),
        "risk_label": (
            state.get("risk_label")
            or (latest_assessment or {}).get("risk_label")
            or ""
        ),
        "state_score": canonical.get("confidence"),
        "evidence": state.get("evidence")
        or (latest_assessment or {}).get("evidence_summary")
        or "",
        "task_id": state.get("task_id") or (latest_assessment or {}).get("task_id"),
        "session_id": canonical.get("resolved_session_id") or session_id,
        "session_no": state.get("session_no")
        or (latest_assessment or {}).get("session_no"),
        "discussion_id": canonical.get("discussion_id")
        or state.get("discussion_id")
        or (latest_assessment or {}).get("discussion_id"),
        "context_json": _safe_json_loads(state.get("context_json"), {}),
        "feature_json": _safe_json_loads(state.get("feature_json"), {}),
        "rule_assessment_json": _safe_json_loads(state.get("rule_assessment_json"), {}),
        "confirmed_windows": int(state.get("confirmed_windows") or 0),
        "confirmation_status": state.get("confirmation_status") or "",
        "state_assessment_id": state.get("state_assessment_id")
        or (latest_assessment or {}).get("id"),
        "group_state_id": state.get("id"),
        "llm_state_code": state.get("llm_state_code")
        or (latest_assessment or {}).get("llm_state_code"),
        "fusion_json": _safe_json_loads(
            state.get("fusion_json"),
            (latest_assessment or {}).get("fusion") or {},
        ),
        "latest_coarse_assessment": latest_assessment,
        "updated_at": canonical.get("updated_at"),
        "read_only": True,
        "source": "canonical_state_read_model",
    }


def _student_visible_message(row):
    message = dict(row)
    if (message.get("resolved_role") or message.get("role")) == "agent":
        message["content"] = clean_assistant_message_content(message.get("content"))
    return message


def _group_discussion_locked_response(session_id, group_id):
    try:
        from services.group_discussion_runtime_service import is_group_discussion_write_closed
        if is_group_discussion_write_closed(session_id, group_id):
            return jsonify({
                "error": "group discussion closed",
                "code": "GROUP_DISCUSSION_CLOSED",
            }), 423
    except Exception:
        return None
    return None


def _room_runtime_row(group_id):
    """Load current room state after reclaiming any orphaned expired lease."""
    try:
        from services.intervention_pipeline_v2.room_lease_service import (
            RoomLeaseService,
        )

        RoomLeaseService.recover_expired(group_id)
    except Exception:
        app.logger.exception(
            "Failed to recover expired room lease for group %s",
            group_id,
        )
    return query_one(
        """
        SELECT id, state, version, active_intervention_run_id, lock_expires_at
          FROM groups
         WHERE id=?
        """,
        (group_id,),
    )


def _room_ai_lock_payload(room):
    room_data = dict(room or {})
    locked = room_data.get("state") == "AI_INTERVENING"
    owner = {}
    if locked:
        try:
            from services.intervention_pipeline_v2.room_lease_service import (
                RoomLeaseService,
            )

            owner = RoomLeaseService.get_lock_info(
                room_data.get("id") or room_data.get("group_id")
            )
        except Exception:
            owner = {}
    return {
        "locked": locked,
        "reason": "ROOM_AI_INTERVENING" if locked else None,
        "active_intervention_run_id": room_data.get("active_intervention_run_id"),
        "lock_expires_at": room_data.get("lock_expires_at"),
        "lock_owner_type": owner.get("lock_owner_type"),
        "lock_owner_run_id": owner.get("lock_owner_run_id"),
        "lock_owner_status": owner.get("lock_owner_status"),
        "lock_reason": owner.get("lock_reason"),
    }


def _ai_intervening_locked_response(group_id):
    room = _room_runtime_row(group_id)
    if not room or room["state"] != "AI_INTERVENING":
        return None
    room_data = dict(room)
    room_data["id"] = group_id
    ai_lock = _room_ai_lock_payload(room_data)
    return jsonify({
        "error": "AI intervening",
        "code": "ROOM_AI_INTERVENING",
        "locked": True,
        "ai_lock": ai_lock,
    }), 423


def _queue_student_help_request(
    group_id,
    user_id,
    request_text,
    session_ctx,
    *,
    source_message_id=None,
    check_rate_limit=True,
):
    from services.student_help_service import _normalized_request_text, _student_help_rate_limit
    from db import execute, now_str, query_one

    normalized = _normalized_request_text(request_text)

    if source_message_id:
        existing = query_one(
            "SELECT id, status FROM help_requests WHERE source_message_id=? ORDER BY id DESC LIMIT 1",
            (source_message_id,),
        )
        if existing:
            return {
                "accepted": True,
                "queued": existing["status"] in {"QUEUED", "RUNNING"},
                "duplicate": True,
                "help_request_id": existing["id"],
                "request_text": normalized,
                "assistant_log_id": None,
                "assistant_message_id": None,
            }

    if check_rate_limit:
        limit = _student_help_rate_limit(
            group_id,
            session_id=session_ctx.get("session_id"),
            discussion_id=session_ctx.get("discussion_id")
            or session_ctx.get("group_discussion_id"),
        )
        if not limit["allowed"]:
            return {
                "accepted": False,
                "queued": False,
                "rate_limited": True,
                "retry_after_seconds": limit["retry_after_seconds"],
                "reason": limit["reason"],
                "request_text": normalized,
                "assistant_log_id": None,
                "assistant_message_id": None,
            }
    scope_conn = db()
    try:
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            scope_conn,
            group_id=group_id,
            message_id=source_message_id,
            session_id=session_ctx.get("session_id"),
            session_no=session_ctx.get("session_no"),
            task_id=session_ctx.get("task_id"),
            discussion_id=session_ctx.get("discussion_id")
            or session_ctx.get("group_discussion_id"),
            allow_legacy_fallback=False,
        )
    finally:
        scope_conn.close()
    help_request_id = execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id, discussion_id,
            status, request_text, source_message_id, created_at
        ) VALUES(?,?,?,?,?,?,'QUEUED',?,?,?)
        """,
        (
            group_id,
            user_id,
            scope.task_id,
            scope.session_no,
            scope.session_id,
            scope.discussion_id,
            normalized,
            source_message_id,
            now_str(),
        ),
    )
    try:
        from agent.help_tasks import process_student_help
        process_student_help.schedule(args=(help_request_id,), delay=0)
        scheduled = True
    except Exception as exc:
        execute(
            "UPDATE help_requests SET status='FAILED', failure_reason=?, completed_at=? WHERE id=?",
            (f"schedule_failed: {exc}"[:200], now_str(), help_request_id),
        )
        raise
    return {
        "accepted": True,
        "queued": True,
        "help_request_id": help_request_id,
        "request_text": normalized,
        "scheduled": scheduled,
        "assistant_log_id": None,
        "assistant_message_id": None,
    }


def _queue_student_message_monitoring(message, group_id, *, trigger_type="student_message"):
    if not DISCUSSION_PIPELINE_V2_ENABLED:
        return {"queued": False, "reason": "pipeline_v2_disabled"}
    if not isinstance(message, dict) or message.get("duplicate"):
        return {"queued": False, "reason": "duplicate_or_missing_message"}
    try:
        from services.discussion_pipeline_v2.monitoring_service import MonitoringService as _ms
        _ms.process_new_message(
            message_id=message["id"],
            group_id=group_id,
            sequence=message.get("sequence") or 0,
            is_student_msg=True,
            trigger_type=trigger_type,
        )
        return {"queued": True, "trigger_type": trigger_type}
    except Exception as exc:
        app.logger.warning(
            "Failed to queue monitoring for student message %s in group %s: %s",
            message.get("id"),
            group_id,
            exc,
        )
        return {"queued": False, "reason": "schedule_failed", "error": str(exc)}


@app.route("/api/message", methods=["POST"])
@login_required("student")
def api_message():
    user = current_user()
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify({"error": "no active experiment session"}), 400
    tab_token = get_tab_token_from_request()
    if not tab_token:
        return jsonify({"error": "no session"}), 401
    data = request.get_json(force=True)
    group_id = int(data.get("group_id"))
    content = (data.get("content") or "").strip()
    client_message_id = data.get("client_message_id")
    if get_user_group_id(user["id"]) != group_id:
        return jsonify({"error": "access denied"}), 403
    locked = _group_discussion_locked_response(session_ctx["session_id"], group_id)
    if locked:
        return locked
    ai_locked = _ai_intervening_locked_response(group_id)
    if ai_locked:
        return ai_locked
    if not content:
        return jsonify({"error": "empty"}), 400
    room = _room_runtime_row(group_id)
    if not room:
        return jsonify({"error": "not found"}), 404
    if room["state"] == "AI_INTERVENING":
        ai_lock = _room_ai_lock_payload(room)
        return jsonify({
            "error": "AI intervening",
            "code": "ROOM_AI_INTERVENING",
            "locked": True,
            "ai_lock": ai_lock,
        }), 423
    if room["state"] == "CLOSED":
        return jsonify({"error": "room closed", "code": "ROOM_CLOSED"}), 423
    message = create_message(group_id, user["id"], content, role="student",
                             client_message_id=client_message_id)
    help_request = extract_student_help_request(content)
    help_response = None
    if message and not message.get("duplicate"):
        _queue_student_message_monitoring(
            message,
            group_id,
            trigger_type="student_help" if help_request else "student_message",
        )
        if help_request:
            print(f"[SERA DEBUG][api_message] help_request detected: text={repr(content[:80])}")
            help_response = _queue_student_help_request(
                group_id,
                user["id"],
                help_request,
                session_ctx,
                source_message_id=message["id"],
            )
    return jsonify({"ok": True, "message_id": message["id"] if message else None, "help_request_detected": bool(help_request), "help_response": help_response})

@app.route("/api/group/<int:group_id>/messages")
@login_required()
def api_group_messages(group_id):
    user = current_user()
    if user["role"] == "student" and get_user_group_id(user["id"]) != group_id:
        return jsonify({"error": "access denied"}), 403
    after_id = _safe_int(request.args.get("after_id"), 0, min_value=0)
    limit = _safe_int(request.args.get("limit"), 120, min_value=1, max_value=120)
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if user["role"] == "student" and session_ctx:
        if after_id > 0:
            rows = query_all(
                "SELECT m.*, COALESCE(ep.display_name, '') AS display_name, u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role "
                "FROM messages m JOIN users u ON m.user_id = u.id LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
                "WHERE m.group_id=? AND m.id>? AND m.session_no=? AND m.task_id=? "
                "ORDER BY m.id ASC LIMIT ?",
                (group_id, after_id, session_ctx["session_no"], session_ctx["task_id"], limit)
            )
        else:
            rows = query_all(
                "SELECT m.*, COALESCE(ep.display_name, '') AS display_name, u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role "
                "FROM messages m JOIN users u ON m.user_id = u.id LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
                "WHERE m.group_id=? AND m.session_no=? AND m.task_id=? "
                "ORDER BY m.id DESC LIMIT ?",
                (group_id, session_ctx["session_no"], session_ctx["task_id"], limit)
            )
            rows = list(reversed(rows))
    else:
        rows = query_all("SELECT m.*, COALESCE(ep.display_name, '') AS display_name, u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role FROM messages m JOIN users u ON m.user_id = u.id LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id WHERE m.group_id=? AND m.id>? ORDER BY m.id ASC LIMIT ?", (group_id, after_id, limit)) if after_id > 0 else query_all("SELECT m.*, COALESCE(ep.display_name, '') AS display_name, u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role FROM messages m JOIN users u ON m.user_id = u.id LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id WHERE m.group_id=? ORDER BY m.id DESC LIMIT ?", (group_id, limit))
        if after_id == 0:
            rows = list(reversed(rows))
    messages = [_student_visible_message(r) for r in (rows or [])]
    return jsonify({"messages": messages, "latest_id": max([m["id"] for m in messages]) if messages else after_id})
@app.route("/api/rooms/<int:room_id>/events")
@login_required("student")
def api_room_events(room_id):
    user = current_user()
    if get_user_group_id(user["id"]) != room_id:
        return jsonify({"error": "access denied"}), 403
    after_sequence = _safe_int(request.args.get("after_sequence"), 0, min_value=0)
    limit = _safe_int(request.args.get("limit"), 120, min_value=1, max_value=120)
    room = _room_runtime_row(room_id)
    if not room: return jsonify({"error": "not found"}), 404
    rows = query_all("SELECT m.id, m.user_id, m.content, m.role, m.sequence, u.participant_code, COALESCE(ep.display_name, '') AS display_name FROM messages m JOIN users u ON m.user_id = u.id LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id WHERE m.group_id=? AND m.sequence>? ORDER BY m.sequence ASC LIMIT ?", (room_id, after_sequence, limit))
    next_sequence = after_sequence
    if rows: next_sequence = max(r["sequence"] for r in rows)
    messages = [_student_visible_message(r) for r in rows]
    return jsonify({"room": dict(room), "messages": messages, "next_sequence": next_sequence})


def _student_sync_messages(group_id, session_ctx, after_id, limit):
    if session_ctx:
        if after_id > 0:
            rows = query_all(
                "SELECT m.*, COALESCE(ep.display_name, '') AS display_name, "
                "u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role "
                "FROM messages m JOIN users u ON m.user_id = u.id "
                "LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
                "WHERE m.group_id=? AND m.id>? AND m.session_no=? AND m.task_id=? "
                "ORDER BY m.id ASC LIMIT ?",
                (group_id, after_id, session_ctx["session_no"], session_ctx["task_id"], limit),
            )
        else:
            rows = query_all(
                "SELECT m.*, COALESCE(ep.display_name, '') AS display_name, "
                "u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role "
                "FROM messages m JOIN users u ON m.user_id = u.id "
                "LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
                "WHERE m.group_id=? AND m.session_no=? AND m.task_id=? "
                "ORDER BY m.id DESC LIMIT ?",
                (group_id, session_ctx["session_no"], session_ctx["task_id"], limit),
            )
            rows = list(reversed(rows))
    elif after_id > 0:
        rows = query_all(
            "SELECT m.*, COALESCE(ep.display_name, '') AS display_name, "
            "u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role "
            "FROM messages m JOIN users u ON m.user_id = u.id "
            "LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
            "WHERE m.group_id=? AND m.id>? ORDER BY m.id ASC LIMIT ?",
            (group_id, after_id, limit),
        )
    else:
        rows = query_all(
            "SELECT m.*, COALESCE(ep.display_name, '') AS display_name, "
            "u.participant_code, COALESCE(NULLIF(TRIM(m.role),''), u.role) AS resolved_role "
            "FROM messages m JOIN users u ON m.user_id = u.id "
            "LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
            "WHERE m.group_id=? ORDER BY m.id DESC LIMIT ?",
            (group_id, limit),
        )
        rows = list(reversed(rows))

    messages = [_student_visible_message(r) for r in (rows or [])]
    return {
        "messages": messages,
        "latest_id": max([m["id"] for m in messages]) if messages else after_id,
    }


def _student_sync_room_events(group_id, after_sequence, limit):
    room = _room_runtime_row(group_id)
    if not room:
        return {
            "room": None,
            "messages": [],
            "next_sequence": after_sequence,
            "ai_lock": {"locked": False, "reason": None},
        }
    rows = query_all(
        "SELECT m.id, m.user_id, m.content, m.role, m.sequence, u.participant_code, "
        "COALESCE(ep.display_name, '') AS display_name "
        "FROM messages m JOIN users u ON m.user_id = u.id "
        "LEFT JOIN experiment_participants ep ON m.user_id = ep.user_id AND m.group_id = ep.group_id "
        "WHERE m.group_id=? AND m.sequence>? ORDER BY m.sequence ASC LIMIT ?",
        (group_id, after_sequence, limit),
    )
    next_sequence = max([r["sequence"] for r in rows]) if rows else after_sequence
    room_data = dict(room)
    room_data["id"] = group_id
    return {
        "room": room_data,
        "messages": [_student_visible_message(r) for r in rows],
        "next_sequence": next_sequence,
        "ai_lock": _room_ai_lock_payload(room_data),
    }


def _student_pipeline_terminal(row):
    """Return a safe, client-facing terminal classification for one pipeline."""
    statuses = [
        str(row.get("final_status") or "").strip().upper(),
        str(row.get("publish_status") or "").strip().upper(),
    ]
    for status in statuses:
        if not status:
            continue
        if status in {"PUBLISHED", "ALREADY_PUBLISHED"}:
            return "PUBLISHED", status
        if status == "SUPPRESSED" or status.startswith("SUPPRESSED_"):
            return "SUPPRESSED", status
        if status == "STALE":
            return "STALE", status
        if status == "SUPERSEDED":
            return "SUPERSEDED", status
        if status == "SKIPPED":
            return "SKIPPED", status
        if status in {"FAILED", "ERROR", "FAILURE", "TIMEOUT", "CANCELLED"}:
            return "FAILED", status
        if status == "NOT_PUBLISHED":
            return "NOT_PUBLISHED", status
    return None, None


def _student_sync_pipeline_runs(group_id, session_ctx, trigger_message_id):
    """Expose only lifecycle fields needed by the student's expected gate.

    This is a read-only projection. It deliberately excludes generated text,
    model payloads, room lock tokens, and participant identity.
    """
    if not trigger_message_id:
        return []

    session_filter = ""
    params = [group_id, trigger_message_id]
    if session_ctx and session_ctx.get("session_id") is not None:
        session_filter = " AND session_id=?"
        params.append(session_ctx["session_id"])

    try:
        rows = query_all(
            f"""
            SELECT id, run_uuid, group_id, session_id, discussion_id,
                   trigger_message_id, parent_run_id, superseded_by_run_id,
                   assessment_batch_id, assessment_owner_pipeline_run_id,
                   input_start_sequence, input_end_sequence,
                   input_cutoff_student_sequence,
                   replaced_by_pipeline_run_id, replacement_reason,
                   replacement_trigger_message_id, replacement_cutoff_sequence,
                   trigger_level_state, latest_state, latest_should_intervene,
                   latest_state_pipeline_run_id,
                   stage2_status, stage3_status,
                   room_lock_acquired_at, room_lock_released_at,
                   publish_status, published_message_id, published_at,
                   final_status, failure_code, skip_reason,
                   created_at, updated_at
            FROM strategy_pipeline_runs
            WHERE group_id=? AND trigger_message_id=?{session_filter}
            ORDER BY id ASC
            """,
            tuple(params),
        )
    except Exception:
        app.logger.exception(
            "Failed to read student pipeline projection for group %s trigger %s",
            group_id,
            trigger_message_id,
        )
        return []

    trigger_row = query_one(
        "SELECT created_at FROM messages WHERE id=? AND group_id=? LIMIT 1",
        (trigger_message_id, group_id),
    )
    trigger_at = trigger_row["created_at"] if trigger_row else None
    result = []
    for row in rows or []:
        data = dict(row)
        replacement = None
        replacement_id = data.get("replaced_by_pipeline_run_id")
        if replacement_id:
            replacement = query_one(
                """
                SELECT id, stage2_status, canonical_sub_state_code,
                       should_intervene, publish_status, final_status
                FROM strategy_pipeline_runs
                WHERE id=? AND group_id=?
                LIMIT 1
                """,
                (replacement_id, group_id),
            )
        terminal_reason, terminal_status = _student_pipeline_terminal(data)
        data["pipeline_run_id"] = data.pop("id", None)
        data.pop("run_uuid", None)
        data["trigger_message_at"] = trigger_at or data.get("created_at")
        data["terminal_reason"] = terminal_reason
        data["terminal_status"] = terminal_status
        data["original_pipeline_run_id"] = data.get("pipeline_run_id")
        data["original_pipeline_terminal"] = data.get("final_status")
        data["replacement_pipeline_run_id"] = (
            replacement["id"] if replacement else None
        )
        data["replacement_final_state"] = (
            replacement["canonical_sub_state_code"]
            if replacement and str(replacement["stage2_status"] or "").upper() == "SUCCEEDED"
            else None
        )
        data["replacement_should_intervene"] = (
            replacement["should_intervene"]
            if replacement
            and str(replacement["stage2_status"] or "").upper() == "SUCCEEDED"
            else None
        )
        data["replacement_publish_status"] = (
            replacement["publish_status"] if replacement else None
        )
        data["pipeline_terminal_at"] = (
            data.get("published_at")
            if terminal_reason == "PUBLISHED"
            else data.get("updated_at")
            if terminal_reason
            else None
        )
        data["lease_acquired_at"] = data.get("room_lock_acquired_at")
        data["lease_released_at"] = data.get("room_lock_released_at")
        result.append(data)
    return result


def _student_sync_discussion(session_ctx, group_id):
    from services.group_discussion_runtime_service import (
        get_group_discussion_runtime,
        group_discussion_timer_payload,
    )

    runtime = None
    if session_ctx and session_ctx.get("session_id") and group_id:
        runtime = get_group_discussion_runtime(session_ctx["session_id"], group_id)
    timer = group_discussion_timer_payload(runtime)
    status = timer.get("group_discussion_status")
    return {
        "runtime": runtime,
        "status": status,
        "waiting": status == "waiting",
        "ready_count": runtime.get("ready_student_count") if runtime else 0,
        "expected_count": runtime.get("expected_student_count") if runtime else 0,
        "remaining_seconds": timer.get("group_remaining_seconds"),
        "timed_out": timer.get("group_timed_out"),
        **timer,
    }


def _student_sync_document(user_id, group_id, session_ctx):
    if not session_ctx or not session_ctx.get("task_id"):
        return {"document": None, "permission": None}
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
    if not doc:
        return {"document": None, "permission": None}
    from services.collaborative_permissions import get_document_permission

    document = dict(doc)
    return {
        "document": document,
        "permission": get_document_permission(user_id, document["id"]),
    }


def _student_sync_navigation(user_id, group_id, session_ctx, discussion, document):
    if not session_ctx:
        return {
            "session_open": False,
            "pretest_required": False,
            "posttest_available": False,
            "should_enter_posttest": False,
        }
    pretest_required = has_student_pending_questionnaires(
        "pre",
        user_id,
        session_id=session_ctx["session_id"],
        group_id=group_id,
    )
    document_submitted = bool(document and document.get("status") in {"submitted", "locked"})
    posttest_available = bool(
        document_submitted
        or discussion.get("group_discussion_status") in {"timed_out", "submitted", "closed"}
    )
    return {
        "session_open": bool(session_ctx.get("status") == "running"),
        "pretest_required": pretest_required,
        "posttest_available": posttest_available,
        "should_enter_posttest": bool(posttest_available and not pretest_required),
    }


@app.route("/api/student/sync")
@login_required("student")
def api_student_sync():
    user = current_user()
    heartbeat_touched = touch_client_session()
    group_id = get_user_group_id(user["id"])
    after_message_id = _safe_int(request.args.get("after_message_id"), 0, min_value=0)
    after_sequence = _safe_int(request.args.get("after_sequence"), 0, min_value=0)
    limit = _safe_int(request.args.get("limit"), 120, min_value=1, max_value=120)

    base = {
        "ok": True,
        "server_time": now_str(),
        "heartbeat": {
            "ok": True,
            "touched": heartbeat_touched,
            "min_interval_seconds": 30,
        },
    }
    if not group_id:
        return jsonify({
            **base,
            "status": "missing_group",
            "session_open": False,
            "group_id": None,
            "session": None,
            "room": None,
            "ai_lock": {"locked": False, "reason": None},
            "chat": {"messages": [], "latest_id": after_message_id},
            "messages": [],
            "latest_message_id": after_message_id,
            "pipeline": None,
            "pipelines": [],
            "events": {"room": None, "messages": [], "next_sequence": after_sequence},
            "event_sequence": after_sequence,
            "discussion": _student_sync_discussion(None, None),
            "document": None,
            "permission": None,
            "collaborative_document": {"document": None, "permission": None},
            "navigation": _student_sync_navigation(user["id"], None, None, {}, None),
        }), 200

    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    chat = _student_sync_messages(group_id, session_ctx, after_message_id, limit)
    events = _student_sync_room_events(group_id, after_sequence, limit)
    pipeline_runs = _student_sync_pipeline_runs(group_id, session_ctx, after_message_id)
    discussion = _student_sync_discussion(session_ctx, group_id)
    document_state = _student_sync_document(user["id"], group_id, session_ctx)
    document = document_state["document"]
    navigation = _student_sync_navigation(user["id"], group_id, session_ctx, discussion, document)
    status = "ok" if session_ctx and session_ctx.get("task_id") else "waiting_session"

    return jsonify({
        **base,
        "status": status,
        "session_open": navigation["session_open"],
        "group_id": group_id,
        "session": dict(session_ctx) if session_ctx else None,
        "room": events["room"],
        "ai_lock": events["ai_lock"],
        "chat": chat,
        "messages": chat["messages"],
        "latest_message_id": chat["latest_id"],
        "pipeline": pipeline_runs[0] if pipeline_runs else None,
        "pipelines": pipeline_runs,
        "events": {
            "room": events["room"],
            "messages": events["messages"],
            "next_sequence": events["next_sequence"],
        },
        "event_sequence": events["next_sequence"],
        "discussion": discussion,
        "group_discussion": discussion.get("runtime"),
        "group_discussion_status": discussion.get("group_discussion_status"),
        "group_remaining_seconds": discussion.get("group_remaining_seconds"),
        "group_timed_out": discussion.get("group_timed_out"),
        "document": document,
        "permission": document_state["permission"],
        "collaborative_document": document_state,
        "navigation": navigation,
    }), 200

@app.route("/api/heartbeat", methods=["POST"])
@login_required("student")
def api_heartbeat():
    user = current_user()
    touch_client_session()
    group_id = get_user_group_id(user["id"])
    if not group_id: return jsonify({"ok": False}), 400
    return jsonify({"ok": True, "group_id": group_id})

@app.route("/api/checkin", methods=["POST"])
@login_required("student")
def api_checkin():
    user = current_user()
    data = request.get_json(force=True)
    group_id = int(data.get("group_id"))
    if get_user_group_id(user["id"]) != group_id: return jsonify({"error": "access denied"}), 403
    checkin_type = (data.get("checkin_type") or "post").strip().lower()
    if checkin_type not in {"pre", "mid", "post", "event"}: checkin_type = "post"
    emotion_option = (data.get("emotion_option") or "smooth").strip()
    session_ctx = require_running_session()
    if checkin_type in {"mid", "event"}:
        locked = _group_discussion_locked_response(session_ctx["session_id"], group_id)
        if locked:
            return locked
    positivity = int(data.get("positivity", 0))
    engagement = int(data.get("engagement", 0))
    atmosphere = int(data.get("atmosphere", 0))
    expression_willingness = int(data.get("expression_willingness", 0))
    note = (data.get("note") or "")[:500]
    session_id = session_ctx["session_id"]
    task_id = session_ctx["task_id"]
    session_no = session_ctx["session_no"]
    checkin_id = execute("INSERT INTO emotion_checkins(group_id, user_id, session_no, emotion_option, positivity, engagement, atmosphere, expression_willingness, note, checkin_type, session_id, task_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (group_id, user["id"], session_no, emotion_option, positivity, engagement, atmosphere, expression_willingness, note, checkin_type, session_id, task_id, now_str()))
    scheduled = False
    if checkin_type in {"mid", "event"} and DISCUSSION_PIPELINE_V2_ENABLED:
        from services.discussion_pipeline_v2.monitoring_service import MonitoringService as _ms
        res = _ms.run_detection(group_id, trigger_type="checkin_" + checkin_type)
        scheduled = not res.get("skipped", True)
    return jsonify({"ok": True, "checkin_type": checkin_type, "analysis_scheduled": scheduled})

@app.route("/api/student/help", methods=["POST"])
@login_required("student")
def api_student_help():
    user = current_user()
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify({"error": "no active experiment session"}), 400
    data = request.get_json(force=True)
    group_id = int(data.get("group_id"))
    request_text = (data.get("request_text") or "").strip()
    print(f"[SERA DEBUG][api_student_help] called: group={group_id}, user={user['id']}, text={repr(request_text[:80])}")
    if get_user_group_id(user["id"]) != group_id: return jsonify({"error": "access denied"}), 403
    locked = _group_discussion_locked_response(session_ctx["session_id"], group_id)
    if locked:
        return locked
    ai_locked = _ai_intervening_locked_response(group_id)
    if ai_locked:
        return ai_locked
    from services.student_help_service import _student_help_rate_limit, _normalized_request_text, _student_help_message_text
    limit = _student_help_rate_limit(
        group_id,
        session_id=session_ctx.get("session_id"),
        discussion_id=session_ctx.get("discussion_id")
        or session_ctx.get("group_discussion_id"),
    )
    print(f"[SERA DEBUG][api_student_help] rate_limit: allowed={limit['allowed']}, retry_after={limit.get('retry_after_seconds')}")
    if not limit["allowed"]:
        return jsonify({**limit, "accepted": False, "rate_limited": True}), 429
    normalized = _normalized_request_text(request_text)
    message_text = _student_help_message_text(normalized)
    request_message = create_message(group_id, user["id"], message_text, role="student", client_message_id=data.get("client_message_id"))
    _queue_student_message_monitoring(
        request_message,
        group_id,
        trigger_type="student_help",
    )
    help_response = _queue_student_help_request(
        group_id,
        user["id"],
        normalized,
        session_ctx,
        source_message_id=request_message.get("id") if isinstance(request_message, dict) else None,
        check_rate_limit=False,
    )
    if not help_response.get("accepted"):
        return jsonify(help_response), 429
    return jsonify({"accepted": True, "help_response": help_response}), 202

@app.route("/api/intervention/feedback", methods=["POST"])
@login_required("student")
def api_intervention_feedback():
    user = current_user()
    data = request.get_json(force=True)
    log_id = int(data.get("log_id"))
    rating = (data.get("rating") or "neutral").strip()
    if rating not in {"helpful", "neutral", "not_helpful", "interruptive"}: rating = "neutral"
    log = query_one("SELECT * FROM intervention_logs WHERE id=?", (log_id,))
    if not log: return jsonify({"error": "not found"}), 404
    if get_user_group_id(user["id"]) != log["group_id"]: return jsonify({"error": "access denied"}), 403
    execute("INSERT OR REPLACE INTO intervention_feedback(log_id, user_id, rating, note, created_at) VALUES(?,?,?,?,?)", (log_id, user["id"], rating, "", now_str()))
    return jsonify({"ok": True})

@app.route("/api/group/<int:group_id>/state")
@login_required()
def api_group_state(group_id):
    user = current_user()
    if user["role"] == "student" and get_user_group_id(user["id"]) != group_id:
        return jsonify({"error": "access denied"}), 403
    if not query_one("SELECT id FROM groups WHERE id=?", (group_id,)):
        return jsonify({"error": "not found"}), 404
    session_id = request.args.get("session_id", type=int)
    if session_id is None:
        session_id = get_active_session_id()
    return jsonify(_read_group_state_v2(group_id, session_id=session_id))
# ============================================================
# Questionnaire API endpoints
# ============================================================

@app.route("/api/student/current-session")
@login_required("student")
def api_student_current_session():
    """Return student-facing session state split from group discussion state."""
    user = current_user()
    group_id = get_user_group_id(user["id"])
    if not group_id:
        return jsonify({
            "status": "missing_group",
            "session_open": False,
            "pretest_completed": False,
            "posttest_available": False,
        }), 200

    from services.session_lifecycle import get_student_accessible_session_context
    from services.group_discussion_runtime_service import (
        get_group_discussion_runtime,
        group_discussion_timer_payload,
    )

    ctx = get_student_accessible_session_context(user["id"])
    if not ctx:
        return jsonify({
            "status": "waiting_session",
            "session_open": False,
            "group_id": group_id,
            "pretest_completed": False,
            "posttest_available": False,
            **group_discussion_timer_payload(None),
        }), 200

    runtime = get_group_discussion_runtime(ctx["session_id"], group_id)
    timer = group_discussion_timer_payload(runtime)
    pretest_completed = not has_student_pending_questionnaires(
        "pre",
        user["id"],
        session_id=ctx["session_id"],
        group_id=group_id,
    )
    submitted_doc = query_one(
        """
        SELECT id FROM collaborative_documents
        WHERE group_id=? AND status='submitted'
          AND (session_id=? OR (session_id IS NULL AND task_id=? AND session_no=?))
        ORDER BY id DESC LIMIT 1
        """,
        (group_id, ctx["session_id"], ctx.get("task_id"), ctx.get("session_no")),
    )
    posttest_available = bool(
        ctx.get("posttest_available")
        or submitted_doc
        or timer.get("group_discussion_status") in {"timed_out", "submitted", "closed"}
    )
    return jsonify({
        **ctx,
        **timer,
        "status": "ok",
        "session_open": bool(ctx.get("is_running")),
        "group_id": group_id,
        "pretest_completed": pretest_completed,
        "posttest_available": posttest_available,
        "group_discussion": runtime,
    }), 200

@app.route("/api/student/questionnaires")
@login_required("student")
def api_student_questionnaires():
    """Get student questionnaires with items and responses."""
    user = current_user()
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify({
            "questionnaires": [],
            "status": "waiting_session",
            "message": "等待教师设置课时",
        })
    stage = request.args.get("stage", "pre").strip().lower()
    if stage not in QUESTIONNAIRE_STAGE_VALUES:
        stage = "pre"
    questionnaires = list_student_questionnaires(
        user["id"],
        session_id=session_ctx["session_id"],
        response_stage=stage,
    )
    post_checkin_completed = False
    if stage == "post":
        group_id = get_user_group_id(user["id"])
        post_checkin_completed = bool(query_one(
            "SELECT id FROM emotion_checkins "
            "WHERE user_id=? AND group_id=? AND session_id=? AND task_id=? "
            "AND checkin_type='post' LIMIT 1",
            (user["id"], group_id, session_ctx["session_id"], session_ctx["task_id"]),
        ))
    return jsonify({
        "questionnaires": questionnaires,
        "session": dict(session_ctx),
        "status": "ok",
        "post_checkin_completed": post_checkin_completed,
    })


@app.route("/api/student/questionnaires/<int:qid>/responses", methods=["POST"])
@login_required("student")
def api_student_submit_questionnaire(qid):
    """Submit questionnaire responses."""
    user = current_user()
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify({"error": "等待教师设置课时", "code": "waiting_session"}), 409
    data = request.get_json(force=True)
    response_stage = (data.get("response_stage") or "pre").strip().lower()
    if response_stage not in QUESTIONNAIRE_STAGE_VALUES:
        response_stage = "pre"
    responses = data.get("responses") or {}
    group_id = get_user_group_id(user["id"])
    if not group_id:
        return jsonify({"error": "no group"}), 400
    visible = list_published_questionnaires_for_student(
        user_id=user["id"],
        session_id=session_ctx["session_id"],
        response_stage=response_stage,
        group_id=group_id,
    )
    if not any(q["id"] == qid for q in visible):
        return jsonify({"error": "Questionnaire is not published for this session/group/user"}), 403
    try:
        result = create_questionnaire_submission(
            questionnaire_id=qid,
            user_id=user["id"],
            group_id=group_id,
            session_id=session_ctx["session_id"],
            session_no=session_ctx["session_no"],
            response_stage=response_stage,
            responses=responses,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/teacher/questionnaires")
@login_required("teacher")
def api_teacher_list_questionnaires():
    """Teacher list questionnaires with items and summary."""
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    return jsonify({"questionnaires": questionnaires})


@app.route("/api/teacher/questionnaires", methods=["POST"])
@login_required("teacher")
def api_teacher_create_questionnaire():
    """Teacher create questionnaire."""
    user = current_user()
    data = request.get_json(force=True)
    payload = {
        "code": data.get("code") or "q_" + now_str().replace(" ", "_").replace(":", ""),
        "category_key": data.get("category_key", "ssrl"),
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "timing": data.get("timing", "both"),
        "scale_max": int(data.get("scale_max", 5)),
        "active": bool(data.get("active", True)),
        "sort_order": int(data.get("sort_order", 0)),
        "created_by": user["id"],
    }
    items = data.get("items", [])
    qid = create_questionnaire(payload, items)
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    q = next((q for q in questionnaires if q["id"] == qid), None)
    return jsonify({"questionnaires": questionnaires, "questionnaire": q})


@app.route("/api/teacher/questionnaires/<int:qid>", methods=["PUT", "POST"])
@login_required("teacher")
def api_teacher_update_questionnaire(qid):
    """Teacher update questionnaire."""
    # Batch 6: reject edits on fixed questionnaires
    if is_fixed_questionnaire(qid):
        return jsonify({"error": "Fixed questionnaire cannot be edited", "code": "FIXED_QUESTIONNAIRE"}), 403
    data = request.get_json(force=True)
    payload = {
        "code": data.get("code", ""),
        "category_key": data.get("category_key", "ssrl"),
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "timing": data.get("timing", "both"),
        "scale_max": int(data.get("scale_max", 5)),
        "active": bool(data.get("active", True)),
        "sort_order": int(data.get("sort_order", 0)),
    }
    items = data.get("items", [])
    conn = db()
    try:
        conn.execute("""UPDATE questionnaires SET code=?, category_key=?, title=?, description=?,
            timing=?, scale_max=?, active=?, sort_order=?, updated_at=? WHERE id=?""",
        (payload["code"], payload["category_key"], payload["title"], payload["description"],
         payload["timing"], payload["scale_max"], 1 if payload["active"] else 0,
         payload["sort_order"], now_str(), qid))
        _replace_questionnaire_items(conn, qid, _normalize_questionnaire_items(items or []), now_str())
        conn.commit()
    finally:
        conn.close()
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    q = next((q for q in questionnaires if q["id"] == qid), None)
    return jsonify({"questionnaires": questionnaires, "questionnaire": q})


@app.route("/api/teacher/questionnaires/<int:qid>", methods=["DELETE"])
@login_required("teacher")
def api_teacher_delete_questionnaire(qid):
    """Teacher delete questionnaire and responses."""
    # Batch 6: reject deletion on fixed questionnaires
    if is_fixed_questionnaire(qid):
        return jsonify({"error": "Fixed questionnaire cannot be deleted", "code": "FIXED_QUESTIONNAIRE"}), 403
    conn = db()
    try:
        conn.execute("DELETE FROM questionnaire_responses WHERE questionnaire_id=?", (qid,))
        conn.execute("DELETE FROM questionnaire_items WHERE questionnaire_id=?", (qid,))
        conn.execute("DELETE FROM questionnaires WHERE id=?", (qid,))
        conn.commit()
    finally:
        conn.close()
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    return jsonify({"questionnaires": questionnaires})


@app.route("/api/teacher/questionnaires/<int:qid>/copy", methods=["POST"])
@login_required("teacher")
def api_teacher_copy_questionnaire(qid):
    """Copy questionnaire."""
    # Batch 6: reject copy on fixed questionnaires
    if is_fixed_questionnaire(qid):
        return jsonify({"error": "Fixed questionnaire cannot be copied", "code": "FIXED_QUESTIONNAIRE"}), 403
    q = query_one("SELECT * FROM questionnaires WHERE id=?", (qid,))
    if not q:
        return jsonify({"error": "not found"}), 404
    items = query_all("SELECT * FROM questionnaire_items WHERE questionnaire_id=?", (qid,))
    payload = dict(q)
    del payload["id"]
    payload["code"] = (payload.get("code") or "") + "_copy"
    payload["title"] = (payload.get("title") or "") + " (copy)"
    payload["active"] = False
    new_qid = create_questionnaire(
        {"code": payload["code"], "category_key": payload["category_key"],
         "title": payload["title"], "description": payload.get("description", ""),
         "timing": payload["timing"], "scale_max": payload["scale_max"],
         "active": payload["active"], "sort_order": payload["sort_order"]},
        [dict(it) for it in items],
    )
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    return jsonify({"questionnaires": questionnaires})



@app.route("/api/teacher/questionnaires/<int:qid>/active", methods=["POST"])
@login_required("teacher")
def api_teacher_toggle_questionnaire_active(qid):
    """Toggle a questionnaire active/inactive."""
    data = request.get_json(force=True)
    active = bool(data.get("active", True))
    execute("UPDATE questionnaires SET active=?, updated_at=? WHERE id=?", (1 if active else 0, now_str(), qid))
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    return jsonify({"questionnaires": questionnaires})

@app.route("/api/teacher/questionnaires/batch-active", methods=["POST"])
@login_required("teacher")
def api_teacher_batch_toggle_active():
    """Batch enable or disable questionnaires."""
    data = request.get_json(force=True)
    ids = data.get("questionnaire_ids", [])
    active = bool(data.get("active", True))
    if not ids:
        return jsonify({"error": "no ids"}), 400
    placeholders = ",".join(["?"] * len(ids))
    execute(f"UPDATE questionnaires SET active=?, updated_at=? WHERE id IN ({placeholders})",
        (1 if active else 0, now_str(), *ids))
    questionnaires = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
    return jsonify({"questionnaires": questionnaires})


@app.route("/api/teacher/questionnaires/<int:qid>/sort", methods=["POST"])
@login_required("teacher")
def api_teacher_update_sort_order(qid):
    """Update questionnaire sort order."""
    data = request.get_json(force=True)
    sort_order = int(data.get("sort_order", 0))
    execute("UPDATE questionnaires SET sort_order=?, updated_at=? WHERE id=?", (sort_order, now_str(), qid))
    return jsonify({"ok": True})


@app.route("/api/teacher/questionnaires/<int:qid>/responses/<int:gid>")
@login_required("teacher")
def api_teacher_questionnaire_responses(qid, gid):
    """Get individual responses for a questionnaire in a group."""
    q = query_one("SELECT * FROM questionnaires WHERE id=?", (qid,))
    if not q:
        return jsonify({"error": "not found"}), 404
    users = get_questionnaire_individual_responses(qid, gid)
    return jsonify({"questionnaire": dict(q), "users": users})


def _questionnaire_set_items_from_payload(data):
    return data.get("items") if "items" in data else data.get("questionnaire_ids")


def _questionnaire_sets_response(include_inactive=True):
    return jsonify({"questionnaire_sets": list_questionnaire_sets(include_inactive=include_inactive)})


@app.route("/api/teacher/questionnaire-sets", methods=["GET"])
@login_required("teacher")
def api_teacher_list_questionnaire_sets():
    """Teacher list reusable questionnaire sets."""
    include_inactive = str(request.args.get("include_inactive", "0")).lower() in ("1", "true", "yes")
    return _questionnaire_sets_response(include_inactive=include_inactive)


@app.route("/api/teacher/questionnaire-sets", methods=["POST"])
@login_required("teacher")
def api_teacher_create_questionnaire_set():
    """Teacher create a reusable questionnaire set."""
    user = current_user()
    data = request.get_json(force=True)
    try:
        set_id = create_questionnaire_set(
            data.get("name"),
            description=data.get("description", ""),
            created_by=user["id"],
            questionnaire_ids=_questionnaire_set_items_from_payload(data),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    sets = list_questionnaire_sets(include_inactive=True)
    qset = next((item for item in sets if item["id"] == set_id), None)
    return jsonify({"questionnaire_sets": sets, "questionnaire_set": qset}), 201


@app.route("/api/teacher/questionnaire-sets/<int:set_id>", methods=["PUT", "POST"])
@login_required("teacher")
def api_teacher_update_questionnaire_set(set_id):
    """Teacher update a reusable questionnaire set."""
    data = request.get_json(force=True)
    updates = {}
    for field in ("name", "description", "active"):
        if field in data:
            updates[field] = data.get(field)
    if "items" in data or "questionnaire_ids" in data:
        updates["questionnaire_ids"] = _questionnaire_set_items_from_payload(data)
    try:
        update_questionnaire_set(set_id, **updates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    sets = list_questionnaire_sets(include_inactive=True)
    qset = next((item for item in sets if item["id"] == set_id), None)
    return jsonify({"questionnaire_sets": sets, "questionnaire_set": qset})


@app.route("/api/teacher/questionnaire-sets/<int:set_id>", methods=["DELETE"])
@login_required("teacher")
def api_teacher_delete_questionnaire_set(set_id):
    """Teacher delete an unused questionnaire set or deactivate a used set."""
    try:
        result = delete_questionnaire_set(set_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    sets = list_questionnaire_sets(include_inactive=True)
    return jsonify({"ok": True, "result": result, "questionnaire_sets": sets})

# ============================================================
# Teacher task management API endpoints
# ============================================================

@app.route("/api/teacher/experiment-phases", methods=["GET"])
@login_required("teacher")
def api_teacher_list_experiment_phases():
    """List all experiment phases."""
    from db import list_experiment_phases
    phases = list_experiment_phases(active_only=False)
    return jsonify({"experiment_phases": phases})


@app.route("/api/teacher/experiment-phases", methods=["POST"])
@login_required("teacher")
def api_teacher_create_experiment_phase():
    """Create a new experiment phase."""
    data = request.get_json(force=True)
    from db import create_experiment_phase
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Phase name is required"}), 400
    description = data.get("description", "")
    default_agent_intervention_enabled = data.get("default_agent_intervention_enabled", 1)
    phase_id = create_experiment_phase(name, description, default_agent_intervention_enabled)
    return jsonify({"ok": True, "phase_id": phase_id})


def api_teacher_list_tasks():
    """List all learning tasks."""
    from db import list_learning_tasks, get_current_learning_task, get_current_session_no, list_experiment_phases
    tasks = list_learning_tasks()
    for t in tasks:
        t["active"] = True  # Tasks are always active
        if "is_active" in t:
            del t["is_active"]
    current_task = get_current_learning_task()
    if current_task:
        current_task["active"] = True
        if "is_active" in current_task:
            del current_task["is_active"]
    return jsonify({
        "tasks": tasks,
        "current_session_no": get_current_session_no(),
        "current_task": current_task,
        "experiment_phases": list_experiment_phases(active_only=False),
    })


@app.route("/api/teacher/tasks", methods=["GET"])
@login_required("teacher")
def api_teacher_get_tasks():
    """Get all learning tasks."""
    return api_teacher_list_tasks()


@app.route("/api/teacher/tasks", methods=["POST"])
@login_required("teacher")
def api_teacher_create_task():
    """Create a new learning task."""
    data = request.get_json(force=True)
    from db import create_learning_task
    create_learning_task(data)
    return api_teacher_list_tasks()


@app.route("/api/teacher/tasks/<int:task_id>", methods=["PUT"])
@login_required("teacher")
def api_teacher_update_task(task_id):
    """Update a learning task."""
    data = request.get_json(force=True)
    from db import update_learning_task, set_current_task
    update_learning_task(task_id, data)
    if data.get("active") and data.get("set_current"):
        set_current_task(task_id)
    return api_teacher_list_tasks()


@app.route("/api/teacher/tasks/<int:task_id>", methods=["DELETE"])
@login_required("teacher")
def api_teacher_delete_task(task_id):
    """Delete a learning task."""
    from db import delete_learning_task
    try:
        delete_learning_task(task_id)
        return api_teacher_list_tasks()
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/teacher/tasks/<int:task_id>/current", methods=["POST"])
@login_required("teacher")
def api_teacher_set_current_task(task_id):
    """Set a task as the current active task."""
    from db import set_current_task
    set_current_task(task_id)
    return api_teacher_list_tasks()


@app.route("/api/teacher/tasks/<int:task_id>/active", methods=["POST"])
@login_required("teacher")
def api_teacher_toggle_task_active(task_id):
    """[DEPRECATED] Toggle task active state.

    This endpoint is deprecated. Task enable/disable is no longer a concept;
    current task is determined by session binding. Kept for backward compatibility
    but may be removed in a future version.
    """
    data = request.get_json(force=True)
    active = bool(data.get("active", True))
    from db import update_learning_task
    update_learning_task(task_id, {"is_active": 1 if active else 0, "active": active})
    return api_teacher_list_tasks()


@app.route("/api/teacher/tasks/current-session", methods=["POST"])
@login_required("teacher")
def api_teacher_set_current_session():
    """Set current session number to an existing unique session."""
    data = request.get_json(force=True)
    try:
        session_no = int(data.get("session_no", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "session_no must be an integer"}), 400
    rows = query_all(
        "SELECT id, task_id, status FROM experiment_sessions WHERE session_no=? ORDER BY id ASC",
        (session_no,),
    )
    if not rows:
        return jsonify({"error": f"课时 {session_no} 不存在，请先创建课次"}), 404
    if len(rows) > 1:
        ids = ", ".join(str(row["id"]) for row in rows)
        return jsonify({"error": f"课时 {session_no} 存在重复课次（课次ID: {ids}），请先清理重复课次"}), 409
    from db import set_current_session, set_setting
    session = rows[0]
    set_current_session(session_no)
    set_setting("current_session_id", str(session["id"]) if session["status"] == "running" else "")
    if session["task_id"]:
        set_setting("current_task_id", str(session["task_id"]))
    return api_teacher_list_tasks()


# ============================================================
# Teacher group monitoring API endpoints
# ============================================================

@app.route("/api/teacher/groups")
@login_required("teacher")
def api_teacher_list_groups():
    """List all groups with dashboard rollup data."""
    from datetime import datetime, timedelta
    requested_session_id = request.args.get("session_id", type=int)
    scope_session_id = requested_session_id or get_active_session_id()
    if (
        request.args.get("all", "").lower() not in ("1", "true", "yes")
        and not requested_session_id
    ):
        return jsonify({
            "summary": {
                "group_count": 0,
                "high_risk_count": 0,
                "msg_count_10m": 0,
                "intervention_count": 0,
                "pending_suggestion_count": 0,
            },
            "groups": [],
        })
    groups = query_all("SELECT * FROM groups ORDER BY id ASC")
    group_list = []
    total_high_risk = 0
    total_msg_10m = 0
    total_interventions = 0
    total_pending_suggestions = 0

    for g in groups:
        g = dict(g)
        gid = g["id"]
        state = _read_group_state_v2(
            gid,
            session_id=scope_session_id,
        )
        member_count = get_group_member_count(gid)
        suggestion = get_latest_agent_suggestion(gid)
        if suggestion is not None and not isinstance(suggestion, dict):
            suggestion = dict(suggestion)
        last_intervention = get_group_latest_intervention_log(gid)

        recent_cutoff = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        if scope_session_id:
            msg_10m = query_one(
                """
                SELECT COUNT(*) AS c
                FROM messages
                WHERE group_id=? AND session_id=? AND created_at>=?
                """,
                (gid, scope_session_id, recent_cutoff),
            )
        else:
            msg_10m = query_one(
                "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND created_at>=?",
                (gid, recent_cutoff),
            )
        msg_count = int(msg_10m["c"]) if msg_10m else 0
        total_msg_10m += msg_count

        if last_intervention:
            total_interventions += 1

        if suggestion and suggestion.get("status") == "pending":
            total_pending_suggestions += 1

        risk_level = int(state.get("risk_level", 0)) if state else 0
        if risk_level == 3:
            total_high_risk += 1

        if scope_session_id:
            last_msg = query_one(
                """
                SELECT created_at
                FROM messages
                WHERE group_id=? AND session_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (gid, scope_session_id),
            )
        else:
            last_msg = query_one(
                "SELECT created_at FROM messages WHERE group_id=? ORDER BY id DESC LIMIT 1",
                (gid,),
            )

        current_task = get_current_learning_task()

        group_list.append({
            "group_id": gid,
            "group_name": g["name"],
            "group_code": g.get("group_code") or "",
            "condition": g.get("condition") or "experiment",
            "risk_level": risk_level,
            "final_sub_state_code": state.get("final_sub_state_code"),
            "final_sub_state_label": state.get("final_sub_state_label"),
            "display_state_code": state.get("display_state_code"),
            "display_state_label": state.get("display_state_label"),
            # Deprecated aliases deliberately mirror the canonical/process
            # display state for older dashboard clients.
            "state_code": state.get("state_code", "unclassified"),
            "state_label": state.get("state_label", "") if state else "",
            "coarse_state_code": state.get("coarse_state_code"),
            "coarse_state_label": state.get("coarse_state_label"),
            "legacy_state_code": state.get("legacy_state_code"),
            "risk_label": state.get("risk_label", "") if state else "",
            "confidence": float(state.get("confidence", 0)) if state and state.get("confidence") is not None else None,
            "assessment_status": state.get("assessment_status", "") if state else "",
            "assignment_source": state.get("assignment_source"),
            "inferred": bool(state.get("inferred")),
            "active_silence": state.get("active_silence") or {"active": False},
            "member_count": member_count,
            "active_member_count": 0,
            "msg_count_10m": msg_count,
            "state_duration_seconds": 0,
            "last_message_at": last_msg["created_at"] if last_msg else "",
            "updated_at": g.get("created_at", ""),
            "current_session_no": get_current_session_no(),
            "session_id": scope_session_id,
            "current_task_title": current_task.get("title", "") if current_task else "",
            "auto_intervention_enabled": bool(g.get("auto_intervention_enabled", 1)),
            "evidence": state.get("evidence", "") if state else "",
            "confirmed_windows": int(state.get("confirmed_windows", 0)) if state else 0,
            "confirmation_status": state.get("confirmation_status", "") if state else "",
            "auxiliary_flags": [],
            "questionnaire_progress": {},
            "submission_overview": {"submission_count": 0, "scored_count": 0},
            "latest_submission": None,
            "latest_intervention": dict(last_intervention) if last_intervention else None,
            "agent_suggestion": dict(suggestion) if suggestion else None,
        })

    return jsonify({
        "summary": {
            "group_count": len(group_list),
            "high_risk_count": total_high_risk,
            "msg_count_10m": total_msg_10m,
            "intervention_count": total_interventions,
            "pending_suggestion_count": total_pending_suggestions,
        },
        "groups": group_list,
    })


@app.route("/api/teacher/group/<int:group_id>/detail")
@login_required("teacher")
def api_teacher_group_detail(group_id):
    """Get detailed data for a single group."""
    group = query_one("SELECT * FROM groups WHERE id=?", (group_id,))
    if not group:
        return jsonify({"error": "not found"}), 404

    suggestion = get_latest_agent_suggestion(group_id)
    if suggestion is not None and not isinstance(suggestion, dict):
        suggestion = dict(suggestion)
    last_intervention = get_group_latest_intervention_log(group_id)
    requested_session_id = request.args.get("session_id", type=int)
    scope_session_id = requested_session_id or get_active_session_id()
    if scope_session_id:
        assessments = query_all(
            """
            SELECT *
            FROM state_assessments
            WHERE group_id=? AND session_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (group_id, scope_session_id),
        )
    else:
        assessments = []
    from services.teacher_emotion_trend_service import get_emotion_trend

    canonical_trend = get_emotion_trend(
        group_id=group_id,
        session_id=scope_session_id,
        window_minutes=0,
        include_legacy_scope=False,
    )
    if canonical_trend.get("error"):
        canonical_trend = {
            "current_state": {
                "final_sub_state_code": None,
                "final_sub_state_label": None,
                "state_code": "unclassified",
                "state_label": "未分类",
                "assessment_status": "unclassified",
                "assignment_source": "canonical_read_error",
            },
            "state_segments": [],
            "message_assignment_summary": {},
            "active_silence": {"active": False},
        }
    current_task = get_current_learning_task()
    sub_count = query_one(
        "SELECT COUNT(*) AS c FROM submissions WHERE group_id=?",
        (group_id,),
    )

    return jsonify({
        "group": dict(group),
        "questionnaireProgress": {},
        "submissionOverview": {
            "submission_count": int(sub_count["c"]) if sub_count else 0,
            "scored_count": 0,
        },
        "latest_suggestion": dict(suggestion) if suggestion else None,
        "latest_intervention": dict(last_intervention) if last_intervention else None,
        "latest_decision": None,
        "current_state": canonical_trend.get("current_state"),
        "canonical_segments": canonical_trend.get("state_segments") or [],
        "message_assignment_summary": (
            canonical_trend.get("message_assignment_summary") or {}
        ),
        "active_silence": canonical_trend.get("active_silence")
        or {"active": False},
        "recent_coarse_assessments": [
            dict(a) for a in (assessments or [])
        ],
        "session_id": scope_session_id,
        "current_session_no": get_current_session_no(),
        "current_task": current_task,
    })


@app.route("/api/teacher/group/<int:group_id>/condition", methods=["POST"])
@login_required("teacher")
def api_teacher_update_group_condition(group_id):
    """DEPRECATED: Condition must be set via assignment/freeze workflow."""
    return jsonify({
        "error": "This endpoint is deprecated. Use POST /api/teacher/assignment/run + freeze.",
        "code": "ENDPOINT_DEPRECATED",
    }), 410


@app.route("/api/teacher/group/<int:group_id>/auto-intervention", methods=["POST"])
@login_required("teacher")
def api_teacher_toggle_auto_intervention(group_id):
    """Toggle auto intervention on/off for a group."""
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", True))
    execute("UPDATE groups SET auto_intervention_enabled=? WHERE id=?", (1 if enabled else 0, group_id))
    return jsonify({"ok": True})


# ============================================================
# Agent / SERA suggestion API endpoints
# ============================================================

@app.route("/api/agent/suggestion/<int:suggestion_id>/push", methods=["POST"])
@login_required("teacher")
def api_agent_push_suggestion(suggestion_id):
    """Manually push a SERA suggestion to the group."""
    if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
        return jsonify({
            "error": "legacy suggestion push is disabled; use the three-stage intervention publisher",
            "code": "LEGACY_SUGGESTION_PUSH_DISABLED",
        }), 410
    user = current_user()
    from agent import push_agent_suggestion
    result = push_agent_suggestion(suggestion_id, user["id"])
    if result is None:
        return jsonify({"error": "cannot push or suggestion not found"}), 400
    return jsonify({"ok": True, "log_id": result})


@app.route("/api/agent/suggestion/<int:suggestion_id>/ignore", methods=["POST"])
@login_required("teacher")
def api_agent_ignore_suggestion(suggestion_id):
    """Ignore a pending SERA suggestion."""
    user = current_user()
    from agent import ignore_agent_suggestion
    ignore_agent_suggestion(suggestion_id, user["id"])
    return jsonify({"ok": True})


@app.route("/api/group/<int:group_id>/agent/analyze", methods=["POST"])
@login_required("teacher")
def api_group_analyze(group_id):
    """Deprecated developer-only V1 analysis entry point."""
    if not LEGACY_GROUP_ANALYZE_ENABLED:
        return jsonify({
            "error": "legacy analyze endpoint disabled",
            "code": "LEGACY_ANALYZE_DISABLED",
        }), 403
    from agent import analyze_group
    result = analyze_group(group_id)
    return jsonify({"ok": True, "result": result})


# ============================================================
# Experiment reset
# ============================================================

@app.route("/api/teacher/experiment/reset", methods=["POST"])
@login_required("teacher")
def api_teacher_experiment_reset():
    """Reset all experiment data."""
    data = request.get_json(force=True)
    clear_files = bool(data.get("clear_files", False))
    from db import reset_experiment_data
    ok = reset_experiment_data(clear_files=clear_files)
    return jsonify({"ok": ok})
 
@app.route("/api/teacher/roster")
@login_required("teacher")
def api_teacher_roster():
    """Return JSON roster of all users (id, username, real_name, participant_code, role, group, condition, created_at)."""
    rows = query_all("""
        SELECT u.id, u.participant_code,
               COALESCE(ep.display_name, '') AS display_name,
               g.group_code,
               COALESCE(ep.group_no, '') AS group_no,
               COALESCE(ep.member_no, '') AS member_no,
               u.role, g.condition, u.created_at
        FROM users u
        LEFT JOIN group_members gm ON gm.user_id=u.id
        LEFT JOIN groups g ON gm.group_id=g.id
        LEFT JOIN experiment_participants ep ON u.id = ep.user_id AND gm.group_id = ep.group_id
        WHERE u.role IN ('student', 'teacher', 'agent')
        ORDER BY u.id ASC
    """)
    return jsonify({"users": [dict(r) for r in rows]})



# ============================================================
# Batch 6: Fixed questionnaire library, publications, completion
# ============================================================

@app.route("/api/teacher/questionnaires/fixed", methods=["GET"])
@login_required("teacher")
def api_teacher_list_fixed_questionnaires():
    """List fixed questionnaires (is_fixed=1) with details."""
    try:
        questionnaires = list_fixed_questionnaires()
        return jsonify({"questionnaires": questionnaires})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/questionnaire-publications", methods=["GET"])
@login_required("teacher")
def api_teacher_list_publications():
    """List all questionnaire publications."""
    try:
        publications = list_questionnaire_publications()
        return jsonify({"publications": publications})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/questionnaire-publications", methods=["POST"])
@login_required("teacher")
def api_teacher_create_publication():
    """Create a questionnaire publication for a session/stage."""
    data = request.get_json(force=True)
    questionnaire_id = data.get("questionnaire_id")
    session_id = data.get("session_id")
    session_no = data.get("session_no")
    response_stage = data.get("response_stage", "pre")
    group_id = data.get("group_id")
    user_id = data.get("user_id")

    if not all([questionnaire_id, session_id, session_no]):
        return jsonify({"error": "questionnaire_id, session_id, and session_no are required"}), 400

    try:
        pub_id = create_questionnaire_publication(
            questionnaire_id=int(questionnaire_id),
            session_id=int(session_id),
            session_no=int(session_no),
            response_stage=response_stage,
            group_id=group_id,
            user_id=user_id,
        )
        return jsonify({"ok": True, "publication_id": pub_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/teacher/questionnaire-publications/<int:pid>", methods=["PUT"])
@login_required("teacher")
def api_teacher_update_publication(pid):
    """Update publication status (enabled/closed)."""
    data = request.get_json(force=True)
    status = data.get("status", "enabled")
    try:
        update_questionnaire_publication(pid, status)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route("/api/teacher/questionnaire-publications/<int:pid>", methods=["DELETE"])
@login_required("teacher")
def api_teacher_delete_publication(pid):
    """Delete a publication. If submissions exist, closes it instead."""
    try:
        delete_questionnaire_publication(pid)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": True, "note": str(e)})

@app.route("/api/teacher/questionnaire-completion", methods=["GET"])
@login_required("teacher")
def api_teacher_questionnaire_completion():
    """Get completion statistics by session/questionnaire/stage/group."""
    session_id = request.args.get("session_id", type=int)
    questionnaire_id = request.args.get("questionnaire_id", type=int)
    try:
        stats = list_questionnaire_completion(
            session_id=session_id,
            questionnaire_id=questionnaire_id,
        )
        return jsonify({"stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/student/published-questionnaires", methods=["GET"])
@login_required("student")
def api_student_published_questionnaires():
    """Get questionnaires published for the current session and stage."""
    user = current_user()
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify({
            "questionnaires": [],
            "status": "waiting_session",
            "message": "等待教师设置课时",
        })
    stage = request.args.get("stage", "pre").strip().lower()
    if stage not in QUESTIONNAIRE_STAGE_VALUES:
        stage = "pre"
    try:
        questionnaires = list_published_questionnaires_for_student(
            user_id=user["id"],
            session_id=session_ctx["session_id"],
            response_stage=stage,
            group_id=get_user_group_id(user["id"]),
        )
        # Attach items for each questionnaire
        for q in questionnaires:
            items = query_all(
                "SELECT * FROM questionnaire_items WHERE questionnaire_id=? ORDER BY section_no ASC, sort_order ASC, id ASC",
                (q["id"],),
            )
            q["items"] = [_sanitize_student_item(it) for it in items]
            # Check if already submitted
            sub = query_one(
                "SELECT id, status FROM questionnaire_submissions WHERE questionnaire_id=? AND user_id=? AND session_id=? AND response_stage=?",
                (q["id"], user["id"], session_ctx["session_id"], stage),
            )
            q["submitted"] = bool(sub and sub["status"] == "submitted")
            q["submission_id"] = sub["id"] if sub else None
        return jsonify({"questionnaires": questionnaires, "session": dict(session_ctx), "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/student/questionnaires/<int:qid>/submit", methods=["POST"])
@login_required("student")
def api_student_submit_published_questionnaire(qid):
    """Submit a published questionnaire response transactionally."""
    user = current_user()
    from db import get_current_running_session_context
    session_ctx = get_current_running_session_context()
    if not session_ctx:
        return jsonify({"error": "等待教师设置课时", "code": "waiting_session"}), 409
    data = request.get_json(force=True)
    response_stage = (data.get("response_stage") or "pre").strip().lower()
    responses = data.get("responses") or {}
    group_id = get_user_group_id(user["id"])
    if not group_id:
        return jsonify({"error": "no group"}), 400

    visible = list_published_questionnaires_for_student(
        user_id=user["id"],
        session_id=session_ctx["session_id"],
        response_stage=response_stage,
        group_id=group_id,
    )
    if not any(q["id"] == qid for q in visible):
        return jsonify({"error": "Questionnaire is not published for this session/group/user"}), 403

    try:
        result = create_questionnaire_submission(
            questionnaire_id=qid,
            user_id=user["id"],
            group_id=group_id,
            session_id=session_ctx["session_id"],
            session_no=session_ctx["session_no"],
            response_stage=response_stage,
            responses=responses,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ============================================================
# End Batch 6 API routes
# ============================================================
