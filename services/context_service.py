# -*- coding: utf-8 -*-
"""Context collection helpers for the SSRL-ESP analysis pipeline."""
import json
from datetime import datetime, timedelta

from config import (
    AGENT_CONTEXT_MINUTES,
    CHECKIN_VALID_WINDOW_MINUTES,
    ONLINE_ACTIVE_SECONDS,
    ONLINE_LOW_INTERACTION_MINUTES,
    STATE_WINDOW_MINUTES,
)
from db import (
    get_current_learning_task,
    get_current_session_no,
    get_learning_task,
    now_str,
    parse_dt,
    query_all,
    query_one,
    get_active_session_id,
)

HELP_REQUEST_TRIGGER_SOURCES = (
    "student_invoked_help",
    "student_agent_call",
    "student_help_request",
    "student_manual_help",
)


def _row_to_dict(row):
    return dict(row) if row else None


def _loads_json(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _normalize_message_row(row):
    data = dict(row)
    data["role"] = data.pop("resolved_role", None) or data.get("role") or "student"
    return data


def _load_message_rows(
    group_id,
    *,
    since_text=None,
    limit=None,
    student_only=False,
    session_id=None,
    task_id=None,
    session_no=None,
    discussion_id=None,
):
    clauses = ["m.group_id=?"]
    params = [group_id]
    if since_text:
        clauses.append("m.created_at>=?")
        params.append(since_text)
    if session_id is not None:
        clauses.append("m.session_id=?")
        params.append(session_id)
    if discussion_id is not None:
        clauses.append("m.discussion_id=?")
        params.append(discussion_id)
    if task_id is not None:
        clauses.append("m.task_id=?")
        params.append(task_id)
    if session_no is not None and session_id is None:
        clauses.append("m.session_no=?")
        params.append(session_no)
    if student_only:
        clauses.append("u.role='student'")
    sql = f"""
        SELECT m.*, u.real_name, u.username, u.participant_code,
               g.group_code, g.condition,
               COALESCE(NULLIF(TRIM(m.role), ''), u.role) AS resolved_role
        FROM messages m
        JOIN users u ON m.user_id = u.id
        JOIN groups g ON m.group_id = g.id
        WHERE {' AND '.join(clauses)}
        ORDER BY m.id DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = query_all(sql, tuple(params))
    return list(reversed([_normalize_message_row(row) for row in rows]))


def _collect_recent_checkins(group_id, since_text, session_id=None):
    rows = [
        dict(row)
        for row in query_all(
            """
            SELECT *
            FROM emotion_checkins
            WHERE group_id=? AND created_at>=? AND (? IS NULL OR session_id=?)
              AND COALESCE(checkin_type, 'post') IN ('mid', 'event')
            ORDER BY created_at DESC
            """,
            (group_id, since_text, session_id, session_id),
        )
    ]
    if not rows:
        return rows, {
            "count": 0,
            "avg_positivity": 3.0,
            "avg_engagement": 3.0,
            "avg_atmosphere": 3.0,
            "avg_expression": 3.0,
            "dominant_option": "none",
        }
    options = [row.get("emotion_option") or "none" for row in rows]
    dominant_option = max(set(options), key=options.count)
    return rows, {
        "count": len(rows),
        "avg_positivity": round(sum(int(row.get("positivity") or 0) for row in rows) / len(rows), 2),
        "avg_engagement": round(sum(int(row.get("engagement") or 0) for row in rows) / len(rows), 2),
        "avg_atmosphere": round(sum(int(row.get("atmosphere") or 0) for row in rows) / len(rows), 2),
        "avg_expression": round(sum(int(row.get("expression_willingness") or 0) for row in rows) / len(rows), 2),
        "dominant_option": dominant_option,
    }


def _collect_page_activity(group_id, active_seconds):
    rows = [
        dict(row)
        for row in query_all(
            """
            SELECT cs.token, cs.user_id, cs.role, cs.created_at, cs.last_seen,
                   u.username, u.real_name, u.participant_code
            FROM client_sessions cs
            JOIN group_members gm ON gm.user_id = cs.user_id
            JOIN users u ON u.id = cs.user_id
            WHERE gm.group_id=? AND u.role='student'
            ORDER BY cs.last_seen DESC, cs.created_at DESC
            """,
            (group_id,),
        )
    ]
    threshold = datetime.now() - timedelta(seconds=active_seconds)
    active_rows = []
    for row in rows:
        last_seen_dt = parse_dt(row.get("last_seen"))
        if last_seen_dt and last_seen_dt >= threshold:
            active_rows.append(row)
    active_user_ids = sorted({row["user_id"] for row in active_rows})
    active_codes = sorted({row["participant_code"] for row in active_rows if row.get("participant_code")})
    last_seen_values = [parse_dt(row.get("last_seen")) for row in active_rows if row.get("last_seen")]
    created_values = [parse_dt(row.get("created_at")) for row in active_rows if row.get("created_at")]
    latest_last_seen = max(last_seen_values) if last_seen_values else None
    earliest_created = min(created_values) if created_values else None
    active_duration_seconds = None
    if earliest_created:
        active_duration_seconds = max(0, int((datetime.now() - earliest_created).total_seconds()))
    return {
        "active_seconds_window": active_seconds,
        "active_students": len(active_user_ids),
        "active_user_ids": active_user_ids,
        "active_participant_codes": active_codes,
        "session_count": len(rows),
        "active_session_count": len(active_rows),
        "first_seen": earliest_created.strftime("%Y-%m-%d %H:%M:%S") if earliest_created else None,
        "last_seen": latest_last_seen.strftime("%Y-%m-%d %H:%M:%S") if latest_last_seen else None,
        "active_duration_seconds": active_duration_seconds,
        "rows": active_rows,
    }


def _collect_participants(group_id, window_student_rows, recent_student_rows, current_task_id, current_session_no, page_activity):
    student_rows = [
        dict(row)
        for row in query_all(
            """
            SELECT u.id, u.username, u.real_name, u.participant_code
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id=? AND u.role='student'
            ORDER BY u.id ASC
            """,
            (group_id,),
        )
    ]
    window_counts = {}
    recent_counts = {}
    session_counts = {}
    last_message_at = {}
    for row in window_student_rows:
        user_id = row["user_id"]
        window_counts[user_id] = window_counts.get(user_id, 0) + 1
        if row.get("task_id") == current_task_id and row.get("session_no") == current_session_no:
            session_counts[user_id] = session_counts.get(user_id, 0) + 1
        if row.get("created_at"):
            last_message_at[user_id] = row["created_at"]
    for row in recent_student_rows:
        user_id = row["user_id"]
        recent_counts[user_id] = recent_counts.get(user_id, 0) + 1
        if row.get("created_at") and user_id not in last_message_at:
            last_message_at[user_id] = row["created_at"]
    active_user_ids = set(page_activity.get("active_user_ids") or [])
    active_session_counts = {}
    for row in page_activity.get("rows") or []:
        active_session_counts[row["user_id"]] = active_session_counts.get(row["user_id"], 0) + 1
    participants = []
    for student in student_rows:
        user_id = student["id"]
        participants.append(
            {
                "user_id": user_id,
                "username": student["username"],
                "real_name": student["real_name"],
                "participant_code": student.get("participant_code"),
                "message_count_10m": window_counts.get(user_id, 0),
                "recent_message_count": recent_counts.get(user_id, 0),
                "session_task_message_count": session_counts.get(user_id, 0),
                "last_message_at": last_message_at.get(user_id),
                "active_on_page": user_id in active_user_ids,
                "active_session_count": active_session_counts.get(user_id, 0),
            }
        )
    return participants


def _collect_current_progress(group_id, task_id, session_no, window_start_text):
    latest = _row_to_dict(
        query_one(
            """
            SELECT s.*, u.participant_code, u.username, g.group_code, g.condition
            FROM submissions s
            JOIN users u ON u.id = s.user_id
            JOIN groups g ON g.id = s.group_id
            WHERE s.group_id=?
              AND (? IS NULL OR s.task_id=?)
              AND (? IS NULL OR s.session_no=?)
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (group_id, task_id, task_id, session_no, session_no),
        )
    )
    aggregate = _row_to_dict(
        query_one(
            """
            SELECT COUNT(*) AS submission_count,
                   SUM(CASE WHEN s.created_at>=? THEN 1 ELSE 0 END) AS recent_submission_count,
                   MAX(LENGTH(COALESCE(s.content, ''))) AS max_content_length
            FROM submissions s
            WHERE s.group_id=?
              AND (? IS NULL OR s.task_id=?)
              AND (? IS NULL OR s.session_no=?)
            """,
            (window_start_text, group_id, task_id, task_id, session_no, session_no),
        )
    ) or {}
    latest_age_seconds = None
    if latest and latest.get("created_at"):
        latest_dt = parse_dt(latest["created_at"])
        if latest_dt:
            latest_age_seconds = max(0, int((datetime.now() - latest_dt).total_seconds()))
        latest["content_preview"] = (latest.get("content") or "")[:200]
        latest["content_length"] = len(latest.get("content") or "")
        latest["has_file"] = bool(latest.get("stored_file_name"))
    return {
        "submission_count": int(aggregate.get("submission_count") or 0),
        "recent_submission_count": int(aggregate.get("recent_submission_count") or 0),
        "max_content_length": int(aggregate.get("max_content_length") or 0),
        "latest_submission": latest,
        "latest_submission_age_seconds": latest_age_seconds,
        "has_submission": bool(latest),
    }


_RECENT_STATE_SUMMARY_FIELDS = (
    "id",
    "group_id",
    "session_id",
    "session_no",
    "discussion_id",
    "task_id",
    "state_assessment_id",
    "state_code",
    "state_label",
    "llm_state_code",
    "assessment_status",
    "confirmation_status",
    "confirmed_windows",
    "risk_level",
    "risk_label",
    "state_score",
    "created_at",
)


def _collect_recent_state(group_id, session_id=None, discussion_id=None):
    where = "group_id=?"
    params = [group_id]
    if session_id is not None:
        where += " AND session_id=?"
        params.append(session_id)
    if discussion_id is not None:
        where += " AND discussion_id=?"
        params.append(discussion_id)
    row = _row_to_dict(
        query_one(
            f"""
            SELECT {", ".join(_RECENT_STATE_SUMMARY_FIELDS)}
            FROM group_states
            WHERE {where}
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(params),
        )
    )
    if not row:
        return None
    # Only state identity and status are needed by downstream context consumers.
    # Returning the previous context/rule/feature payloads duplicates a full
    # monitoring snapshot into every later snapshot and makes the DB grow
    # quadratically across a discussion.
    return {
        field: row.get(field)
        for field in _RECENT_STATE_SUMMARY_FIELDS
        if field in row
    }


def _collect_recent_intervention(group_id, session_id=None, discussion_id=None):
    where = "group_id=?"
    params = [group_id]
    if session_id is not None:
        where += " AND session_id=?"
        params.append(session_id)
    if discussion_id is not None:
        where += " AND discussion_id=?"
        params.append(discussion_id)
    return _row_to_dict(
        query_one(
            f"""
            SELECT *
            FROM intervention_logs
            WHERE {where}
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(params),
        )
    )


def _collect_recent_help_request(
    group_id,
    session_id=None,
    discussion_id=None,
):
    placeholders = ",".join(["?"] * len(HELP_REQUEST_TRIGGER_SOURCES))
    scope_clauses = []
    scope_params = []
    if session_id is not None:
        scope_clauses.append("hr.session_id=?")
        scope_params.append(session_id)
    if discussion_id is not None:
        scope_clauses.append("hr.discussion_id=?")
        scope_params.append(discussion_id)
    scope_sql = "".join(f" AND {clause}" for clause in scope_clauses)
    return _row_to_dict(
        query_one(
            f"""
            SELECT suggestion.*
            FROM agent_suggestions AS suggestion
            LEFT JOIN help_requests AS hr
              ON hr.id=suggestion.help_request_id
            WHERE suggestion.group_id=?
              AND suggestion.trigger_source IN ({placeholders})
              {scope_sql}
            ORDER BY suggestion.id DESC
            LIMIT 1
            """,
            (group_id, *HELP_REQUEST_TRIGGER_SOURCES, *scope_params),
        )
    )


def collect_group_context(
    group_id,
    task_id=None,
    session_no=None,
    session_id=None,
    discussion_id=None,
):
    now_dt = datetime.now()
    resolved_session_no = max(1, int(session_no or get_current_session_no()))
    resolved_session_id = session_id if session_id is not None else get_active_session_id()
    task = get_learning_task(int(task_id)) if task_id else get_current_learning_task()
    resolved_task_id = task["id"] if task else None
    window_start_dt = now_dt - timedelta(minutes=STATE_WINDOW_MINUTES)
    low_window_start_dt = now_dt - timedelta(minutes=ONLINE_LOW_INTERACTION_MINUTES)
    checkin_start_dt = now_dt - timedelta(minutes=CHECKIN_VALID_WINDOW_MINUTES)
    context_start_dt = now_dt - timedelta(minutes=AGENT_CONTEXT_MINUTES)
    window_start_text = window_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    low_window_start_text = low_window_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    checkin_start_text = checkin_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    context_start_text = context_start_dt.strftime("%Y-%m-%d %H:%M:%S")

    window_messages = _load_message_rows(
        group_id,
        since_text=window_start_text,
        task_id=resolved_task_id,
        session_id=resolved_session_id,
        session_no=resolved_session_no,
        discussion_id=discussion_id,
    )
    low_window_student_messages = _load_message_rows(
        group_id,
        since_text=low_window_start_text,
        student_only=True,
        task_id=resolved_task_id,
        session_id=resolved_session_id,
        session_no=resolved_session_no,
        discussion_id=discussion_id,
    )
    recent_student_messages = _load_message_rows(
        group_id,
        since_text=context_start_text,
        limit=15,
        student_only=True,
        task_id=resolved_task_id,
        session_id=resolved_session_id,
        session_no=resolved_session_no,
        discussion_id=discussion_id,
    )
    window_student_messages = [row for row in window_messages if row.get("role") == "student"]
    recent_checkins, checkin_summary = _collect_recent_checkins(group_id, checkin_start_text, session_id=resolved_session_id)
    page_activity = _collect_page_activity(group_id, ONLINE_ACTIVE_SECONDS)
    participants = _collect_participants(
        group_id,
        window_student_messages,
        recent_student_messages,
        resolved_task_id,
        resolved_session_no,
        page_activity,
    )
    latest_student_message = window_student_messages[-1] if window_student_messages else (
        recent_student_messages[-1] if recent_student_messages else None
    )
    student_message_count_session = int(
        _row_to_dict(
            query_one(
                """
                SELECT COUNT(*) AS c
                FROM messages m
                JOIN users u ON u.id = m.user_id
                WHERE m.group_id=? AND u.role='student'
                  AND (? IS NULL OR m.task_id=?)
                  AND (? IS NULL OR m.session_id=?)
                  AND (? IS NULL OR m.session_no=?)
                  AND (? IS NULL OR m.discussion_id=?)
                """,
                (
                    group_id,
                    resolved_task_id,
                    resolved_task_id,
                    resolved_session_id,
                    resolved_session_id,
                    resolved_session_no,
                    resolved_session_no,
                    discussion_id,
                    discussion_id,
                ),
            )
        )["c"]
        or 0
    )
    current_progress = _collect_current_progress(group_id, resolved_task_id, resolved_session_no, window_start_text)
    return {
        "context_version": "phase5_v1",
        "group_id": group_id,
        "task_id": resolved_task_id,
        "session_id": resolved_session_id,
        "session_no": resolved_session_no,
        "discussion_id": discussion_id,
        "window_minutes": STATE_WINDOW_MINUTES,
        "window_start": window_start_text,
        "window_end": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "current_task": task,
        "window_messages": window_messages,
        "window_student_messages": window_student_messages,
        "recent_student_messages": recent_student_messages,
        "low_window_student_messages": low_window_student_messages,
        "recent_checkins": recent_checkins,
        "checkin_summary": checkin_summary,
        "participants": participants,
        "participant_count": len(participants),
        "active_member_count": page_activity.get("active_students", 0),
        "student_message_count_session": student_message_count_session,
        "last_student_message_time": latest_student_message.get("created_at") if latest_student_message else None,
        "last_activity_time": page_activity.get("last_seen") or (latest_student_message.get("created_at") if latest_student_message else None),
        "recent_state": _collect_recent_state(
            group_id,
            resolved_session_id,
            discussion_id,
        ),
        "recent_intervention": _collect_recent_intervention(
            group_id,
            resolved_session_id,
            discussion_id,
        ),
        "current_progress": current_progress,
        "page_activity": page_activity,
        "recent_help_request": _collect_recent_help_request(
            group_id,
            resolved_session_id,
            discussion_id,
        ),
        "server_time": now_str(),
    }
