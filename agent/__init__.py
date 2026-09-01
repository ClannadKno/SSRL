# -*- coding: utf-8 -*-
"""Stable exports for agent modules used by routes."""
from agent.detector import analyze_group, get_active_task, get_group_member_count, latest_group_state, summarize_context
from agent.trigger import (
    get_latest_agent_suggestion,
    get_latest_pending_suggestion,
    ignore_agent_suggestion,
    is_quick_trigger_message,
    push_agent_suggestion,
    push_intervention,
)

__all__ = [
    "analyze_group",
    "get_active_task",
    "get_group_member_count",
    "get_latest_agent_suggestion",
    "get_latest_pending_suggestion",
    "ignore_agent_suggestion",
    "is_quick_trigger_message",
    "latest_group_state",
    "push_agent_suggestion",
    "push_intervention",
    "summarize_context",
]
