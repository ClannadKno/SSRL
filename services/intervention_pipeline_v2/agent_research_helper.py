# -*- coding: utf-8 -*-
"""Agent research event and tracking helpers for V2 intervention pipeline.

Provides safe wrappers around DB research event operations, data builders for
trigger_reason_json and teacher_config_snapshot, and canonical mode checks.
"""
import json
import logging
from typing import Optional

from db import (
    now_str,
    query_one,
    get_session_agent_config,
    create_agent_research_event,
    update_agent_research_event,
    get_active_session_id,
    get_current_running_session_context,
)

logger = logging.getLogger(__name__)


def check_strategy_agent_enabled(group_id: int) -> bool:
    config = get_session_agent_config(group_id=group_id)
    return config.get("agent_mode") == "strategy"


def build_trigger_reason_json(monitor_run: dict) -> dict:
    rule_result = {}
    llm_result = {}
    try:
        raw = monitor_run.get("rule_result_json")
        if raw:
            rule_result = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        raw = monitor_run.get("llm_result_json")
        if raw:
            llm_result = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "monitor_run_id": monitor_run.get("id"),
        "cutoff_sequence": monitor_run.get("cutoff_sequence"),
        "trigger_type": monitor_run.get("trigger_type"),
        "final_state": monitor_run.get("final_state"),
        "confidence": monitor_run.get("confidence"),
        "rule_result_json": rule_result,
        "llm_result_json": llm_result,
    }


def build_teacher_config_snapshot(group_id: int):
    try:
        ctx = get_current_running_session_context()
        if not ctx:
            return None
        session_id = ctx.get("session_id")
        if not session_id:
            return None
        row = query_one(
            """SELECT s.id, s.task_id, s.session_no,
                      s.agent_mode,
                      g.condition
               FROM experiment_sessions s
               LEFT JOIN groups g ON g.id=?
               WHERE s.id=?""",
            (group_id, session_id),
        )
        if not row:
            return None
        d = dict(row)
        config = get_session_agent_config(session_id=d["id"])
        return json.dumps({
            "session_id": d["id"],
            "task_id": d["task_id"],
            "session_no": d["session_no"],
            "group_id": group_id,
            "condition": d.get("condition"),
            "agent_mode": config.get("agent_mode"),
            "strategy_agent_enabled": bool(config.get("strategy_agent_enabled")),
            "emotion_agent_enabled": bool(config.get("emotion_agent_enabled")),
            "configuration_error": config.get("configuration_error"),
        }, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Failed to build teacher config snapshot: %s", exc)
        return None


def safe_create_agent_event(**kwargs):
    try:
        return create_agent_research_event(**kwargs)
    except Exception as exc:
        logger.warning("Failed to create agent_research_event: %s", exc)
        return None


def safe_update_agent_event(event_id, **kwargs):
    if event_id is None:
        return
    try:
        update_agent_research_event(event_id, **kwargs)
    except Exception as exc:
        logger.warning("Failed to update agent_research_event %s: %s", event_id, exc)


def find_agent_event_for_run(intervention_run_id: int):
    try:
        row = query_one(
            "SELECT id FROM agent_research_events WHERE intervention_run_id=? ORDER BY id ASC LIMIT 1",
            (intervention_run_id,),
        )
        return int(row["id"]) if row else None
    except Exception as exc:
        logger.warning("Failed to find agent_event for run %s: %s", intervention_run_id, exc)
        return None


def build_validation_json(validation_result: dict):
    try:
        checks = validation_result.get("checks", {})
        return json.dumps({
            "length_check": checks.get("length_ok", False),
            "no_direct_answer_check": checks.get("no_answer", False),
            "no_public_criticism_check": checks.get("no_criticism", False),
            "no_forbidden_phrases_check": checks.get("no_forbidden_phrases", False),
            "no_duplicate_check": True,
            "final_passed": validation_result.get("valid", False),
            "fallback_reason": validation_result.get("reason"),
            "action": validation_result.get("action"),
        }, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Failed to build validation_json: %s", exc)
        return None


def update_intervention_run_research(run_id: int, **kwargs):
    sets = []
    params = []
    allowed = {
        "trigger_reason_json", "teacher_config_snapshot_json",
        "llm_context_json", "llm_prompt_json", "llm_response_json",
        "validation_json", "actual_started_at", "actual_published_at",
        "skip_reason", "trigger_type",
    }
    for col, val in kwargs.items():
        if col in allowed and val is not None:
            sets.append("{0}=?".format(col))
            params.append(val)
    if not sets:
        return
    params.append(run_id)
    try:
        from db import execute as db_exec
        sql = "UPDATE intervention_runs SET {0} WHERE id=?".format(", ".join(sets))
        db_exec(sql, tuple(params))
    except Exception as exc:
        logger.warning("Failed to update research columns for run %s: %s", run_id, exc)
