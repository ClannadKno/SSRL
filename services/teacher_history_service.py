# -*- coding: utf-8 -*-
"""T7: Teacher history query service.

Queries historical data without triggering LLM or new detection.
Supports filtering by group_id, session_id, task_id, time range, data_type.
All SQL is parameterised.
"""

from db import query_all, query_one, now_str


def query_history(group_id=None, session_id=None, task_id=None,
                   start_time=None, end_time=None, data_type="messages",
                   blind=True):
    """Query historical data for the teacher history page.

    Does NOT trigger LLM or new detection - reads from existing tables only.

    Args:
        group_id: optional group filter
        session_id: optional session filter
        task_id: optional task filter
        start_time: optional ISO datetime start filter
        end_time: optional ISO datetime end filter
        data_type: one of messages/detector_outputs/interventions/uptake/
                   ssrl_events/deliverables/scores/surveys/audit_logs
        blind: if True, hide condition and agent content

    Returns:
        List of dict rows matching the query
    """
    handler = DATA_TYPE_MAP.get(data_type)
    if not handler:
        return {"error": "Unknown data_type: %s" % data_type}

    rows = handler(group_id=group_id, session_id=session_id, task_id=task_id,
                    start_time=start_time, end_time=end_time, blind=blind)
    return rows


def _build_time_clause(params, start_time, end_time, column="created_at"):
    """Append time filter conditions to a WHERE clause list and params list."""
    clauses = []
    if start_time:
        clauses.append("%s >= ?" % column)
        params.append(start_time)
    if end_time:
        clauses.append("%s <= ?" % column)
        params.append(end_time)
    return clauses


def _query_messages(group_id=None, session_id=None, task_id=None,
                     start_time=None, end_time=None, blind=True):
    sql = """SELECT m.id, u.participant_code, g.group_code,
                    g.condition, m.session_no, m.task_id, m.session_id,
                    COALESCE(NULLIF(TRIM(m.role), ''), u.role) AS role,
                    m.strategy_id, m.reply_to_message_id, m.client_message_id,
                    m.linked_log_id, m.content, m.created_at
             FROM messages m
             JOIN groups g ON m.group_id=g.id
             JOIN users u ON m.user_id=u.id
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND m.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND m.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND m.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "m.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY m.id ASC"
    rows = query_all(sql, tuple(params))
    if blind:
        rows = [_blind_row(r, {"condition", "content"}) for r in rows]
    return [dict(r) for r in rows]


def _query_detector_outputs(group_id=None, session_id=None, task_id=None,
                             start_time=None, end_time=None, blind=True):
    sql = """SELECT sa.*
             FROM state_assessments sa
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND sa.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND sa.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND sa.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "sa.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY sa.id ASC"
    rows = query_all(sql, tuple(params))
    if blind:
        rows = [_blind_row(r, {"condition"}) for r in rows]
    return [dict(r) for r in rows]


def _query_interventions(group_id=None, session_id=None, task_id=None,
                          start_time=None, end_time=None, blind=True):
    sql = """SELECT il.*, g.group_code, g.condition AS group_condition
             FROM intervention_logs il
             JOIN groups g ON il.group_id=g.id
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND il.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND il.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND il.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "il.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY il.id ASC"
    rows = query_all(sql, tuple(params))
    if blind:
        blind_fields = {"condition", "group_condition", "message", "template_id", "strategy_id"}
        rows = [_blind_row(r, blind_fields) for r in rows]
    return [dict(r) for r in rows]


def _query_uptake(group_id=None, session_id=None, task_id=None,
                   start_time=None, end_time=None, blind=True):
    sql = """SELECT iu.*
             FROM intervention_uptake iu
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND iu.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND iu.session_id = ?"
        params.append(session_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "iu.corrected_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY iu.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _query_ssrl_events(group_id=None, session_id=None, task_id=None,
                        start_time=None, end_time=None, blind=True):
    sql = """SELECT are.*
             FROM autonomous_regulation_events are
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND are.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND are.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND are.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "are.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY are.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _query_deliverables(group_id=None, session_id=None, task_id=None,
                         start_time=None, end_time=None, blind=True):
    sql = """SELECT cd.*, g.group_code
             FROM collaborative_documents cd
             JOIN groups g ON cd.group_id=g.id
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND cd.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND cd.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND cd.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "cd.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY cd.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _query_scores(group_id=None, session_id=None, task_id=None,
                   start_time=None, end_time=None, blind=True):
    sql = """SELECT ds.*
             FROM deliverable_scores ds
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND ds.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND ds.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND ds.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "ds.scored_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY ds.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _query_surveys(group_id=None, session_id=None, task_id=None,
                    start_time=None, end_time=None, blind=True):
    sql = """SELECT qr.id, u.participant_code, g.group_code,
                    qr.session_no, qr.task_id, qr.session_id,
                    q.code AS questionnaire_code, q.category_key,
                    q.title AS questionnaire_title, q.timing AS questionnaire_timing,
                    q.scale_max, qi.item_code, qi.dimension_label, qi.prompt_text,
                    qr.response_stage, qr.response_value, qr.response_batch_id,
                    qr.created_at
             FROM questionnaire_responses qr
             JOIN questionnaires q ON qr.questionnaire_id=q.id
             JOIN questionnaire_items qi ON qr.item_id=qi.id
             JOIN users u ON qr.user_id=u.id
             LEFT JOIN groups g ON qr.group_id=g.id
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND qr.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND qr.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND qr.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "qr.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY qr.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _query_audit_logs(group_id=None, session_id=None, task_id=None,
                       start_time=None, end_time=None, blind=True):
    sql = """SELECT al.*, u.real_name AS operator_name
             FROM audit_logs al
             LEFT JOIN users u ON al.operator_id=u.id
             WHERE 1=1"""
    params = []
    time_clauses = _build_time_clause(params, start_time, end_time, "al.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY al.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _query_help_requests(group_id=None, session_id=None, task_id=None,
                          start_time=None, end_time=None, blind=True):
    sql = """SELECT hr.id, u.participant_code, g.group_code,
                    hr.session_id, hr.session_no, hr.task_id,
                    hr.status, hr.request_text, hr.intent, hr.response_message,
                    hr.fallback_used, hr.source_message_id, hr.intervention_run_id,
                    hr.failure_reason, hr.created_at, hr.completed_at
             FROM help_requests hr
             JOIN groups g ON hr.group_id=g.id
             JOIN users u ON hr.requester_id=u.id
             WHERE 1=1"""
    params = []
    if group_id:
        sql += " AND hr.group_id = ?"
        params.append(group_id)
    if session_id:
        sql += " AND hr.session_id = ?"
        params.append(session_id)
    if task_id:
        sql += " AND hr.task_id = ?"
        params.append(task_id)
    time_clauses = _build_time_clause(params, start_time, end_time, "hr.created_at")
    for c in time_clauses:
        sql += " AND " + c
    sql += " ORDER BY hr.id ASC"
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows]


# Maps data_type strings to handler functions
DATA_TYPE_MAP = {
    "messages": _query_messages,
    "detector_outputs": _query_detector_outputs,
    "interventions": _query_interventions,
    "uptake": _query_uptake,
    "ssrl_events": _query_ssrl_events,
    "deliverables": _query_deliverables,
    "scores": _query_scores,
    "surveys": _query_surveys,
    "audit_logs": _query_audit_logs,
    "help_requests": _query_help_requests,
}


def _blind_row(row, blind_fields):
    """Set specified fields to None for blinding."""
    d = dict(row)
    for field in blind_fields:
        if field in d:
            d[field] = None
    return d
