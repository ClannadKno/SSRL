# -*- coding: utf-8 -*-
"""End-of-discussion collaboration state finalization.

This service fills the teacher-facing tail state segments after a group
discussion is actually over. It never sends student-visible messages and never
modifies collaborative document content.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from config import HUEY_ENABLED
from db import db, now_str, parse_dt, query_all, query_one
from services.audit_log_service import safe_write_audit_log
from services.intervention_pipeline_v2.context_builder import ContextBuilder
from services.intervention_pipeline_v2.strategy_review_service import (
    STATE_FINALIZATION_MODE,
    STATE_FINALIZATION_PROMPT_VERSION,
    review_state_finalization_context,
)

logger = logging.getLogger(__name__)


VALID_FINALIZATION_REASONS = {
    "student_submit",
    "timeout_auto_submit",
    "teacher_close",
    "room_freeze",
    "session_end",
}
TERMINAL_DISCUSSION_STATUSES = {"timed_out", "submitted", "closed"}
TERMINAL_DOCUMENT_STATUSES = {"submitted", "locked", "frozen", "closed"}
STALE_RUNNING_SECONDS = 10 * 60
FINALIZATION_MESSAGE_MAX_CHARS = 500


def _as_int(value, *, allow_none: bool = False) -> Optional[int]:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_int")
    try:
        return int(value)
    except (TypeError, ValueError):
        if allow_none:
            return None
        raise ValueError("invalid_int")


def _dedupe_key(group_id: int, session_id, session_no, discussion_id) -> str:
    return (
        f"state_finalization:g={group_id}:sid={session_id or ''}:"
        f"sno={session_no or ''}:d={discussion_id or ''}"
    )


def _dt(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return parse_dt(value)
        except Exception:
            return None
    return None


def _is_stale_running(started_at: str) -> bool:
    started = _dt(started_at)
    if not started:
        return True
    return (datetime.now() - started).total_seconds() >= STALE_RUNNING_SECONDS


def _session_row(conn, session_id: int) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT s.id AS session_id, s.session_no, s.status, s.task_id,
               t.title AS task_title, t.description AS task_description,
               t.question AS task_question, t.task_goal, t.output_requirement,
               t.key_concepts_json, t.expected_dimensions_json,
               t.task_payload_json
        FROM experiment_sessions s
        LEFT JOIN learning_tasks t ON t.id = s.task_id
        WHERE s.id=?
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def _json_value(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _session_context_from_row(row: dict) -> dict:
    if not row:
        return {
            "session": {"session_id": None, "session_no": None, "status": None},
            "task": None,
        }
    task_payload = _json_value(row.get("task_payload_json"), {}) or {}
    task = None
    if row.get("task_id"):
        task = {
            "task_id": row.get("task_id"),
            "title": row.get("task_title"),
            "topic": row.get("task_title"),
            "description": row.get("task_description"),
            "goal": row.get("task_goal"),
            "question": row.get("task_question"),
            "output_requirement": row.get("output_requirement"),
            "key_concepts": _json_value(row.get("key_concepts_json"), None),
            "expected_dimensions": _json_value(row.get("expected_dimensions_json"), None),
            "evaluation_criteria": task_payload.get("evaluation_criteria"),
        }
    return {
        "session": {
            "session_id": row.get("session_id"),
            "session_no": row.get("session_no"),
            "status": row.get("status"),
        },
        "task": task,
    }


def _message_session_filter(alias: str, session_id, session_no) -> tuple[str, list]:
    if session_id is not None:
        return (f"{alias}.session_id=?", [session_id])
    if session_no is not None:
        return (f"{alias}.session_no=?", [session_no])
    return ("1=1", [])


def _segment_session_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"(({prefix}session_id IS NULL AND ? IS NULL) OR {prefix}session_id=?) "
        f"AND (({prefix}session_no IS NULL AND ? IS NULL) OR {prefix}session_no=?)"
    )


def _segment_session_params(session_id, session_no) -> tuple:
    return (session_id, session_id, session_no, session_no)


def _discussion_has_ended(conn, *, group_id: int, session_id: int, session_no, task_id) -> dict:
    group = conn.execute("SELECT id, state FROM groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        return {"allowed": False, "reason": "group_not_found"}
    if group["state"] == "CLOSED":
        return {"allowed": True, "reason": "group_closed"}

    session = conn.execute(
        "SELECT id, status FROM experiment_sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if not session:
        return {"allowed": False, "reason": "session_not_found"}
    if session["status"] in {"ended", "archived"}:
        return {"allowed": True, "reason": "session_ended"}

    runtime = conn.execute(
        """
        SELECT status, deadline
        FROM group_session_discussions
        WHERE session_id=? AND group_id=?
        LIMIT 1
        """,
        (session_id, group_id),
    ).fetchone()
    if runtime:
        status = runtime["status"]
        if status in TERMINAL_DISCUSSION_STATUSES:
            return {"allowed": True, "reason": f"discussion_{status}"}
        deadline = _dt(runtime["deadline"])
        if status == "running" and deadline and datetime.now() >= deadline:
            return {"allowed": True, "reason": "discussion_deadline_passed"}

    if task_id is not None and session_no is not None:
        doc = conn.execute(
            """
            SELECT status, submitted_at
            FROM collaborative_documents
            WHERE group_id=?
              AND (session_id=? OR (session_id IS NULL AND task_id=? AND session_no=?))
            ORDER BY CASE WHEN session_id=? THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (group_id, session_id, task_id, session_no, session_id),
        ).fetchone()
    else:
        doc = conn.execute(
            """
            SELECT status, submitted_at
            FROM collaborative_documents
            WHERE group_id=? AND session_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (group_id, session_id),
        ).fetchone()
    if doc and (doc["submitted_at"] is not None or doc["status"] in TERMINAL_DOCUMENT_STATUSES):
        return {"allowed": True, "reason": "document_closed"}

    return {"allowed": False, "reason": "discussion_still_running"}


def _claim_finalization(
    conn,
    *,
    group_id: int,
    session_id: int,
    session_no,
    task_id,
    discussion_id,
    reason: str,
) -> dict:
    now = now_str()
    dedupe_key = _dedupe_key(
        group_id,
        session_id,
        session_no,
        discussion_id,
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT OR IGNORE INTO collaboration_state_finalizations(
            group_id, session_id, session_no, task_id, discussion_id, status, reason,
            retry_count, dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            session_id,
            session_no,
            task_id,
            discussion_id,
            "pending",
            reason,
            0,
            dedupe_key,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM collaboration_state_finalizations WHERE dedupe_key=?",
        (dedupe_key,),
    ).fetchone()
    if not row:
        conn.commit()
        return {"claimed": False, "reason": "row_not_found"}
    data = dict(row)
    status = data.get("status")
    if status == "succeeded":
        conn.commit()
        return {"claimed": False, "status": "already_finalized", "row": data}
    if status == "running" and not _is_stale_running(data.get("started_at")):
        conn.commit()
        return {"claimed": False, "status": "already_running", "row": data}

    retry_count = int(data.get("retry_count") or 0)
    if status in {"failed", "running"}:
        retry_count += 1
    conn.execute(
        """
        UPDATE collaboration_state_finalizations
        SET status='running', reason=?, task_id=COALESCE(?, task_id),
            started_at=?, completed_at=NULL, error=NULL,
            retry_count=?, source_run_id=COALESCE(source_run_id, id),
            updated_at=?
        WHERE id=?
        """,
        (reason, task_id, now, retry_count, now, data["id"]),
    )
    updated = conn.execute(
        "SELECT * FROM collaboration_state_finalizations WHERE id=?",
        (data["id"],),
    ).fetchone()
    conn.commit()
    return {"claimed": True, "row": dict(updated)}


def _mark_succeeded(
    *,
    finalization_id: int,
    analysis_start_message_id=None,
    analysis_end_message_id=None,
) -> None:
    conn = db()
    try:
        now = now_str()
        conn.execute(
            """
            UPDATE collaboration_state_finalizations
            SET status='succeeded',
                analysis_start_message_id=?,
                analysis_end_message_id=?,
                source_run_id=COALESCE(source_run_id, id),
                completed_at=?,
                error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (analysis_start_message_id, analysis_end_message_id, now, now, finalization_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_failed(
    *,
    finalization_id: int,
    error: str,
    analysis_start_message_id=None,
    analysis_end_message_id=None,
) -> None:
    conn = db()
    try:
        now = now_str()
        conn.execute(
            """
            UPDATE collaboration_state_finalizations
            SET status='failed',
                analysis_start_message_id=COALESCE(?, analysis_start_message_id),
                analysis_end_message_id=COALESCE(?, analysis_end_message_id),
                completed_at=?,
                error=?,
                updated_at=?
            WHERE id=?
            """,
            (
                analysis_start_message_id,
                analysis_end_message_id,
                now,
                (error or "state_finalization_failed")[:2000],
                now,
                finalization_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _update_analysis_range(finalization_id: int, start, end) -> None:
    conn = db()
    try:
        conn.execute(
            """
            UPDATE collaboration_state_finalizations
            SET analysis_start_message_id=?, analysis_end_message_id=?, updated_at=?
            WHERE id=?
            """,
            (start, end, now_str(), finalization_id),
        )
        conn.commit()
    finally:
        conn.close()


def _first_student_sequence(conn, *, group_id: int, session_id, session_no, after=None, at_or_after=None, before_or_at=None):
    where = ["m.group_id=?", "m.sequence IS NOT NULL", "COALESCE(m.role, '')='student'"]
    params = [group_id]
    session_sql, session_params = _message_session_filter("m", session_id, session_no)
    where.append(session_sql)
    params.extend(session_params)
    if after is not None:
        where.append("m.sequence>?")
        params.append(after)
    if at_or_after is not None:
        where.append("m.sequence>=?")
        params.append(at_or_after)
    if before_or_at is not None:
        where.append("m.sequence<=?")
        params.append(before_or_at)
    row = conn.execute(
        f"""
        SELECT m.sequence
        FROM messages m
        WHERE {' AND '.join(where)}
        ORDER BY m.sequence ASC, m.id ASC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return int(row["sequence"]) if row and row["sequence"] is not None else None


def _latest_student_sequence(conn, *, group_id: int, session_id, session_no):
    where = ["m.group_id=?", "m.sequence IS NOT NULL", "COALESCE(m.role, '')='student'"]
    params = [group_id]
    session_sql, session_params = _message_session_filter("m", session_id, session_no)
    where.append(session_sql)
    params.extend(session_params)
    row = conn.execute(
        f"""
        SELECT m.sequence
        FROM messages m
        WHERE {' AND '.join(where)}
        ORDER BY m.sequence DESC, m.id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return int(row["sequence"]) if row and row["sequence"] is not None else None


def _last_finalized_message_end(conn, *, group_id: int, session_id, session_no):
    row = conn.execute(
        f"""
        SELECT MAX(end_message_id) AS end_message_id
        FROM collaboration_state_segments
        WHERE group_id=?
          AND {_segment_session_where()}
          AND segment_kind='message_range'
          AND source='session_finalizer'
          AND is_finalized=1
          AND end_message_id IS NOT NULL
        """,
        (group_id, *_segment_session_params(session_id, session_no)),
    ).fetchone()
    return int(row["end_message_id"]) if row and row["end_message_id"] is not None else None


def _latest_unfinalized_anchor(conn, *, group_id: int, session_id, session_no):
    row = conn.execute(
        f"""
        SELECT analysis_anchor_message_id, MAX(updated_at) AS updated_at
        FROM collaboration_state_segments
        WHERE group_id=?
          AND {_segment_session_where()}
          AND source='strategy_llm'
          AND is_finalized=0
          AND analysis_anchor_message_id IS NOT NULL
        GROUP BY analysis_anchor_message_id
        ORDER BY updated_at DESC, analysis_anchor_message_id DESC
        LIMIT 1
        """,
        (group_id, *_segment_session_params(session_id, session_no)),
    ).fetchone()
    return int(row["analysis_anchor_message_id"]) if row and row["analysis_anchor_message_id"] is not None else None


def _compute_tail_range(conn, *, group_id: int, session_id: int, session_no, latest_student_sequence: int) -> dict:
    unfinalized_anchor = _latest_unfinalized_anchor(
        conn,
        group_id=group_id,
        session_id=session_id,
        session_no=session_no,
    )
    if unfinalized_anchor is not None:
        start = _first_student_sequence(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            at_or_after=unfinalized_anchor,
            before_or_at=latest_student_sequence,
        )
        if start is not None:
            return {
                "has_tail": True,
                "start": start,
                "end": latest_student_sequence,
                "anchor": start,
                "basis": "latest_unfinalized_anchor",
            }

    last_finalized_end = _last_finalized_message_end(
        conn,
        group_id=group_id,
        session_id=session_id,
        session_no=session_no,
    )
    if last_finalized_end is not None:
        if last_finalized_end >= latest_student_sequence:
            return {
                "has_tail": False,
                "reason": "all_student_messages_finalized",
                "start": None,
                "end": latest_student_sequence,
                "basis": "finalized_range",
            }
        start = _first_student_sequence(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            after=last_finalized_end,
            before_or_at=latest_student_sequence,
        )
        return {
            "has_tail": start is not None,
            "reason": None if start is not None else "no_unfinalized_student_message",
            "start": start,
            "end": latest_student_sequence,
            "anchor": start,
            "basis": "after_finalized_range",
        }

    previous = ContextBuilder.find_previous_strategy_intervention(
        group_id,
        session_id,
        latest_student_sequence,
    )
    if previous and previous.get("sequence") is not None:
        start = _first_student_sequence(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            after=int(previous["sequence"]),
            before_or_at=latest_student_sequence,
        )
        if start is not None:
            return {
                "has_tail": True,
                "start": start,
                "end": latest_student_sequence,
                "anchor": start,
                "basis": "after_previous_strategy_intervention",
                "previous_strategy_sequence": int(previous["sequence"]),
            }

    start = _first_student_sequence(
        conn,
        group_id=group_id,
        session_id=session_id,
        session_no=session_no,
        before_or_at=latest_student_sequence,
    )
    return {
        "has_tail": start is not None,
        "reason": None if start is not None else "no_student_messages",
        "start": start,
        "end": latest_student_sequence,
        "anchor": start,
        "basis": "first_student_message",
    }


def _load_finalization_messages(conn, *, group_id: int, session_id, session_no, start: int, end: int) -> list[dict]:
    session_sql, session_params = _message_session_filter("m", session_id, session_no)
    rows = conn.execute(
        f"""
        SELECT m.id, m.group_id, m.sequence, m.content, m.created_at,
               m.role, m.sender_type, m.agent_type, m.user_id, m.strategy_id,
               m.linked_log_id, m.intervention_run_id, m.session_id,
               m.session_no, m.task_id,
               COALESCE(NULLIF(TRIM(u.real_name), ''), u.username, 'member') AS display_name
        FROM messages m
        LEFT JOIN users u ON u.id = m.user_id
        WHERE m.group_id=?
          AND m.sequence IS NOT NULL
          AND m.sequence>=?
          AND m.sequence<=?
          AND {session_sql}
          AND COALESCE(m.role, 'student') IN ('student', 'agent', 'system', 'teacher')
        ORDER BY m.sequence ASC, m.id ASC
        """,
        (group_id, start, end, *session_params),
    ).fetchall()
    messages = []
    for row in rows:
        role = row["role"] or row["sender_type"] or "student"
        agent_type = row["agent_type"] if role == "agent" else None
        sender_type = row["sender_type"] or role
        if role == "agent":
            if agent_type == "strategy":
                sender_type = "strategy_agent"
            elif agent_type == "emotion":
                sender_type = "emotion_agent"
            else:
                sender_type = "agent"
        content = row["content"] or ""
        if len(content) > FINALIZATION_MESSAGE_MAX_CHARS:
            content = content[:FINALIZATION_MESSAGE_MAX_CHARS]
        messages.append({
            "id": row["id"],
            "message_id": row["sequence"],
            "group_id": row["group_id"],
            "sequence": row["sequence"],
            "role": role,
            "speaker": row["display_name"] if role in {"student", "teacher"} else "SERA",
            "sender": row["display_name"] if role in {"student", "teacher"} else "SERA",
            "sender_type": sender_type,
            "agent_type": agent_type,
            "content": content,
            "time": row["created_at"],
            "created_at": row["created_at"],
            "session_id": row["session_id"],
            "session_no": row["session_no"],
            "task_id": row["task_id"],
            "strategy_id": row["strategy_id"],
            "linked_log_id": row["linked_log_id"],
            "intervention_run_id": row["intervention_run_id"],
            "can_be_state_evidence": role == "student",
        })
    return messages


def _build_finalization_context(
    *,
    finalization_id: int,
    group_id: int,
    session_id: int,
    session_ctx: dict,
    reason: str,
    tail_range: dict,
    messages: list[dict],
    gate_reason: str,
) -> dict:
    input_sequences = [
        int(msg["sequence"])
        for msg in messages
        if msg.get("sequence") is not None
    ]
    return {
        "mode": STATE_FINALIZATION_MODE,
        "finalization_id": finalization_id,
        "group_id": group_id,
        "session_id": session_id,
        "reason": reason,
        "task_context": session_ctx,
        "session": session_ctx.get("session"),
        "task": session_ctx.get("task"),
        "context_boundary": {
            "from_sequence": tail_range.get("start"),
            "to_sequence": tail_range.get("end"),
            "analysis_anchor_message_id": tail_range.get("anchor"),
            "range_basis": tail_range.get("basis"),
            "previous_strategy_sequence": tail_range.get("previous_strategy_sequence"),
            "context_truncated": False,
            "omitted_sequence_ranges": [],
        },
        "context_from_sequence": tail_range.get("start"),
        "context_to_sequence": tail_range.get("end"),
        "messages": messages,
        "input_message_sequences": input_sequences,
        "runtime_context": {
            "end_reason": reason,
            "gate_reason": gate_reason,
            "message_count_in_context": len(messages),
            "server_time": now_str(),
        },
        "allowed_strategies": [],
    }


def finalize_collaboration_states(group_id: int, session_id: int, reason: str, *, gateway=None) -> dict:
    """Run one final tail state analysis for a group/session.

    Repeated successful calls return ``already_finalized``. Failed calls may be
    retried by calling the same function again.
    """
    group_id = _as_int(group_id)
    session_id = _as_int(session_id)
    reason = reason if reason in VALID_FINALIZATION_REASONS else "session_end"

    conn = db()
    try:
        session = _session_row(conn, session_id)
        if not session:
            return {"ok": False, "reason": "session_not_found", "group_id": group_id, "session_id": session_id}
        session_no = session.get("session_no")
        task_id = session.get("task_id")
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            allow_legacy_fallback=False,
        )
        claim = _claim_finalization(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            discussion_id=scope.discussion_id,
            reason=reason,
        )
        if not claim.get("claimed"):
            return {
                "ok": True,
                "skipped": True,
                "reason": claim.get("status") or claim.get("reason"),
                "group_id": group_id,
                "session_id": session_id,
                "finalization_id": (claim.get("row") or {}).get("id"),
            }
        finalization = claim["row"]
        finalization_id = int(finalization["id"])

        gate = _discussion_has_ended(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
        )
        if not gate["allowed"]:
            _mark_failed(finalization_id=finalization_id, error=gate["reason"])
            return {
                "ok": False,
                "reason": gate["reason"],
                "group_id": group_id,
                "session_id": session_id,
                "finalization_id": finalization_id,
            }

        latest_student = _latest_student_sequence(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
        )
        if latest_student is None:
            _mark_succeeded(finalization_id=finalization_id)
            return {
                "ok": True,
                "skipped": True,
                "reason": "no_student_messages",
                "group_id": group_id,
                "session_id": session_id,
                "finalization_id": finalization_id,
            }

        tail_range = _compute_tail_range(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            latest_student_sequence=latest_student,
        )
        _update_analysis_range(
            finalization_id,
            tail_range.get("start"),
            tail_range.get("end"),
        )
        if not tail_range.get("has_tail"):
            _mark_succeeded(
                finalization_id=finalization_id,
                analysis_start_message_id=tail_range.get("start"),
                analysis_end_message_id=tail_range.get("end"),
            )
            return {
                "ok": True,
                "skipped": True,
                "reason": tail_range.get("reason") or "no_tail_student_messages",
                "group_id": group_id,
                "session_id": session_id,
                "finalization_id": finalization_id,
                "analysis_end_message_id": tail_range.get("end"),
            }

        messages = _load_finalization_messages(
            conn,
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            start=tail_range["start"],
            end=tail_range["end"],
        )
        student_messages = [msg for msg in messages if msg.get("can_be_state_evidence")]
        if not student_messages:
            _mark_succeeded(
                finalization_id=finalization_id,
                analysis_start_message_id=tail_range.get("start"),
                analysis_end_message_id=tail_range.get("end"),
            )
            return {
                "ok": True,
                "skipped": True,
                "reason": "no_tail_student_messages",
                "group_id": group_id,
                "session_id": session_id,
                "finalization_id": finalization_id,
            }
        session_ctx = _session_context_from_row(session)
    finally:
        conn.close()

    context = _build_finalization_context(
        finalization_id=finalization_id,
        group_id=group_id,
        session_id=session_id,
        session_ctx=session_ctx,
        reason=reason,
        tail_range=tail_range,
        messages=messages,
        gate_reason=gate["reason"],
    )

    try:
        review_result = review_state_finalization_context(context, gateway=gateway)
        if not review_result.get("ok"):
            error = review_result.get("reason") or "state_finalization_llm_failed"
            _mark_failed(
                finalization_id=finalization_id,
                error=error,
                analysis_start_message_id=tail_range.get("start"),
                analysis_end_message_id=tail_range.get("end"),
            )
            safe_write_audit_log(
                action_type="collaboration_state_finalization.failed",
                actor_type="system",
                actor_id="state_finalizer",
                target_type="collaboration_state_finalization",
                target_id=finalization_id,
                metadata={
                    "group_id": group_id,
                    "session_id": session_id,
                    "reason": reason,
                    "error_type": error,
                },
            )
            return {
                "ok": False,
                "reason": error,
                "group_id": group_id,
                "session_id": session_id,
                "finalization_id": finalization_id,
            }

        from services.collaboration_state_segment_service import CollaborationStateSegmentService

        persisted = CollaborationStateSegmentService.save_finalization_segments(
            group_id=group_id,
            session_id=session_id,
            session_no=(session_ctx.get("session") or {}).get("session_no"),
            task_id=((session_ctx.get("task") or {}) or {}).get("task_id"),
            state_segments=review_result.get("state_segments") or [],
            source_run_id=finalization_id,
            analysis_anchor_message_id=tail_range.get("anchor"),
            analysis_window_start_message_id=tail_range.get("start"),
            analysis_window_end_message_id=tail_range.get("end"),
            prompt_version=review_result.get("prompt_version") or STATE_FINALIZATION_PROMPT_VERSION,
        )
        validation = review_result.get("validation") or {}
        proposed_segments = validation.get("proposed_segments") or []
        normalized_segments = validation.get("normalized_segments") or []
        rejected_segments = validation.get("rejected_segments") or []
        rejection_reasons = [
            item.get("reason")
            for item in rejected_segments
            if item.get("reason")
        ]
        agent_sequences_inside = validation.get(
            "agent_message_sequences_inside_range"
        ) or []
        current_state_normalization_reason = validation.get(
            "current_state_normalization_reason"
        )
        segment_results = {
            "proposed_count": len(proposed_segments),
            "normalized_count": len(normalized_segments),
            "saved_count": persisted.get("saved_count", 0),
            "rejected_count": len(rejected_segments),
            "proposed": proposed_segments,
            "normalized": normalized_segments,
            "saved": persisted.get("saved") or [],
            "saved_segment_ids": persisted.get("saved_segment_ids") or [],
            "rejected": rejected_segments,
            "reasons": rejection_reasons,
            "agent_messages_existed_inside_range": bool(agent_sequences_inside),
            "agent_message_sequences_inside_range": agent_sequences_inside,
            "current_state_normalization_reason": (
                current_state_normalization_reason
            ),
        }
        _mark_succeeded(
            finalization_id=finalization_id,
            analysis_start_message_id=tail_range.get("start"),
            analysis_end_message_id=tail_range.get("end"),
        )
        safe_write_audit_log(
            action_type="collaboration_state_finalization.succeeded",
            actor_type="system",
            actor_id="state_finalizer",
            target_type="collaboration_state_finalization",
            target_id=finalization_id,
            metadata={
                "group_id": group_id,
                "session_id": session_id,
                "reason": reason,
                "saved_count": persisted.get("saved_count", 0),
                "proposed_segments": proposed_segments,
                "normalized_segments": normalized_segments,
                "saved_segment_ids": persisted.get("saved_segment_ids") or [],
                "rejected_segment_orders": [
                    item.get("segment_order") for item in rejected_segments
                ],
                "rejection_reasons": rejection_reasons,
                "agent_messages_existed_inside_range": bool(
                    agent_sequences_inside
                ),
                "agent_message_sequences_inside_range": agent_sequences_inside,
                "current_state_normalization_reason": (
                    current_state_normalization_reason
                ),
                "analysis_start_message_id": tail_range.get("start"),
                "analysis_end_message_id": tail_range.get("end"),
            },
        )
        return {
            "ok": True,
            "group_id": group_id,
            "session_id": session_id,
            "finalization_id": finalization_id,
            "analysis_start_message_id": tail_range.get("start"),
            "analysis_end_message_id": tail_range.get("end"),
            "state_segments": persisted,
            "segment_results": segment_results,
            "saved": segment_results["saved"],
            "rejected": segment_results["rejected"],
            "reasons": segment_results["reasons"],
            "llm_called": True,
            "should_intervene": False,
        }
    except Exception as exc:
        logger.exception(
            "finalize_collaboration_states failed group=%s session=%s reason=%s",
            group_id,
            session_id,
            reason,
        )
        _mark_failed(
            finalization_id=finalization_id,
            error=f"{exc.__class__.__name__}: {str(exc)[:500]}",
            analysis_start_message_id=tail_range.get("start"),
            analysis_end_message_id=tail_range.get("end"),
        )
        safe_write_audit_log(
            action_type="collaboration_state_finalization.failed",
            actor_type="system",
            actor_id="state_finalizer",
            target_type="collaboration_state_finalization",
            target_id=finalization_id,
            metadata={
                "group_id": group_id,
                "session_id": session_id,
                "reason": reason,
                "error_type": exc.__class__.__name__,
            },
        )
        return {
            "ok": False,
            "reason": str(exc),
            "group_id": group_id,
            "session_id": session_id,
            "finalization_id": finalization_id,
        }


def request_collaboration_state_finalization(group_id: int, session_id: int, reason: str) -> dict:
    """Queue or run finalization for one group/session."""
    if not group_id or not session_id:
        return {"queued": False, "reason": "missing_group_or_session"}
    reason = reason if reason in VALID_FINALIZATION_REASONS else "session_end"
    if HUEY_ENABLED:
        from agent.state_finalization_tasks import finalize_collaboration_states_task

        finalize_collaboration_states_task.schedule(
            args=(int(group_id), int(session_id), reason),
            delay=0,
            priority=60,
        )
        return {"queued": True, "group_id": int(group_id), "session_id": int(session_id), "reason": reason}
    return finalize_collaboration_states(int(group_id), int(session_id), reason)


def safe_request_collaboration_state_finalization(group_id: int, session_id: int, reason: str) -> dict:
    """Best-effort trigger that never blocks the caller with an exception."""
    try:
        return request_collaboration_state_finalization(group_id, session_id, reason)
    except Exception as exc:
        logger.warning(
            "state finalization request failed group=%s session=%s reason=%s: %s",
            group_id,
            session_id,
            reason,
            exc,
        )
        safe_write_audit_log(
            action_type="collaboration_state_finalization.request_failed",
            actor_type="system",
            actor_id="state_finalizer",
            target_type="experiment_session",
            target_id=session_id,
            metadata={
                "group_id": group_id,
                "session_id": session_id,
                "reason": reason,
                "error_type": exc.__class__.__name__,
            },
        )
        return {"queued": False, "reason": str(exc), "group_id": group_id, "session_id": session_id}


def _resolve_document_session(doc: dict) -> Optional[int]:
    if doc.get("session_id"):
        return int(doc["session_id"])
    if doc.get("task_id") is None or doc.get("session_no") is None:
        return None
    row = query_one(
        """
        SELECT id
        FROM experiment_sessions
        WHERE task_id=? AND session_no=?
        ORDER BY id DESC LIMIT 1
        """,
        (doc.get("task_id"), doc.get("session_no")),
    )
    return int(row["id"]) if row else None


def safe_request_finalization_for_document(document_id: int, reason: str) -> dict:
    """Best-effort trigger for a collaborative document close/submit path."""
    try:
        doc = query_one(
            """
            SELECT id, group_id, session_id, task_id, session_no
            FROM collaborative_documents
            WHERE id=?
            """,
            (document_id,),
        )
        if not doc:
            return {"queued": False, "reason": "document_not_found"}
        session_id = _resolve_document_session(dict(doc))
        if not session_id:
            return {"queued": False, "reason": "document_session_missing", "document_id": document_id}
        return safe_request_collaboration_state_finalization(
            int(doc["group_id"]),
            session_id,
            reason,
        )
    except Exception as exc:
        logger.warning("state finalization document request failed doc=%s: %s", document_id, exc)
        return {"queued": False, "reason": str(exc), "document_id": document_id}


def groups_for_session(session_id: int) -> list[dict]:
    session_id = _as_int(session_id)
    session = query_one(
        "SELECT id, session_no, task_id FROM experiment_sessions WHERE id=?",
        (session_id,),
    )
    if not session:
        return []
    rows = query_all(
        """
        SELECT DISTINCT group_id
        FROM (
            SELECT group_id FROM group_session_discussions WHERE session_id=?
            UNION
            SELECT group_id FROM collaborative_documents
            WHERE session_id=? OR (session_id IS NULL AND task_id=? AND session_no=?)
            UNION
            SELECT group_id FROM messages
            WHERE session_id=? OR (session_id IS NULL AND session_no=?)
        )
        WHERE group_id IS NOT NULL
        ORDER BY group_id ASC
        """,
        (
            session_id,
            session_id,
            session["task_id"],
            session["session_no"],
            session_id,
            session["session_no"],
        ),
    )
    return [{"group_id": int(row["group_id"]), "session_id": session_id} for row in rows]


def safe_request_finalization_for_session_groups(session_id: int, reason: str) -> dict:
    """Best-effort trigger for all groups participating in a session."""
    requested = []
    for item in groups_for_session(session_id):
        requested.append(
            safe_request_collaboration_state_finalization(
                item["group_id"],
                item["session_id"],
                reason,
            )
        )
    return {"requested": len(requested), "details": requested}


__all__ = [
    "STATE_FINALIZATION_MODE",
    "finalize_collaboration_states",
    "request_collaboration_state_finalization",
    "safe_request_collaboration_state_finalization",
    "safe_request_finalization_for_document",
    "safe_request_finalization_for_session_groups",
    "groups_for_session",
]
