# -*- coding: utf-8 -*-
"""ContextService：收集题目、近期消息、参与情况。"""
from typing import Optional

from db import now_str, query_one, query_all
from config import (
    PIPELINE_V2_ANALYZER_VERSION,
    AGENT_CONTEXT_MINUTES,
    STATE_WINDOW_MINUTES,
)
from services.context_service import collect_group_context as _collect_group_context

CONTEXT_VERSION = f"{PIPELINE_V2_ANALYZER_VERSION}_context_v1"


class ContextService:
    """收集讨论室当前上下文（任务、消息、参与者等）。"""

    @staticmethod
    def collect(
        group_id: int,
        *,
        task_id: int = None,
        session_no: int = None,
        session_id: int = None,
        discussion_id: int = None,
    ) -> dict:
        """收集当前房间的完整上下文。"""
        raw = _collect_group_context(
            group_id,
            task_id=task_id,
            session_no=session_no,
            session_id=session_id,
            discussion_id=discussion_id,
        )
        return raw

    @staticmethod
    def collect_lightweight(group_id: int) -> dict:
        """轻量上下文：仅房间基本信息、最近消息摘要、参与人数。"""
        group = query_one("SELECT * FROM groups WHERE id=?", (group_id,))
        if not group:
            return {}

        now = __import__("datetime").datetime.now()
        since = (now - __import__("datetime").timedelta(minutes=STATE_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        recent_msgs = query_all(
            """
            SELECT m.*, u.real_name, u.username
            FROM messages m
            JOIN users u ON m.user_id=u.id
            WHERE m.group_id=? AND m.created_at>=?
            ORDER BY m.id ASC
            """,
            (group_id, since),
        )

        return {
            "context_version": CONTEXT_VERSION,
            "group_id": group_id,
            "group_state": group.get("state"),
            "last_message_sequence": group.get("last_message_sequence") or 0,
            "cutoff_sequence": group.get("cutoff_sequence") or 0,
            "recent_message_count": len(recent_msgs),
            "recent_messages": [dict(r) for r in recent_msgs],
            "member_count": len({r["user_id"] for r in recent_msgs}),
            "server_time": now_str(),
        }
