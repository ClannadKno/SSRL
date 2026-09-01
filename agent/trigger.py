# -*- coding: utf-8 -*-
# Batch 9: Teacher UI compat functions only.
# New pipeline uses monitor_runs + intervention_runs.
# agent_suggestions is read-only (historical data kept).

import json
from datetime import datetime, timedelta

from config import *
from db import *
from auth import get_group_condition, get_sera_user_id
from knowledge_base import (
    CONFLICT_WORDS,
    FRUSTRATION_WORDS,
    LOW_MOTIVATION_WORDS,
    OFF_TASK_WORDS,
    PASSIVE_DETACHMENT_WORDS,
    VALUE_DOUBT_WORDS,
)
from services.intervention_execution import clean_assistant_message_content, execute_intervention


def count_hits(text, words):
    return sum(1 for word in words if word and word in text)


def text_has_any(text, words):
    return any(word and word in text for word in words)

def push_intervention(group_id, intervention_id, pushed_by_user_id, push_mode="teacher"):
    if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
        return None

    intervention = query_one("SELECT * FROM interventions WHERE id=?", (intervention_id,))
    if not intervention:
        return None

    scope_conn = db()
    try:
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            scope_conn,
            group_id=group_id,
            allow_legacy_fallback=False,
        )
    finally:
        scope_conn.close()
    log_id = execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, pushed_by_user_id, push_mode, title, message,
            suggestion_id, condition, trigger_source, template_id, sub_category, strategy_type,
            session_id, session_no, task_id, discussion_id, created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            intervention_id,
            pushed_by_user_id,
            push_mode,
            intervention["title"],
            intervention["message"],
            None,
            get_group_condition(group_id),
            push_mode,
            None,
            None,
            intervention["strategy_type"] if "strategy_type" in intervention.keys() else None,
            scope.session_id,
            scope.session_no,
            scope.task_id,
            scope.discussion_id,
            now_str(),
        ),
    )

    sera_user_id = get_sera_user_id()
    if sera_user_id:
        create_message(
            group_id,
            sera_user_id,
            clean_assistant_message_content(intervention["message"]),
            client_message_id=f"intervention-{log_id}" if log_id else None,
            role="agent",
            linked_log_id=log_id,
            session_id=scope.session_id,
            session_no=scope.session_no,
            task_id=scope.task_id,
            discussion_id=scope.discussion_id,
        )
    record_process_event(
        "intervention_pushed",
        source="teacher" if pushed_by_user_id else "agent",
        group_id=group_id,
        user_id=pushed_by_user_id,
        related_table="intervention_logs",
        related_id=log_id,
        event_key=f"intervention:{log_id}",
        payload={
            "push_mode": push_mode,
            "intervention_id": intervention_id,
            "strategy_type": intervention["strategy_type"] if "strategy_type" in intervention.keys() else None,
        },
        created_at=now_str(),
    )
    return log_id



# -----------------------------
# SSRL 策略库 v3.0：通用项目式学习版
# -----------------------------


def get_latest_pending_suggestion(group_id):
    row = query_one(
        """
        SELECT * FROM agent_suggestions
        WHERE group_id=? AND status='pending'
          AND COALESCE(trigger_source, 'student_activity') <> 'student_help_request'
        ORDER BY id DESC LIMIT 1
        """,
        (group_id,),
    )
    return row




def get_latest_agent_suggestion(group_id):
    """教师端查看最近一次 SERA 分析记录，包括自动推送、待确认、已忽略、观察抑制等状态。"""
    row = query_one(
        """
        SELECT * FROM agent_suggestions
        WHERE group_id=?
          AND COALESCE(trigger_source, 'student_activity') <> 'student_help_request'
        ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (group_id,),
    )
    if row:
        return row
    return query_one(
        """
        SELECT * FROM agent_suggestions
        WHERE group_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (group_id,),
    )



def is_quick_trigger_message(content):
    """
    v34：判断单条学生消息是否属于需要快速旁观分析的强信号。
    命中后不等待普通防抖节流，立即排队分析。
    """
    if not content:
        return False
    text = content.strip()
    return (
        count_hits(text, OFF_TASK_WORDS) >= AGENT_OFFTASK_QUICK_HITS
        or count_hits(text, CONFLICT_WORDS) >= AGENT_CONFLICT_QUICK_HITS
        or text_has_any(text, ["没搞懂", "强行扯", "听我的", "没道理", "又不是组长", "凭什么", "凭什么听你的", "你这个想法不行"])
        or count_hits(text, LOW_MOTIVATION_WORDS) >= 1
        or count_hits(text, FRUSTRATION_WORDS) >= 1
        or text_has_any(text, VALUE_DOUBT_WORDS)
        or text_has_any(text, PASSIVE_DETACHMENT_WORDS)
    )



# ============================================================
# 异步分析调度器：固定 worker 队列版
# ------------------------------------------------------------
# 正式实验 100 人同时在线时，不能每次发消息都新建线程。
# 这里采用固定 worker + 按组去重/合并触发：
# - 同一小组同一时刻最多跑一个分析；
# - 同一小组在队列中最多排一个任务；
# - 分析过程中又出现强信号，会在结束后补跑一次；
# - 普通触发按 AGENT_GROUP_MIN_ANALYSIS_INTERVAL_SECONDS 节流。
# ============================================================


def push_agent_suggestion(suggestion_id, teacher_user_id):
    if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
        return None

    suggestion = query_one("SELECT * FROM agent_suggestions WHERE id=?", (suggestion_id,))
    if not suggestion:
        return None
    if suggestion["status"] != "pending":
        return None
    return execute_intervention(
        suggestion["group_id"],
        {"id": suggestion["decision_id"]} if "decision_id" in suggestion.keys() else None,
        dict(suggestion),
        suggestion=suggestion,
        teacher_user_id=teacher_user_id,
        push_mode="sera_teacher_confirmed",
        log_trigger_source="teacher_confirmed",
    )




def ignore_agent_suggestion(suggestion_id, teacher_user_id, note=""):
    suggestion = query_one("SELECT * FROM agent_suggestions WHERE id=?", (suggestion_id,))
    if not suggestion or suggestion["status"] != "pending":
        return False
    execute(
        """
        UPDATE agent_suggestions
        SET status='ignored', decided_at=?, decided_by_user_id=?, decision_note=?
        WHERE id=?
        """,
        (now_str(), teacher_user_id, note[:300], suggestion_id),
    )
    return True


# -----------------------------
# HTML/CSS
# -----------------------------

