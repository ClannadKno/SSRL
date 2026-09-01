# -*- coding: utf-8 -*-
"""Phase 10 execution helpers for persisting and delivering assistant interventions."""

import re

from auth import get_group_condition, get_sera_user_id
from config import LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED
from db import create_message, db, execute, now_str, record_process_event


AUTO_PUSH_MODE = "sera_auto"
TEACHER_PUSH_MODE = "sera_teacher_confirmed"


LEARNING_ASSISTANT_LABEL_RE = re.compile(r"^\s*【学习助手(?:[·・.．][^】]+)?】\s*")


def clean_assistant_message_content(content):
    """Return the student-visible assistant content without display labels."""
    return LEARNING_ASSISTANT_LABEL_RE.sub("", content or "", count=1).strip()


def _suggestion_value(suggestion, key, fallback=None):
    if not suggestion:
        return fallback
    try:
        value = suggestion[key]
    except Exception:
        value = suggestion.get(key) if isinstance(suggestion, dict) else None
    return fallback if value is None else value


def execute_intervention(
    group_id,
    decision,
    strategy,
    suggestion=None,
    teacher_user_id=None,
    push_mode=AUTO_PUSH_MODE,
    log_trigger_source=None,
):
    """Write intervention logs, deliver the assistant message, and update suggestion status."""
    if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
        return None
    if not suggestion:
        return None

    condition = _suggestion_value(suggestion, "condition", get_group_condition(group_id))
    decision_id = _suggestion_value(suggestion, "decision_id") or (decision or {}).get("id")
    trigger_source = log_trigger_source or _suggestion_value(suggestion, "trigger_source", push_mode)
    created_at = now_str()
    scope_conn = db()
    try:
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            scope_conn,
            group_id=group_id,
            session_id=_suggestion_value(suggestion, "session_id"),
            session_no=_suggestion_value(suggestion, "session_no"),
            task_id=_suggestion_value(suggestion, "task_id"),
            discussion_id=_suggestion_value(suggestion, "discussion_id"),
            allow_legacy_fallback=False,
        )
    finally:
        scope_conn.close()
    log_id = execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, pushed_by_user_id, push_mode, title, message,
            suggestion_id, decision_id, condition, trigger_source, strategy_id, template_id,
            sub_category, strategy_type, strategy_version, model_name, prompt_version,
            session_id, session_no, task_id, discussion_id, created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            _suggestion_value(suggestion, "intervention_id"),
            teacher_user_id,
            push_mode,
            _suggestion_value(suggestion, "title", (strategy or {}).get("title")),
            _suggestion_value(suggestion, "message", (strategy or {}).get("message")),
            _suggestion_value(suggestion, "id"),
            decision_id,
            condition,
            trigger_source,
            _suggestion_value(suggestion, "strategy_id", (strategy or {}).get("strategy_id")),
            _suggestion_value(suggestion, "template_id", (strategy or {}).get("template_id")),
            _suggestion_value(suggestion, "sub_category", (strategy or {}).get("sub_category")),
            _suggestion_value(suggestion, "strategy_type", (strategy or {}).get("strategy_type")),
            _suggestion_value(suggestion, "strategy_version", (strategy or {}).get("strategy_version")),
            _suggestion_value(suggestion, "model_name"),
            _suggestion_value(suggestion, "prompt_version"),
            scope.session_id,
            scope.session_no,
            scope.task_id,
            scope.discussion_id,
            created_at,
        ),
    )

    suggestion_id = _suggestion_value(suggestion, "id")
    if suggestion_id:
        next_status = "auto_pushed" if push_mode == AUTO_PUSH_MODE else "pushed"
        note_prefix = "auto_pushed" if push_mode == AUTO_PUSH_MODE else "pushed"
        execute(
            """
            UPDATE agent_suggestions
            SET status=?, decided_at=?, decided_by_user_id=?, decision_note=?
            WHERE id=?
            """,
            (
                next_status,
                created_at,
                teacher_user_id,
                f"{note_prefix}_log_id={log_id}",
                suggestion_id,
            ),
        )

    sera_user_id = get_sera_user_id()
    if sera_user_id:
        message_content = clean_assistant_message_content(
            _suggestion_value(suggestion, "message", (strategy or {}).get("message") or "")
        )
        msg = create_message(
            group_id,
            sera_user_id,
            message_content,
            client_message_id=f"agent-{suggestion_id}" if suggestion_id else None,
            role="agent",
            strategy_id=_suggestion_value(suggestion, "strategy_id", (strategy or {}).get("strategy_id")),
            linked_log_id=log_id,
            session_id=scope.session_id,
            session_no=scope.session_no,
            task_id=scope.task_id,
            discussion_id=scope.discussion_id,
        )
        if msg and msg.get("id"):
            execute(
                "UPDATE messages SET agent_type='strategy' WHERE id=? AND role='agent'",
                (msg["id"],),
            )
    record_process_event(
        "intervention_pushed",
        source="teacher" if teacher_user_id else "agent",
        group_id=group_id,
        user_id=teacher_user_id,
        related_table="intervention_logs",
        related_id=log_id,
        event_key=f"intervention:{log_id}",
        payload={
            "push_mode": push_mode,
            "trigger_source": trigger_source,
            "strategy_id": _suggestion_value(suggestion, "strategy_id", (strategy or {}).get("strategy_id")),
            "template_id": _suggestion_value(suggestion, "template_id", (strategy or {}).get("template_id")),
            "suggestion_id": suggestion_id,
            "decision_id": decision_id,
        },
        created_at=created_at,
    )
    return log_id
