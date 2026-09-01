# -*- coding: utf-8 -*-
"""Research-oriented, full-scope teacher exports.

This module is the single source of truth for the teacher export contract.  It
deliberately does not mirror database tables: every CSV has a fixed schema and
research files are partitioned under ``sessions/<session>/<group>`` except for
questionnaire files, which are aggregated under the session directory.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser

from db import query_all
from services.export_safety import (
    build_export_filename,
    safe_group_dir,
    safe_questionnaire_filename,
    safe_session_dir,
)
from services.three_stage_route_manifest import (
    OPTIONAL_SUPPORT,
    REQUIRED_INTERVENTION,
    SUPPRESS,
    route_for_canonical_state,
)
from services.three_stage_schema import is_primary_sub_state


logger = logging.getLogger(__name__)

PACKAGE_FORMAT_VERSION = "1.0"
CSV_ENCODING = "utf-8-sig"
EXPORT_MODE = "full_nonblinded"
PATH_STRUCTURE = "sessions/session/group[/questionnaires/participant]/file"
QUESTIONNAIRE_PATH_STRUCTURE = "sessions/session/questionnaires/file"

MESSAGES_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "message_id",
    "sequence",
    "sender_role",
    "participant_code",
    "agent_type",
    "agent_event_id",
    "agent_reference_code",
    "state_code",
    "state_assignment_source",
    "selected_strategy_id",
    "content",
    "reply_to_message_id",
    "created_at",
]

STATE_ASSESSMENTS_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "assessment_id",
    "pipeline_run_id",
    "trigger_source",
    "window_start_sequence",
    "window_end_sequence",
    "state_code",
    "state_overlays",
    "confidence",
    "evidence_message_ids",
    "assessment_status",
    "failure_code",
    "latency_ms",
    "model_name",
    "prompt_version",
    "created_at",
]

STRATEGY_PIPELINE_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "pipeline_run_id",
    "trigger_source",
    "trigger_message_id",
    "input_start_sequence",
    "input_end_sequence",
    "stage1_need_intervention",
    "stage1_candidate_states",
    "state_assessment_id",
    "state_code",
    "state_overlays",
    "routing_type",
    "candidate_strategy_ids",
    "selected_strategy_id",
    "inhibition_strategy_id",
    "strategy_selection_reason",
    "generation_status",
    "publish_status",
    "published_message_id",
    "skip_reason",
    "failure_code",
    "stage1_latency_ms",
    "stage2_latency_ms",
    "stage3_latency_ms",
    "total_latency_ms",
    "created_at",
    "completed_at",
]

INTERVENTIONS_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "intervention_id",
    "pipeline_run_id",
    "message_id",
    "trigger_source",
    "trigger_message_id",
    "state_code",
    "selected_strategy_id",
    "content",
    "published_at",
]

PARTICIPATION_SUMMARY_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "participant_code",
    "message_count",
    "character_count",
    "active_minutes",
    "first_message_at",
    "last_message_at",
]

EMOTION_CHECKINS_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "checkin_id",
    "participant_code",
    "checkin_type",
    "emotion_option",
    "positivity",
    "engagement",
    "atmosphere",
    "expression_willingness",
    "note",
    "created_at",
]

EMOTION_FEEDBACK_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "discussion_id",
    "slot_index",
    "previous_window_start",
    "previous_window_end",
    "current_window_start",
    "current_window_end",
    "emotion_feedback_state",
    "confidence",
    "comparison_summary",
    "current_window_summary",
    "previous_window_summary",
    "evidence_message_ids",
    "current_metrics",
    "previous_metrics",
    "final_text",
    "fallback_used",
    "slot_status",
    "failure_reason",
    "created_at",
    "published_at",
    "nearest_previous_canonical_state",
    "nearest_next_canonical_state",
]

HELP_REQUESTS_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "help_request_id",
    "participant_code",
    "request_text",
    "intent",
    "source_message_id",
    "status",
    "response_message_id",
    "pipeline_run_id",
    "failure_code",
    "created_at",
    "completed_at",
]

QUESTIONNAIRE_EXPORT_COLUMNS = [
    "session_id",
    "group_id",
    "participant_code",
]

QUESTIONNAIRE_ITEMS_EXPORT_COLUMNS = [
    "questionnaire_code",
    "item_code",
    "dimension_label",
    "prompt_text",
]

EXPORT_SCHEMAS = {
    "messages": ("messages.csv", MESSAGES_EXPORT_COLUMNS),
    "state-assessments": ("state_assessments.csv", STATE_ASSESSMENTS_EXPORT_COLUMNS),
    "strategy-pipeline": ("strategy_pipeline.csv", STRATEGY_PIPELINE_EXPORT_COLUMNS),
    "interventions": ("interventions.csv", INTERVENTIONS_EXPORT_COLUMNS),
    "participation": ("participation_summary.csv", PARTICIPATION_SUMMARY_EXPORT_COLUMNS),
    "emotion-checkins": ("emotion_checkins.csv", EMOTION_CHECKINS_EXPORT_COLUMNS),
    "emotion-feedback": ("emotion_feedback.csv", EMOTION_FEEDBACK_EXPORT_COLUMNS),
    "help-requests": ("help_requests.csv", HELP_REQUESTS_EXPORT_COLUMNS),
    "questionnaires": (None, QUESTIONNAIRE_EXPORT_COLUMNS),
}

GROUP_LEVEL_EXPORT_KEYS = (
    "messages",
    "state-assessments",
    "strategy-pipeline",
    "interventions",
    "participation",
    "emotion-checkins",
    "emotion-feedback",
    "help-requests",
)

SCHEMA_VERSIONS = {
    "messages.csv": "2.0",
    "state_assessments.csv": "2.0",
    "strategy_pipeline.csv": "1.0",
    "interventions.csv": "1.0",
    "participation_summary.csv": "1.0",
    "emotion_checkins.csv": "1.0",
    "emotion_feedback.csv": "1.0",
    "help_requests.csv": "1.0",
}

HELP_REQUEST_RESPONSE = "HELP_REQUEST_RESPONSE"

_EMOTION_FEEDBACK_STATES = {
    "GROUP_EXCELLENT",
    "GROUP_IMPROVING",
    "GROUP_DECLINING",
    "GROUP_LOW_PARTICIPATION",
    "GROUP_SUSTAINED_EXCELLENT",
}

RESEARCH_EXPORT_KEYS = (
    "messages",
    "state-assessments",
    "strategy-pipeline",
    "interventions",
    "participation",
    "emotion-checkins",
    "emotion-feedback",
    "help-requests",
    "deliverables",
    "questionnaires",
)

EXPORT_DESCRIPTIONS = {
    "messages": "完整讨论消息",
    "state-assessments": "第二阶段状态判断及证据",
    "strategy-pipeline": "三阶段状态路由、策略选择和发布结果",
    "interventions": "真正发布给学生的 Agent 介入",
    "participation": "成员级基础参与统计",
    "emotion-checkins": "学生原始情绪签到",
    "emotion-feedback": "固定时间槽的群体情绪反馈、证据和发布结果",
    "help-requests": "学生主动求助及系统响应关联",
    "deliverables": "小组最终提交的 Markdown 成果",
    "questionnaires": "Session 级问卷的逐题原始回答",
}

_LEGACY_SUB_STATE_MAP = {
    "positive_collaboration": "standard",
    "conflict_tension": "interpersonal_conflict",
    "blocked_frustration": "frustration",
    "task_detached": "perfunctory_detachment",
    "participation_imbalance": "individual_marginalization",
    "coordination_disorder": "confusion",
    "cognitive_overload": "high_intensity_overload",
    "negative_emotion": "frustration",
    "off_task": "off_topic_unregulated",
    "unknown": "unknown_sub_state",
}


def _dict_rows(sql, params=()):
    return [dict(row) for row in query_all(sql, params)]


def _clean(value):
    return "" if value is None else value


def _as_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value):
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return 0 if value.strip().lower() in {"0", "false", "no", "off"} else 1
    return 1 if bool(value) else 0


def _json_value(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _int_list(value):
    parsed = _json_value(value, value)
    if isinstance(parsed, dict):
        parsed = list(parsed.values())
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]
    result = []
    for item in parsed:
        number = _as_int(item)
        if number is not None and number not in result:
            result.append(number)
    return result


def _string_list(value):
    parsed = _json_value(value, value)
    if isinstance(parsed, dict):
        parsed = list(parsed.keys())
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [] if parsed in (None, "") else [parsed]
    result = []
    for item in parsed:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _json_array(value):
    return json.dumps(_string_list(value), ensure_ascii=False, separators=(",", ":"))


def _json_int_array(values):
    return json.dumps([int(v) for v in values], ensure_ascii=False, separators=(",", ":"))


def _parse_time(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _elapsed_ms(start, end):
    first = _parse_time(start)
    last = _parse_time(end)
    if not first or not last:
        return ""
    return max(0, int((last - first).total_seconds() * 1000))


def _canonical_sub_state(value):
    code = str(value or "").strip()
    if is_primary_sub_state(code):
        return code
    return _LEGACY_SUB_STATE_MAP.get(code, "unknown_sub_state")


def _assessment_status(value, failure_code=None):
    status = str(value or "").strip().lower()
    if failure_code or status in {"failed", "error", "retry_exhausted"}:
        return "failed"
    if status in {"skipped", "cancelled", "canceled", "superseded"}:
        return "skipped"
    return "succeeded"


def _failure_code(value, fallback=""):
    text = str(value or "").strip()
    if not text:
        return fallback
    token = re.split(r"[\s:;，。]+", text, maxsplit=1)[0]
    return token[:80]


def _extract_evidence_values(*payloads):
    result = []
    keys = {
        "evidence_message_ids",
        "evidence_message_ids_json",
        "evidence_sequences",
        "message_ids",
    }

    def visit(value):
        parsed = _json_value(value, value)
        if isinstance(parsed, dict):
            for key, item in parsed.items():
                if key in keys:
                    for number in _int_list(item):
                        if number not in result:
                            result.append(number)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, (dict, list)):
                    visit(item)

    for payload in payloads:
        visit(payload)
    return result


def _message_indexes():
    by_sequence = {}
    by_id = {}
    rows = _dict_rows(
        "SELECT id, session_id, group_id, sequence FROM messages "
        "WHERE session_id IS NOT NULL AND group_id IS NOT NULL"
    )
    for row in rows:
        scope = (_as_int(row["session_id"]), _as_int(row["group_id"]))
        message_id = _as_int(row["id"])
        sequence = _as_int(row["sequence"])
        if message_id is not None:
            by_id[(scope, message_id)] = message_id
        if sequence is not None and message_id is not None:
            by_sequence[(scope, sequence)] = message_id
    return by_sequence, by_id


def _message_ids_for_evidence(scope, values, indexes):
    by_sequence, by_id = indexes
    result = []
    for number in values:
        # Historical evidence_message_ids columns contain message sequences.
        message_id = by_sequence.get((scope, number)) or by_id.get((scope, number))
        if message_id is not None and message_id not in result:
            result.append(message_id)
    return result


def _normalized_sender_role(row):
    raw_role = str(
        row.get("role") or row.get("user_role") or row.get("sender_type") or ""
    ).lower()
    if raw_role in {"admin", "researcher"}:
        return "teacher"
    if raw_role in {"student", "agent", "teacher"}:
        return raw_role
    return raw_role


def _published_emotion_events():
    """Return export-only links for emotion feedback that reached the message stream."""
    rows = _dict_rows(
        """
        SELECT ers.id AS slot_id, ers.session_id, ers.group_id,
               ers.discussion_id, ers.slot_index, ers.status AS slot_status,
               COALESCE(efg.published_message_id, ers.message_id, ir.message_id) AS message_id,
               COALESCE(efa.emotion_feedback_state,
                        efg.emotion_feedback_state,
                        ir.emotion_feedback_type_code) AS emotion_feedback_state,
               COALESCE(efg.final_text, ir.final_visible_message, em.content) AS final_text,
               COALESCE(efg.published_at, ir.actual_published_at,
                        ers.completed_at) AS published_at,
               efg.status AS generation_status
          FROM emotion_reflection_slots AS ers
          LEFT JOIN emotion_feedback_assessments AS efa ON efa.slot_id=ers.id
          LEFT JOIN emotion_feedback_generations AS efg
            ON efg.id=(
                SELECT candidate.id
                 FROM emotion_feedback_generations AS candidate
                 WHERE candidate.slot_id=ers.id
                   AND UPPER(COALESCE(candidate.status, ''))='PUBLISHED'
                   AND candidate.published_message_id IS NOT NULL
                 ORDER BY candidate.attempt_no DESC, candidate.id DESC
                 LIMIT 1
            )
          LEFT JOIN intervention_runs AS ir ON ir.id=ers.intervention_run_id
          LEFT JOIN messages AS em
            ON em.id=COALESCE(efg.published_message_id, ers.message_id, ir.message_id)
         WHERE LOWER(COALESCE(ers.status, ''))='sent'
            OR UPPER(COALESCE(efg.status, ''))='PUBLISHED'
         ORDER BY ers.session_id, ers.group_id, ers.slot_index, ers.id
        """
    )
    result = []
    for row in rows:
        feedback_state = str(row.get("emotion_feedback_state") or "").strip()
        if feedback_state not in _EMOTION_FEEDBACK_STATES:
            continue
        row["agent_event_id"] = "EF-%s-%s-%s-%s" % (
            row.get("session_id"),
            row.get("group_id"),
            row.get("discussion_id"),
            row.get("slot_index"),
        )
        result.append(row)
    return result


def _time_distance_seconds(first, second):
    left = _parse_time(first)
    right = _parse_time(second)
    if not left or not right:
        return float("inf")
    try:
        return abs((left - right).total_seconds())
    except TypeError:
        return abs((left.replace(tzinfo=None) - right.replace(tzinfo=None)).total_seconds())


def _resolve_emotion_message_links(message_rows, events):
    """Resolve published emotion events without choosing an ambiguous candidate."""
    messages_by_id = {
        _as_int(row.get("message_id")): row
        for row in message_rows
        if _as_int(row.get("message_id")) is not None
    }
    resolved = {}
    warnings = []
    matched_event_ids = set()
    content_mismatches = 0

    for event in events:
        event_key = _as_int(event.get("slot_id"))
        direct_message_id = _as_int(event.get("message_id"))
        direct_message = messages_by_id.get(direct_message_id)
        if direct_message is not None:
            if str(direct_message.get("content") or "") != str(event.get("final_text") or ""):
                content_mismatches += 1
                warnings.append(
                    "Published emotion slot %s links to message %s with different content."
                    % (event.get("slot_id"), direct_message_id)
                )
            resolved[direct_message_id] = event
            matched_event_ids.add(event_key)
            continue

        candidates = []
        for message in message_rows:
            if _normalized_sender_role(message) != "agent":
                continue
            if str(message.get("agent_type") or "").strip().lower() != "emotion":
                continue
            if (
                _as_int(message.get("session_id")) != _as_int(event.get("session_id"))
                or _as_int(message.get("group_id")) != _as_int(event.get("group_id"))
                or str(message.get("content") or "") != str(event.get("final_text") or "")
            ):
                continue
            candidates.append(
                (
                    _time_distance_seconds(
                        message.get("created_at"), event.get("published_at")
                    ),
                    _as_int(message.get("message_id")),
                    message,
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1] or 0))
        if candidates:
            minimum_distance = candidates[0][0]
            nearest = [item for item in candidates if item[0] == minimum_distance]
            if len(nearest) == 1 and nearest[0][1] is not None:
                resolved[nearest[0][1]] = event
                matched_event_ids.add(event_key)
                continue
            warnings.append(
                "Published emotion slot %s has multiple equally near message matches."
                % event.get("slot_id")
            )

    return resolved, {
        "published_event_count": len(events),
        "matched_event_count": len(matched_event_ids),
        "unmatched_event_count": len(events) - len(matched_event_ids),
        "content_mismatches": content_mismatches,
        "warnings": warnings,
    }


def _load_messages_with_diagnostics():
    state_rows = _dict_rows(
        """
        SELECT session_id, group_id, start_sequence, end_sequence,
               canonical_sub_state_code, state_code, is_finalized,
               COALESCE(sub_state_confidence, confidence, 0) AS confidence,
               created_at, id
          FROM collaboration_state_segments
         WHERE segment_kind='message_range'
           AND start_sequence IS NOT NULL
           AND end_sequence IS NOT NULL
         ORDER BY is_finalized DESC, confidence DESC, created_at DESC, id DESC
        """
    )
    states_by_scope = defaultdict(list)
    for state in state_rows:
        scope = (_as_int(state.get("session_id")), _as_int(state.get("group_id")))
        states_by_scope[scope].append(state)

    rows = _dict_rows(
        """
        SELECT m.session_id, m.group_id, m.id AS message_id, m.sequence,
               m.sender_type, m.role, u.role AS user_role,
               COALESCE(NULLIF(ep.participant_code, ''),
                        NULLIF(u.participant_code, '')) AS source_participant_code,
               m.agent_type,
               COALESCE(
                   spr.selected_strategy_id,
                   ir.selected_strategy_id,
                   il.strategy_id,
                   m.strategy_id
               ) AS resolved_strategy_id,
               m.content, m.reply_to_message_id, m.created_at
          FROM messages AS m
          LEFT JOIN users AS u ON u.id=m.user_id
          LEFT JOIN experiment_participants AS ep
            ON ep.user_id=m.user_id AND ep.group_id=m.group_id
          LEFT JOIN intervention_logs AS il ON il.id=m.linked_log_id
          LEFT JOIN intervention_runs AS ir
            ON ir.id=COALESCE(m.intervention_run_id, il.intervention_run_id, il.intervention_id)
          LEFT JOIN strategy_pipeline_runs AS spr
            ON spr.id=ir.strategy_pipeline_run_id
         ORDER BY m.session_id, m.group_id, m.sequence, m.id
        """
    )
    help_by_message = {
        _as_int(row.get("response_message_id")): row
        for row in _dict_rows(
            """
            SELECT id AS help_request_id, response_message_id
              FROM help_requests
             WHERE response_message_id IS NOT NULL
             ORDER BY id
            """
        )
        if _as_int(row.get("response_message_id")) is not None
    }
    intervention_by_message = {}
    for intervention in _load_interventions():
        message_id = _as_int(intervention.get("message_id"))
        if message_id is not None:
            intervention_by_message.setdefault(message_id, intervention)

    emotion_events = _published_emotion_events()
    emotion_by_message, diagnostics = _resolve_emotion_message_links(rows, emotion_events)
    result = []
    for row in rows:
        role = _normalized_sender_role(row)
        message_id = _as_int(row.get("message_id"))
        state_code = ""
        state_assignment_source = ""
        if role == "student":
            sequence = _as_int(row.get("sequence"))
            scope = (_as_int(row.get("session_id")), _as_int(row.get("group_id")))
            if sequence is not None:
                for state in states_by_scope.get(scope, []):
                    start_sequence = _as_int(state.get("start_sequence"))
                    end_sequence = _as_int(state.get("end_sequence"))
                    if (
                        start_sequence is not None
                        and end_sequence is not None
                        and start_sequence <= sequence <= end_sequence
                    ):
                        state_code = _canonical_sub_state(
                            state.get("canonical_sub_state_code") or state.get("state_code")
                        )
                        state_assignment_source = "detected"
                        break
            if not state_code:
                state_code = "standard"
                state_assignment_source = "export_fallback"

        agent_type = ""
        agent_event_id = ""
        agent_reference_code = ""
        selected_strategy_id = ""
        if role == "agent":
            help_request = help_by_message.get(message_id)
            emotion_event = emotion_by_message.get(message_id)
            intervention = intervention_by_message.get(message_id)
            if help_request is not None:
                agent_type = "help"
                agent_event_id = help_request.get("help_request_id")
                agent_reference_code = HELP_REQUEST_RESPONSE
            elif emotion_event is not None:
                agent_type = "emotion"
                agent_event_id = emotion_event.get("agent_event_id")
                agent_reference_code = emotion_event.get("emotion_feedback_state")
            elif intervention is not None:
                agent_type = "strategy"
                agent_event_id = intervention.get("intervention_id")
                selected_strategy_id = (
                    intervention.get("selected_strategy_id")
                    or row.get("resolved_strategy_id")
                )
                agent_reference_code = selected_strategy_id
            else:
                original_type = str(row.get("agent_type") or "").strip().lower()
                if original_type in {"strategy", "emotion", "help"}:
                    agent_type = original_type
                if agent_type == "strategy":
                    selected_strategy_id = row.get("resolved_strategy_id")
                    agent_reference_code = selected_strategy_id
                elif agent_type == "help":
                    agent_reference_code = HELP_REQUEST_RESPONSE
        result.append({
            "session_id": row.get("session_id"),
            "group_id": row.get("group_id"),
            "message_id": row.get("message_id"),
            "sequence": row.get("sequence"),
            "sender_role": role,
            "participant_code": row.get("source_participant_code") if role == "student" else "",
            "agent_type": agent_type,
            "agent_event_id": agent_event_id,
            "agent_reference_code": agent_reference_code,
            "state_code": state_code,
            "state_assignment_source": state_assignment_source,
            "selected_strategy_id": selected_strategy_id,
            "content": row.get("content"),
            "reply_to_message_id": row.get("reply_to_message_id"),
            "created_at": row.get("created_at"),
        })
    return result, diagnostics


def _load_messages():
    return _load_messages_with_diagnostics()[0]


def _segment_assessment_id(row):
    return row.get("assessment_id") or "segment-%s" % row.get("segment_id")


def _export_state_code(value):
    code = str(value or "").strip()
    if not code or code.upper() in _EMOTION_FEEDBACK_STATES:
        return ""
    return _canonical_sub_state(code)


def _stage2_was_started(row):
    status = str(row.get("stage2_status") or row.get("batch_status") or "").strip().lower()
    if row.get("stage2_started_at") or row.get("batch_started_at"):
        return True
    return status in {
        "running",
        "succeeded",
        "success",
        "completed",
        "complete",
        "failed",
        "error",
        "retry_exhausted",
    }


def _state_candidate_rank(candidate, current_prompt_version):
    status = str(candidate.get("assessment_status") or "").strip().lower()
    return (
        1 if status in {"succeeded", "success", "completed", "complete", "confirmed"} else 0,
        1 if str(candidate.get("state_code") or "").strip() else 0,
        1 if candidate.get("_explicit_reference") else 0,
        1
        if current_prompt_version
        and candidate.get("prompt_version") == current_prompt_version
        else 0,
        str(candidate.get("created_at") or ""),
        str(candidate.get("assessment_id") or ""),
    )


def _load_state_assessments_with_diagnostics():
    """Build one canonical Stage-2 record per formal strategy pipeline."""
    indexes = _message_indexes()
    pipeline_rows = _dict_rows(
        """
        SELECT spr.id AS pipeline_run_id, spr.session_id, spr.group_id,
               spr.trigger_source, spr.input_start_sequence, spr.input_end_sequence,
               spr.stage2_status, spr.stage2_started_at, spr.stage2_completed_at,
               spr.canonical_sub_state_code, spr.raw_sub_state_code,
               spr.secondary_sub_state_tags_json, spr.sub_state_confidence,
               spr.sub_state_evidence_message_ids_json,
               spr.state_model_name, spr.state_prompt_version,
               spr.failure_code, spr.created_at, spr.assessment_batch_id,
               sab.status AS batch_status, sab.error_code AS batch_error_code,
               sab.model AS batch_model, sab.prompt_version AS batch_prompt_version,
               sab.started_at AS batch_started_at,
               sab.completed_at AS batch_completed_at,
               sab.candidate_start_sequence, sab.candidate_end_sequence,
               sab.student_sequences_json
          FROM strategy_pipeline_runs AS spr
          LEFT JOIN state_assessment_batches AS sab ON sab.id=spr.assessment_batch_id
         ORDER BY spr.session_id, spr.group_id, spr.created_at, spr.id
        """
    )
    pipeline_by_id = {
        _as_int(row.get("pipeline_run_id")): row for row in pipeline_rows
    }
    pipeline_ids_by_batch = defaultdict(list)
    for row in pipeline_rows:
        batch_id = _as_int(row.get("assessment_batch_id"))
        if batch_id is not None:
            pipeline_ids_by_batch[batch_id].append(_as_int(row.get("pipeline_run_id")))

    candidates_by_pipeline = defaultdict(list)
    segment_rows = _dict_rows(
        """
        SELECT css.id AS segment_id, css.session_id, css.group_id,
               css.assessment_id, css.strategy_pipeline_run_id,
               css.assessment_batch_id, css.start_sequence, css.end_sequence,
               css.evidence_sequences, css.evidence_message_ids_json,
               css.canonical_sub_state_code, css.state_code,
               css.secondary_tags_json, css.sub_state_confidence, css.confidence,
               css.assessment_status, css.fallback_reason, css.trigger_type,
               css.detected_at, css.created_at,
               css.prompt_version AS segment_prompt_version,
               sab.status AS batch_status, sab.error_code,
               sab.model AS batch_model, sab.prompt_version AS batch_prompt_version,
               sab.started_at AS batch_started_at,
               sab.completed_at AS batch_completed_at
          FROM collaboration_state_segments AS css
          LEFT JOIN state_assessment_batches AS sab ON sab.id=css.assessment_batch_id
         WHERE css.strategy_pipeline_run_id IS NOT NULL
            OR css.assessment_batch_id IS NOT NULL
         ORDER BY css.created_at, css.id
        """
    )
    for row in segment_rows:
        direct_pipeline_id = _as_int(row.get("strategy_pipeline_run_id"))
        target_pipeline_ids = []
        if direct_pipeline_id in pipeline_by_id:
            target_pipeline_ids.append(direct_pipeline_id)
        for pipeline_id in pipeline_ids_by_batch.get(
            _as_int(row.get("assessment_batch_id")), []
        ):
            if pipeline_id is not None and pipeline_id not in target_pipeline_ids:
                target_pipeline_ids.append(pipeline_id)
        for pipeline_id in target_pipeline_ids:
            pipeline = pipeline_by_id[pipeline_id]
            raw_state_code = (
                row.get("canonical_sub_state_code") or row.get("state_code")
            )
            if str(raw_state_code or "").strip().upper() in _EMOTION_FEEDBACK_STATES:
                continue
            scope = (
                _as_int(pipeline.get("session_id")),
                _as_int(pipeline.get("group_id")),
            )
            evidence_values = _int_list(row.get("evidence_sequences")) or _int_list(
                row.get("evidence_message_ids_json")
            )
            evidence_ids = _message_ids_for_evidence(scope, evidence_values, indexes)
            failure = _failure_code(row.get("error_code") or row.get("fallback_reason"))
            candidates_by_pipeline[pipeline_id].append({
                "session_id": pipeline.get("session_id"),
                "group_id": pipeline.get("group_id"),
                "assessment_id": _segment_assessment_id(row),
                "pipeline_run_id": pipeline_id,
                "trigger_source": row.get("trigger_type") or pipeline.get("trigger_source"),
                "window_start_sequence": row.get("start_sequence"),
                "window_end_sequence": row.get("end_sequence"),
                "state_code": _export_state_code(raw_state_code),
                "state_overlays": _json_array(row.get("secondary_tags_json")),
                "confidence": row.get("sub_state_confidence")
                if row.get("sub_state_confidence") is not None
                else row.get("confidence"),
                "evidence_message_ids": _json_int_array(evidence_ids),
                "assessment_status": _assessment_status(
                    row.get("batch_status") or row.get("assessment_status"), failure
                ),
                "failure_code": failure,
                "latency_ms": _elapsed_ms(
                    row.get("batch_started_at"), row.get("batch_completed_at")
                ),
                "model_name": row.get("batch_model"),
                "prompt_version": row.get("batch_prompt_version")
                or row.get("segment_prompt_version"),
                "created_at": row.get("detected_at") or row.get("created_at"),
                "_explicit_reference": direct_pipeline_id == pipeline_id,
                "_candidate_key": "segment:%s" % row.get("segment_id"),
            })

    legacy_rows = _dict_rows(
        """
        SELECT ir.strategy_pipeline_run_id AS pipeline_run_id,
               sa.id AS assessment_id, sa.session_id, sa.group_id,
               sa.window_start, sa.window_end, sa.fused_state_code,
               sa.llm_state_code, sa.state_code, sa.confidence, sa.state_score,
               sa.assessment_status, sa.error_message,
               sa.latency_ms, sa.model_name, sa.prompt_version, sa.created_at,
               sa.evidence, sa.evidence_summary, sa.rule_assessment_json,
               sa.llm_assessment_json, sa.fusion_json, sa.context_json,
               ir.trigger_type AS trigger_source
          FROM intervention_runs AS ir
          JOIN state_assessments AS sa ON sa.id=ir.state_assessment_id
         WHERE ir.strategy_pipeline_run_id IS NOT NULL
         ORDER BY ir.strategy_pipeline_run_id, sa.created_at, sa.id
        """
    )
    for row in legacy_rows:
        pipeline_id = _as_int(row.get("pipeline_run_id"))
        pipeline = pipeline_by_id.get(pipeline_id)
        if pipeline is None:
            continue
        raw_state_code = (
            row.get("fused_state_code") or row.get("llm_state_code") or row.get("state_code")
        )
        if str(raw_state_code or "").strip().upper() in _EMOTION_FEEDBACK_STATES:
            continue
        scope = (
            _as_int(pipeline.get("session_id")),
            _as_int(pipeline.get("group_id")),
        )
        evidence_values = _extract_evidence_values(
            row.get("evidence"),
            row.get("rule_assessment_json"),
            row.get("llm_assessment_json"),
            row.get("fusion_json"),
            row.get("context_json"),
        )
        evidence_ids = _message_ids_for_evidence(scope, evidence_values, indexes)
        start_sequence = _as_int(row.get("window_start"))
        end_sequence = _as_int(row.get("window_end"))
        if evidence_values:
            start_sequence = start_sequence or min(evidence_values)
            end_sequence = end_sequence or max(evidence_values)
        payloads = [
            _json_value(row.get("llm_assessment_json"), {}),
            _json_value(row.get("fusion_json"), {}),
        ]
        overlays = []
        for payload in payloads:
            if isinstance(payload, dict):
                overlays.extend(
                    _string_list(payload.get("state_overlays") or payload.get("secondary_tags"))
                )
        failure = _failure_code(row.get("error_message"))
        candidates_by_pipeline[pipeline_id].append({
            "session_id": pipeline.get("session_id"),
            "group_id": pipeline.get("group_id"),
            "assessment_id": row.get("assessment_id"),
            "pipeline_run_id": pipeline_id,
            "trigger_source": row.get("trigger_source"),
            "window_start_sequence": start_sequence,
            "window_end_sequence": end_sequence,
            "state_code": _export_state_code(raw_state_code),
            "state_overlays": json.dumps(overlays, ensure_ascii=False, separators=(",", ":")),
            "confidence": row.get("confidence")
            if row.get("confidence") is not None
            else row.get("state_score"),
            "evidence_message_ids": _json_int_array(evidence_ids),
            "assessment_status": _assessment_status(row.get("assessment_status"), failure),
            "failure_code": failure,
            "latency_ms": row.get("latency_ms"),
            "model_name": row.get("model_name"),
            "prompt_version": row.get("prompt_version"),
            "created_at": row.get("created_at"),
            "_explicit_reference": True,
            "_candidate_key": "legacy:%s" % row.get("assessment_id"),
        })

    result = []
    removed = 0
    for pipeline in pipeline_rows:
        pipeline_id = _as_int(pipeline.get("pipeline_run_id"))
        candidates = candidates_by_pipeline.get(pipeline_id, [])
        unique_candidates = {}
        for candidate in candidates:
            key = candidate.get("_candidate_key")
            previous = unique_candidates.get(key)
            if previous is None or _state_candidate_rank(
                candidate, pipeline.get("state_prompt_version")
            ) > _state_candidate_rank(previous, pipeline.get("state_prompt_version")):
                unique_candidates[key] = candidate
        candidates = list(unique_candidates.values())

        if not candidates and _stage2_was_started(pipeline):
            scope = (
                _as_int(pipeline.get("session_id")),
                _as_int(pipeline.get("group_id")),
            )
            evidence_values = _int_list(
                pipeline.get("student_sequences_json")
                or pipeline.get("sub_state_evidence_message_ids_json")
            )
            evidence_ids = _message_ids_for_evidence(scope, evidence_values, indexes)
            failure = _failure_code(
                pipeline.get("batch_error_code") or pipeline.get("failure_code")
            )
            batch_id = _as_int(pipeline.get("assessment_batch_id"))
            candidates = [{
                "session_id": pipeline.get("session_id"),
                "group_id": pipeline.get("group_id"),
                "assessment_id": "batch-%s" % batch_id
                if batch_id is not None
                else "pipeline-%s-stage2" % pipeline_id,
                "pipeline_run_id": pipeline_id,
                "trigger_source": pipeline.get("trigger_source"),
                "window_start_sequence": pipeline.get("candidate_start_sequence")
                or pipeline.get("input_start_sequence"),
                "window_end_sequence": pipeline.get("candidate_end_sequence")
                or pipeline.get("input_end_sequence"),
                "state_code": _export_state_code(
                    pipeline.get("canonical_sub_state_code")
                    or pipeline.get("raw_sub_state_code")
                ),
                "state_overlays": _json_array(
                    pipeline.get("secondary_sub_state_tags_json")
                ),
                "confidence": pipeline.get("sub_state_confidence"),
                "evidence_message_ids": _json_int_array(evidence_ids),
                "assessment_status": _assessment_status(
                    pipeline.get("batch_status") or pipeline.get("stage2_status"),
                    failure,
                ),
                "failure_code": failure,
                "latency_ms": _elapsed_ms(
                    pipeline.get("batch_started_at") or pipeline.get("stage2_started_at"),
                    pipeline.get("batch_completed_at")
                    or pipeline.get("stage2_completed_at"),
                ),
                "model_name": pipeline.get("batch_model")
                or pipeline.get("state_model_name"),
                "prompt_version": pipeline.get("batch_prompt_version")
                or pipeline.get("state_prompt_version"),
                "created_at": pipeline.get("batch_started_at")
                or pipeline.get("stage2_started_at")
                or pipeline.get("created_at"),
                "_explicit_reference": False,
                "_candidate_key": "synthetic:%s" % pipeline_id,
            }]

        if not candidates:
            continue
        candidates.sort(
            key=lambda candidate: _state_candidate_rank(
                candidate, pipeline.get("state_prompt_version")
            ),
            reverse=True,
        )
        removed += max(0, len(candidates) - 1)
        selected = dict(candidates[0])
        selected.pop("_explicit_reference", None)
        selected.pop("_candidate_key", None)
        result.append(selected)

    return result, {"state_assessments_removed": removed}


def _load_state_assessments():
    return _load_state_assessments_with_diagnostics()[0]


def _routing_type(state_code, inhibition_strategy_id, should_intervene):
    if inhibition_strategy_id:
        return "inhibited"
    try:
        mode = route_for_canonical_state(state_code)["route_mode"]
    except (KeyError, ValueError):
        mode = None
    if mode == REQUIRED_INTERVENTION or _as_bool(should_intervene) == 1:
        return "required"
    if mode == OPTIONAL_SUPPORT:
        return "optional"
    if mode == SUPPRESS and state_code in {"deep_thinking", "execution_progress", "constructive_conflict", "off_topic_self_regulated"}:
        return "inhibited"
    return "observation_only"


def _load_strategy_pipeline():
    assessment_ids_by_pipeline = {
        _as_int(row.get("pipeline_run_id")): row.get("assessment_id")
        for row in _load_state_assessments()
    }
    rows = _dict_rows(
        """
        SELECT spr.id AS pipeline_run_id, spr.session_id, spr.group_id,
               spr.trigger_source, spr.trigger_message_id,
               spr.input_start_sequence, spr.input_end_sequence,
               spr.coarse_should_escalate, spr.coarse_decision,
               spr.coarse_state_code, spr.canonical_sub_state_code,
               spr.secondary_sub_state_tags_json, spr.should_intervene,
               spr.strategy_candidate_ids_json, spr.selected_strategy_id,
               spr.inhibition_strategy_id, spr.strategy_selection_reason,
               spr.stage1_status, spr.stage1_started_at, spr.stage1_completed_at,
               spr.stage2_status, spr.stage2_started_at, spr.stage2_completed_at,
               spr.stage3_status, spr.stage3_started_at, spr.stage3_completed_at,
               spr.publish_status, spr.published_message_id, spr.published_at,
               spr.skip_reason, spr.failure_code, spr.final_status,
               spr.created_at, spr.updated_at,
               COALESCE(
                   (SELECT COALESCE(CAST(css.assessment_id AS TEXT), 'segment-' || css.id)
                      FROM collaboration_state_segments AS css
                     WHERE css.strategy_pipeline_run_id=spr.id
                        OR (spr.assessment_batch_id IS NOT NULL
                            AND css.assessment_batch_id=spr.assessment_batch_id)
                     ORDER BY css.segment_order, css.id LIMIT 1),
                   (SELECT CAST(ir.state_assessment_id AS TEXT)
                      FROM intervention_runs AS ir
                     WHERE ir.strategy_pipeline_run_id=spr.id
                       AND ir.state_assessment_id IS NOT NULL
                     ORDER BY ir.id LIMIT 1)
               ) AS state_assessment_id
          FROM strategy_pipeline_runs AS spr
         ORDER BY spr.session_id, spr.group_id, spr.created_at, spr.id
        """
    )
    result = []
    for row in rows:
        state_code = _export_state_code(row.get("canonical_sub_state_code"))
        stage1_need = row.get("coarse_should_escalate")
        if stage1_need is None:
            decision = str(row.get("coarse_decision") or "").lower()
            stage1_need = 1 if decision in {"escalate", "intervene", "required"} else 0
        candidates = [row.get("coarse_state_code")] if row.get("coarse_state_code") else []
        completed_at = row.get("published_at") or row.get("stage3_completed_at") or row.get("updated_at")
        result.append({
            "session_id": row.get("session_id"),
            "group_id": row.get("group_id"),
            "pipeline_run_id": row.get("pipeline_run_id"),
            "trigger_source": row.get("trigger_source"),
            "trigger_message_id": row.get("trigger_message_id"),
            "input_start_sequence": row.get("input_start_sequence"),
            "input_end_sequence": row.get("input_end_sequence"),
            "stage1_need_intervention": _as_bool(stage1_need),
            "stage1_candidate_states": json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
            "state_assessment_id": assessment_ids_by_pipeline.get(
                _as_int(row.get("pipeline_run_id"))
            ),
            "state_code": state_code,
            "state_overlays": _json_array(row.get("secondary_sub_state_tags_json")),
            "routing_type": _routing_type(
                state_code, row.get("inhibition_strategy_id"), row.get("should_intervene")
            ),
            "candidate_strategy_ids": _json_array(row.get("strategy_candidate_ids_json")),
            "selected_strategy_id": row.get("selected_strategy_id"),
            "inhibition_strategy_id": row.get("inhibition_strategy_id"),
            "strategy_selection_reason": row.get("strategy_selection_reason"),
            "generation_status": row.get("stage3_status"),
            "publish_status": row.get("publish_status") or row.get("final_status"),
            "published_message_id": row.get("published_message_id"),
            "skip_reason": row.get("skip_reason"),
            "failure_code": row.get("failure_code"),
            "stage1_latency_ms": _elapsed_ms(row.get("stage1_started_at"), row.get("stage1_completed_at")),
            "stage2_latency_ms": _elapsed_ms(row.get("stage2_started_at"), row.get("stage2_completed_at")),
            "stage3_latency_ms": _elapsed_ms(row.get("stage3_started_at"), row.get("stage3_completed_at")),
            "total_latency_ms": _elapsed_ms(row.get("created_at"), completed_at),
            "created_at": row.get("created_at"),
            "completed_at": completed_at,
        })
    return result


def _load_interventions():
    rows = _dict_rows(
        """
        SELECT il.id AS intervention_id,
               COALESCE(m.session_id, il.session_id, ir.session_id) AS session_id,
               COALESCE(m.group_id, il.group_id, ir.group_id) AS group_id,
               COALESCE(ir.strategy_pipeline_run_id, spr.id) AS pipeline_run_id,
               m.id AS message_id,
               COALESCE(il.trigger_source, ir.trigger_type, spr.trigger_source) AS trigger_source,
               spr.trigger_message_id,
               COALESCE(spr.canonical_sub_state_code,
                        ir.canonical_sub_state_code, ir.detected_state) AS state_code,
               COALESCE(spr.selected_strategy_id,
                        ir.selected_strategy_id, il.strategy_id) AS selected_strategy_id,
               m.content,
               COALESCE(ir.actual_published_at, ir.published_at,
                        spr.published_at, m.created_at, il.created_at) AS published_at
          FROM intervention_logs AS il
          LEFT JOIN intervention_runs AS ir
            ON ir.id=COALESCE(il.intervention_run_id, il.intervention_id)
          LEFT JOIN strategy_pipeline_runs AS spr
            ON spr.id=ir.strategy_pipeline_run_id
          LEFT JOIN messages AS m
            ON m.id=COALESCE(
                il.message_id,
                ir.message_id,
                (SELECT m2.id FROM messages AS m2
                  WHERE m2.linked_log_id=il.id
                  ORDER BY m2.id DESC LIMIT 1)
            )
         WHERE m.id IS NOT NULL
         ORDER BY session_id, group_id, published_at, intervention_id
        """
    )
    result = []
    for row in rows:
        result.append({
            "session_id": row.get("session_id"),
            "group_id": row.get("group_id"),
            "intervention_id": row.get("intervention_id"),
            "pipeline_run_id": row.get("pipeline_run_id"),
            "message_id": row.get("message_id"),
            "trigger_source": row.get("trigger_source"),
            "trigger_message_id": row.get("trigger_message_id"),
            "state_code": _canonical_sub_state(row.get("state_code")),
            "selected_strategy_id": row.get("selected_strategy_id"),
            "content": row.get("content"),
            "published_at": row.get("published_at"),
        })
    return result


def _load_participation():
    scope_rows = _dict_rows(
        """
        SELECT session_id, group_id FROM group_session_discussions
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM messages
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        """
    )
    scopes = {(_as_int(r["session_id"]), _as_int(r["group_id"])) for r in scope_rows}
    participant_rows = _dict_rows(
        """
        SELECT ep.group_id, ep.user_id,
               COALESCE(NULLIF(ep.participant_code, ''),
                        NULLIF(u.participant_code, '')) AS participant_code
          FROM experiment_participants AS ep
          LEFT JOIN users AS u ON u.id=ep.user_id
         WHERE ep.is_active=1
        """
    )
    participants = defaultdict(dict)
    for row in participant_rows:
        participants[_as_int(row.get("group_id"))][_as_int(row.get("user_id"))] = row.get(
            "participant_code"
        )
    message_rows = _dict_rows(
        """
        SELECT m.session_id, m.group_id, m.user_id, m.content, m.created_at,
               COALESCE(NULLIF(ep.participant_code, ''),
                        NULLIF(u.participant_code, '')) AS participant_code
          FROM messages AS m
          LEFT JOIN users AS u ON u.id=m.user_id
          LEFT JOIN experiment_participants AS ep
            ON ep.user_id=m.user_id AND ep.group_id=m.group_id
         WHERE COALESCE(NULLIF(m.role, ''), u.role, m.sender_type)='student'
           AND TRIM(COALESCE(m.content, ''))<>''
         ORDER BY m.created_at, m.id
        """
    )
    stats = {}
    for row in message_rows:
        sid = _as_int(row.get("session_id"))
        gid = _as_int(row.get("group_id"))
        uid = _as_int(row.get("user_id"))
        scopes.add((sid, gid))
        participants[gid].setdefault(uid, row.get("participant_code"))
        key = (sid, gid, uid)
        item = stats.setdefault(
            key,
            {"count": 0, "characters": 0, "minutes": set(), "first": None, "last": None},
        )
        content = str(row.get("content") or "").strip()
        created_at = row.get("created_at")
        item["count"] += 1
        item["characters"] += len(content)
        if created_at:
            item["minutes"].add(str(created_at)[:16])
            item["first"] = min(item["first"], created_at) if item["first"] else created_at
            item["last"] = max(item["last"], created_at) if item["last"] else created_at

    result = []
    for sid, gid in sorted(scopes, key=lambda value: (value[0] or 0, value[1] or 0)):
        for uid, participant_code in sorted(participants.get(gid, {}).items()):
            item = stats.get((sid, gid, uid), {})
            result.append({
                "session_id": sid,
                "group_id": gid,
                "participant_code": participant_code,
                "message_count": item.get("count", 0),
                "character_count": item.get("characters", 0),
                "active_minutes": len(item.get("minutes", set())),
                "first_message_at": item.get("first"),
                "last_message_at": item.get("last"),
            })
    return result


def _load_emotion_checkins():
    return _dict_rows(
        """
        SELECT e.session_id, e.group_id, e.id AS checkin_id,
               COALESCE(NULLIF(ep.participant_code, ''),
                        NULLIF(u.participant_code, '')) AS participant_code,
               e.checkin_type, e.emotion_option, e.positivity, e.engagement,
               e.atmosphere, e.expression_willingness, e.note, e.created_at
          FROM emotion_checkins AS e
          LEFT JOIN users AS u ON u.id=e.user_id
          LEFT JOIN experiment_participants AS ep
            ON ep.user_id=e.user_id AND ep.group_id=e.group_id
         ORDER BY e.session_id, e.group_id, e.created_at, e.id
        """
    )


def _load_help_requests():
    rows = _dict_rows(
        """
        SELECT hr.session_id, hr.group_id, hr.id AS help_request_id,
               COALESCE(NULLIF(ep.participant_code, ''),
                        NULLIF(u.participant_code, '')) AS participant_code,
               hr.request_text, hr.intent, hr.source_message_id, hr.status,
               hr.response_message_id,
               ir.strategy_pipeline_run_id AS pipeline_run_id,
               hr.failure_reason, hr.created_at, hr.completed_at
          FROM help_requests AS hr
          LEFT JOIN users AS u ON u.id=hr.requester_id
          LEFT JOIN experiment_participants AS ep
            ON ep.user_id=hr.requester_id AND ep.group_id=hr.group_id
          LEFT JOIN intervention_runs AS ir ON ir.id=hr.intervention_run_id
         ORDER BY hr.session_id, hr.group_id, hr.created_at, hr.id
        """
    )
    result = []
    for row in rows:
        row["failure_code"] = _failure_code(row.pop("failure_reason", None))
        result.append(row)
    return result


def _load_emotion_feedback():
    """Export Emotion E1/E2 records with export-only canonical adjacency."""
    from services.emotion_feedback_record_service import (
        list_emotion_feedback_records,
    )

    rows = list_emotion_feedback_records(
        limit=None,
        include_nearest_canonical=True,
    )
    result = []
    for row in rows:
        item = dict(row)
        item["evidence_message_ids"] = json.dumps(
            item.get("evidence_message_ids") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        item["current_metrics"] = json.dumps(
            item.get("current_metrics") or {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        item["previous_metrics"] = json.dumps(
            item.get("previous_metrics") or {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        item["fallback_used"] = _as_bool(item.get("fallback_used"))
        result.append(item)
    return result


class _HTMLToMarkdown(HTMLParser):
    """Small deterministic converter for saved collaborative-document HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.list_stack = []
        self.href_stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "blockquote"}:
            self.parts.append("\n\n" + ("> " if tag == "blockquote" else ""))
        elif tag == "br":
            self.parts.append("  \n")
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
            self.parts.append("\n")
        elif tag == "li":
            marker = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.parts.append("\n" + marker)
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            self.parts.append("[")
            self.href_stack.append(attrs.get("href") or "")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n")
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self.parts.append("\n")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            href = self.href_stack.pop() if self.href_stack else ""
            self.parts.append("](" + href + ")" if href else "]")

    def handle_data(self, data):
        self.parts.append(data)

    def markdown(self):
        text = "".join(self.parts).replace("\r\n", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_markdown(value):
    parser = _HTMLToMarkdown()
    parser.feed(str(value or ""))
    parser.close()
    return parser.markdown()


def _yaml_string(value):
    return json.dumps(str(value or ""), ensure_ascii=False)


def _load_deliverables():
    rows = _dict_rows(
        """
        WITH resolved_documents AS (
            SELECT cd.*,
                   COALESCE(
                       cd.session_id,
                       (
                           SELECT MIN(es2.id)
                             FROM experiment_sessions AS es2
                            WHERE es2.task_id=cd.task_id
                              AND es2.session_no=cd.session_no
                           HAVING COUNT(*)=1
                       )
                   ) AS resolved_session_id
              FROM collaborative_documents AS cd
             WHERE cd.status='submitted'
        )
        SELECT cd.id, cd.resolved_session_id AS session_id, cd.group_id, cd.task_id,
               cd.title, cd.content_text, cd.content_html, cd.submitted_at,
               COALESCE(NULLIF(es.title, ''), NULLIF(es.session_role, ''),
                        'Session-' || es.id) AS session_name,
               g.group_code, lt.title AS task_title
          FROM resolved_documents AS cd
          LEFT JOIN experiment_sessions AS es ON es.id=cd.resolved_session_id
          LEFT JOIN groups AS g ON g.id=cd.group_id
          LEFT JOIN learning_tasks AS lt ON lt.id=cd.task_id
         ORDER BY cd.resolved_session_id, cd.group_id, cd.submitted_at DESC, cd.id DESC
        """
    )
    result = []
    seen = set()
    for row in rows:
        key = (_as_int(row.get("session_id")), _as_int(row.get("group_id")))
        if key in seen:
            continue
        seen.add(key)
        body = str(row.get("content_text") or "")
        if not body.strip() and row.get("content_html"):
            body = _html_to_markdown(row.get("content_html"))
        if not body.strip():
            body = "> 本组没有可导出的最终文本内容。"
        front_matter = [
            "---",
            "session_id: %s" % _clean(row.get("session_id")),
            "session_name: %s" % _yaml_string(row.get("session_name")),
            "group_id: %s" % _clean(row.get("group_id")),
            "group_code: %s" % _yaml_string(row.get("group_code")),
            "task_id: %s" % _clean(row.get("task_id")),
            "task_title: %s" % _yaml_string(row.get("task_title")),
            "submitted_at: %s" % _yaml_string(row.get("submitted_at")),
            "---",
            "",
        ]
        row["_content"] = "\n".join(front_matter) + body.rstrip() + "\n"
        result.append(row)
    return result


def _option_display(options_json, option_key):
    if option_key in (None, ""):
        return ""
    options = _json_value(options_json, [])
    if isinstance(options, dict):
        value = options.get(str(option_key), option_key)
        if isinstance(value, dict):
            return value.get("label") or value.get("text") or str(option_key)
        return value
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict):
                key = option.get("key", option.get("value"))
                if str(key) == str(option_key):
                    return option.get("label") or option.get("text") or str(option_key)
    return str(option_key)


def _questionnaire_response_value(row):
    """Return the value that was actually persisted for a questionnaire item."""
    option_key = row.get("response_option_key")
    if option_key not in (None, ""):
        return option_key
    if row.get("response_value") is not None:
        return row.get("response_value")
    return row.get("response_text") if row.get("response_text") is not None else ""


def _questionnaire_export_filename(questionnaire, response_stage):
    """Use the questionnaire code once, without duplicating an embedded stage."""
    code = str(questionnaire.get("code") or "")
    stage = str(response_stage or "").strip().lower()
    has_embedded_stage = bool(
        stage and re.search(r"(?:^|[_-])%s(?:$|[_-])" % re.escape(stage), code, re.I)
    )
    return safe_questionnaire_filename(
        questionnaire,
        response_stage=None if has_embedded_stage else response_stage,
    )


def _load_questionnaires():
    rows = _dict_rows(
        """
        SELECT qs.id AS submission_id, qs.session_id, qs.group_id, qs.user_id,
               qs.questionnaire_id, qs.response_stage, qs.submitted_at,
               q.code AS questionnaire_code, q.title AS questionnaire_title,
               q.sort_order AS questionnaire_sort_order,
               COALESCE(NULLIF(ep.participant_code, ''),
                        NULLIF(u.participant_code, '')) AS participant_code,
               qi.id AS item_id, qi.item_code, qi.question_type,
               qi.dimension_label, qi.prompt_text, qi.options_json,
               qi.section_no AS item_section_no,
               qi.sort_order AS item_sort_order,
               qi.item_order AS item_order,
               qr.response_value, qr.response_option_key, qr.response_text
          FROM questionnaire_submissions AS qs
          JOIN questionnaires AS q ON q.id=qs.questionnaire_id
          JOIN questionnaire_items AS qi ON qi.questionnaire_id=qs.questionnaire_id
          LEFT JOIN questionnaire_responses AS qr
            ON qr.submission_id=qs.id AND qr.item_id=qi.id
          LEFT JOIN users AS u ON u.id=qs.user_id
          LEFT JOIN experiment_participants AS ep
            ON ep.user_id=qs.user_id AND ep.group_id=qs.group_id
         WHERE qs.status='submitted'
         ORDER BY qs.session_id, qs.group_id, qs.user_id,
                  qs.questionnaire_id, qs.response_stage,
                  qs.submitted_at DESC, qs.id DESC,
                  qi.sort_order, qi.item_order, qi.id
        """
    )
    # Only the newest submitted record is valid for a participant/questionnaire/stage.
    newest_submission = {}
    result = []
    for row in rows:
        unique_key = (
            row.get("session_id"),
            row.get("group_id"),
            row.get("user_id"),
            row.get("questionnaire_id"),
            row.get("response_stage"),
        )
        chosen = newest_submission.setdefault(unique_key, row.get("submission_id"))
        if row.get("submission_id") != chosen:
            row["_duplicate_submission"] = True
            result.append(row)
            continue
        question_type = str(row.get("question_type") or "").lower()
        option_key = row.get("response_option_key")
        response_value = row.get("response_value")
        response_text = row.get("response_text")
        if option_key not in (None, ""):
            raw_answer = option_key
            response_value = ""
            response_text = _option_display(row.get("options_json"), option_key)
        elif response_value is not None:
            raw_answer = response_value
            response_text = "" if question_type != "text" else response_text
        else:
            raw_answer = response_text if response_text is not None else ""
        questionnaire = {
            "id": row.get("questionnaire_id"),
            "code": row.get("questionnaire_code"),
            "title": row.get("questionnaire_title"),
        }
        response_value = _questionnaire_response_value(row)
        result.append({
            "session_id": row.get("session_id"),
            "group_id": row.get("group_id"),
            "participant_code": row.get("participant_code"),
            "questionnaire_id": row.get("questionnaire_id"),
            "questionnaire_code": row.get("questionnaire_code"),
            "questionnaire_title": row.get("questionnaire_title"),
            "questionnaire_sort_order": row.get("questionnaire_sort_order"),
            "response_stage": row.get("response_stage"),
            "submission_id": row.get("submission_id"),
            "item_id": row.get("item_id"),
            "item_code": row.get("item_code"),
            "question_type": row.get("question_type"),
            "dimension_label": row.get("dimension_label"),
            "prompt_text": row.get("prompt_text"),
            "response_value": response_value,
            "response_option_key": option_key,
            "response_text": response_text,
            "raw_answer": raw_answer,
            "submitted_at": row.get("submitted_at"),
            "_item_definition_order": (
                _as_int(row.get("item_section_no")) or 0,
                _as_int(row.get("item_sort_order")) or 0,
                _as_int(row.get("item_order")) or 0,
                _as_int(row.get("item_id")) or 0,
            ),
            "_questionnaire_filename": _questionnaire_export_filename(
                questionnaire, response_stage=row.get("response_stage")
            ),
        })
    return result


def _questionnaire_item_definition_order(row):
    if row.get("_item_definition_order"):
        return tuple(row["_item_definition_order"])
    return (
        _as_int(row.get("item_section_no")) or 0,
        _as_int(row.get("item_sort_order")) or 0,
        _as_int(row.get("item_order")) or 0,
        _as_int(row.get("item_id")) or 0,
    )


def _pivot_questionnaire_rows(rows):
    """Build one raw-answer row per participant and questionnaire file."""
    partitions = defaultdict(list)
    item_records_by_session = defaultdict(dict)
    item_warnings = set()

    for row in rows:
        partition_key = (
            row.get("session_id"),
            row.get("_questionnaire_filename"),
        )
        partitions[partition_key].append(row)

        questionnaire_code = "" if row.get("questionnaire_code") is None else str(
            row.get("questionnaire_code")
        )
        item_code = "" if row.get("item_code") is None else str(row.get("item_code"))
        metadata_key = (questionnaire_code, item_code)
        metadata_order = (
            _as_int(row.get("questionnaire_sort_order")) or 0,
            questionnaire_code,
            _questionnaire_item_definition_order(row),
        )
        metadata = {
            "questionnaire_code": questionnaire_code,
            "item_code": item_code,
            "dimension_label": row.get("dimension_label"),
            "prompt_text": row.get("prompt_text"),
            "_metadata_order": metadata_order,
        }
        item_records = item_records_by_session[row.get("session_id")]
        existing = item_records.get(metadata_key)
        if existing is None:
            item_records[metadata_key] = metadata
        elif (
            existing.get("dimension_label") != metadata.get("dimension_label")
            or existing.get("prompt_text") != metadata.get("prompt_text")
        ):
            item_warnings.add(
                "questionnaire_export_metadata_conflict: "
                "questionnaire_code=%s item_code=%s; first definition kept"
                % metadata_key
            )

    wide_rows = []
    warnings = sorted(item_warnings)
    stats = {
        "questionnaire_file_count": len(partitions),
        "questionnaire_file_count_written": 0,
        "participant_row_count": 0,
        "item_count": sum(
            len(records) for records in item_records_by_session.values()
        ),
        "duplicate_participant_count": 0,
        "duplicate_item_code_count": 0,
        "invalid_questionnaire_file_count": 0,
    }

    for (session_id, questionnaire_filename), partition_rows in sorted(
        partitions.items(), key=lambda value: (value[0][0] or 0, str(value[0][1]))
    ):
        item_definitions = {}
        questionnaire_ids = set()
        questionnaire_codes = set()
        duplicate_item_codes = set()
        for row in partition_rows:
            questionnaire_id = row.get("questionnaire_id")
            questionnaire_ids.add(questionnaire_id)
            questionnaire_codes.add(str(row.get("questionnaire_code") or ""))
            item_code = "" if row.get("item_code") is None else str(row.get("item_code"))
            if not item_code:
                duplicate_item_codes.add(item_code)
                continue
            definition_identity = (questionnaire_id, row.get("item_id"))
            existing = item_definitions.get(item_code)
            if existing is None:
                item_definitions[item_code] = {
                    "identity": definition_identity,
                    "row": row,
                }
            elif existing["identity"] != definition_identity:
                duplicate_item_codes.add(item_code)

        invalid_reasons = []
        if len(questionnaire_ids) > 1 or len(questionnaire_codes) > 1:
            invalid_reasons.append("multiple questionnaire definitions share one export path")
        if duplicate_item_codes:
            stats["duplicate_item_code_count"] += len(duplicate_item_codes)
            invalid_reasons.append(
                "duplicate item_code(s): %s"
                % ", ".join(sorted(duplicate_item_codes))
            )

        ordered_item_codes = [
            item_code
            for item_code, _definition in sorted(
                item_definitions.items(),
                key=lambda value: (
                    _questionnaire_item_definition_order(value[1]["row"]),
                    value[0],
                ),
            )
        ]

        participant_rows = {}
        duplicate_participants = set()
        for row in partition_rows:
            participant_code = str(row.get("participant_code") or "")
            item_code = "" if row.get("item_code") is None else str(row.get("item_code"))
            participant = participant_rows.get(participant_code)
            if participant is None:
                participant = {
                    "session_id": session_id,
                    "group_id": row.get("group_id"),
                    "participant_code": participant_code,
                    "answers": {},
                }
                participant_rows[participant_code] = participant
            elif participant["group_id"] != row.get("group_id"):
                duplicate_participants.add(participant_code)

            if item_code in participant["answers"]:
                duplicate_participants.add(participant_code)
                continue
            participant["answers"][item_code] = _questionnaire_response_value(row)

        if duplicate_participants:
            stats["duplicate_participant_count"] += len(duplicate_participants)
            invalid_reasons.append(
                "duplicate participant answer rows: %s"
                % ", ".join(sorted(duplicate_participants))
            )

        if invalid_reasons:
            stats["invalid_questionnaire_file_count"] += 1
            warnings.append(
                "questionnaire_export_skipped: session_id=%s filename=%s; %s"
                % (session_id, questionnaire_filename, "; ".join(invalid_reasons))
            )
            continue

        questionnaire_code = next(iter(questionnaire_codes), "")
        for participant in sorted(
            participant_rows.values(),
            key=lambda value: (
                _as_int(value.get("group_id")) or 0,
                str(value.get("participant_code") or ""),
            ),
        ):
            wide_row = {
                "session_id": participant["session_id"],
                "group_id": participant["group_id"],
                "participant_code": participant["participant_code"],
                "_questionnaire_code": questionnaire_code,
                "_questionnaire_filename": questionnaire_filename,
                "_questionnaire_item_codes": ordered_item_codes,
            }
            wide_row.update({
                item_code: participant["answers"].get(item_code, "")
                for item_code in ordered_item_codes
            })
            wide_rows.append(wide_row)
            stats["participant_row_count"] += 1
        stats["questionnaire_file_count_written"] += 1

    item_rows_by_session = {}
    for session_id, item_records in item_records_by_session.items():
        item_rows = sorted(
            item_records.values(),
            key=lambda row: (
                row.get("_metadata_order") or (0, "", (0, 0, 0, 0)),
                row.get("questionnaire_code") or "",
                row.get("item_code") or "",
            ),
        )
        for row in item_rows:
            row.pop("_metadata_order", None)
        item_rows_by_session[session_id] = item_rows
    return wide_rows, item_rows_by_session, sorted(set(warnings)), stats


ROW_LOADERS = {
    "messages": _load_messages,
    "state-assessments": _load_state_assessments,
    "strategy-pipeline": _load_strategy_pipeline,
    "interventions": _load_interventions,
    "participation": _load_participation,
    "emotion-checkins": _load_emotion_checkins,
    "emotion-feedback": _load_emotion_feedback,
    "help-requests": _load_help_requests,
    "deliverables": _load_deliverables,
    "questionnaires": _load_questionnaires,
}


def _csv_safe(value):
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "\t" + value
    return value


def _csv_bytes(columns, rows, protect_formulas=True):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            column: (
                _csv_safe(row.get(column))
                if protect_formulas
                else ("" if row.get(column) is None else row.get(column))
            )
            for column in columns
        })
    return output.getvalue().encode(CSV_ENCODING)


def _csv_data_row_count(data):
    text = data.decode(CSV_ENCODING)
    return sum(1 for _row in csv.DictReader(io.StringIO(text)))


def _load_export_scopes():
    """Return every concrete session/group represented by the group datasets."""
    rows = _dict_rows(
        """
        SELECT session_id, group_id FROM group_session_discussions
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM messages
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM strategy_pipeline_runs
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM collaboration_state_segments
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM emotion_checkins
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM emotion_reflection_slots
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        UNION
        SELECT session_id, group_id FROM help_requests
         WHERE session_id IS NOT NULL AND group_id IS NOT NULL
        """
    )
    return {
        (_as_int(row.get("session_id")), _as_int(row.get("group_id")))
        for row in rows
        if _as_int(row.get("session_id")) is not None
        and _as_int(row.get("group_id")) is not None
    }


def _agent_message_statistics(message_rows):
    result = {"strategy": 0, "emotion": 0, "help": 0, "unclassified": 0}
    for row in message_rows:
        if row.get("sender_role") != "agent":
            continue
        agent_type = str(row.get("agent_type") or "").strip().lower()
        if agent_type in {"strategy", "emotion", "help"}:
            result[agent_type] += 1
        else:
            result["unclassified"] += 1
    return result


def _state_export_fallback(message_rows):
    counts = defaultdict(int)
    for row in message_rows:
        if row.get("state_assignment_source") == "export_fallback":
            counts[(_as_int(row.get("session_id")), _as_int(row.get("group_id")))] += 1
    return {
        "fallback_state": "standard",
        "total_rows": sum(counts.values()),
        "by_session_group": [
            {"session_id": scope[0], "group_id": scope[1], "rows": count}
            for scope, count in sorted(
                counts.items(), key=lambda item: (item[0][0] or 0, item[0][1] or 0)
            )
        ],
    }


def _duplicate_count(values):
    seen = set()
    duplicates = 0
    for value in values:
        if value in (None, ""):
            continue
        if value in seen:
            duplicates += 1
        else:
            seen.add(value)
    return duplicates


def _validation_results(valid_rows, message_diagnostics, inventory_mismatches):
    messages = valid_rows.get("messages", [])
    states = valid_rows.get("state-assessments", [])
    interventions = valid_rows.get("interventions", [])
    help_requests = valid_rows.get("help-requests", [])
    messages_in_package = "messages" in valid_rows
    message_ids = {
        _as_int(row.get("message_id"))
        for row in messages
        if _as_int(row.get("message_id")) is not None
    }
    return {
        "duplicate_message_ids": _duplicate_count(
            _as_int(row.get("message_id")) for row in messages
        ),
        "duplicate_pipeline_assessments": _duplicate_count(
            _as_int(row.get("pipeline_run_id")) for row in states
        ),
        "student_messages_without_state": sum(
            1
            for row in messages
            if row.get("sender_role") == "student" and not row.get("state_code")
        ),
        "agent_messages_without_type": sum(
            1
            for row in messages
            if row.get("sender_role") == "agent" and not row.get("agent_type")
        ),
        "agent_messages_without_reference_code": sum(
            1
            for row in messages
            if row.get("sender_role") == "agent"
            and row.get("agent_type") in {"strategy", "emotion", "help"}
            and not row.get("agent_reference_code")
        ),
        "orphan_intervention_messages": sum(
            1
            for row in interventions
            if _as_int(row.get("message_id")) not in message_ids
        ) if messages_in_package else 0,
        "orphan_help_response_messages": sum(
            1
            for row in help_requests
            if row.get("response_message_id") not in (None, "")
            and _as_int(row.get("response_message_id")) not in message_ids
        ) if messages_in_package else 0,
        "unmatched_published_emotion_messages": int(
            message_diagnostics.get("unmatched_event_count", 0)
        ),
        "inventory_row_count_mismatches": int(inventory_mismatches),
    }


def _load_metadata(scopes):
    session_ids = sorted({sid for sid, _gid in scopes if sid is not None})
    group_ids = sorted({gid for _sid, gid in scopes if gid is not None})
    sessions = {}
    groups = {}
    participants = defaultdict(list)
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        rows = _dict_rows(
            """
            SELECT es.id AS session_id,
                   COALESCE(NULLIF(es.title, ''), NULLIF(es.session_role, ''),
                            'Session-' || es.id) AS session_name,
                   es.session_no, es.task_id, lt.title AS task_title
              FROM experiment_sessions AS es
              LEFT JOIN learning_tasks AS lt ON lt.id=es.task_id
             WHERE es.id IN (%s)
            """ % placeholders,
            tuple(session_ids),
        )
        sessions = {row["session_id"]: row for row in rows}
    if group_ids:
        placeholders = ",".join("?" for _ in group_ids)
        group_rows = _dict_rows(
            "SELECT id AS group_id, group_code FROM groups WHERE id IN (%s)" % placeholders,
            tuple(group_ids),
        )
        groups = {row["group_id"]: row for row in group_rows}
        participant_rows = _dict_rows(
            """
            SELECT group_id, participant_code
              FROM experiment_participants
             WHERE is_active=1 AND group_id IN (%s)
             ORDER BY group_id, group_no, member_no, participant_code
            """ % placeholders,
            tuple(group_ids),
        )
        for row in participant_rows:
            code = row.get("participant_code")
            if code and code not in participants[row["group_id"]]:
                participants[row["group_id"]].append(code)
    return sessions, groups, participants


def _session_info(session_id, sessions):
    return sessions.get(session_id) or {
        "session_id": session_id,
        "session_name": "Session-%s" % session_id,
        "session_no": "",
        "task_id": "",
        "task_title": "",
    }


def _group_info(group_id, groups):
    return groups.get(group_id) or {"group_id": group_id, "group_code": "G%s" % group_id}


def _archive_base(session_id, group_id, sessions, groups):
    session = _session_info(session_id, sessions)
    group = _group_info(group_id, groups)
    return "sessions/%s/%s" % (safe_session_dir(session), safe_group_dir(group))


def _questionnaire_archive_base(session_id, sessions):
    return "sessions/%s" % safe_session_dir(_session_info(session_id, sessions))


def _questionnaire_row_sort_key(row):
    return (
        _as_int(row.get("group_id")) or 0,
        str(row.get("participant_code") or ""),
        row.get("_item_definition_order") or (0, 0, 0, 0),
        _as_int(row.get("submission_id")) or 0,
    )


def _path_structure(included_keys):
    if included_keys == ["questionnaires"]:
        return QUESTIONNAIRE_PATH_STRUCTURE
    if "questionnaires" in included_keys:
        return "%s; %s" % (PATH_STRUCTURE, QUESTIONNAIRE_PATH_STRUCTURE)
    return PATH_STRUCTURE


def _manifest_sessions(scopes, sessions, groups, participants):
    by_session = defaultdict(set)
    for session_id, group_id in scopes:
        by_session[session_id].add(group_id)
    result = []
    for session_id in sorted(by_session, key=lambda value: value or 0):
        session = _session_info(session_id, sessions)
        group_items = []
        for group_id in sorted(by_session[session_id], key=lambda value: value or 0):
            group = _group_info(group_id, groups)
            group_items.append({
                "group_id": group_id,
                "group_code": group.get("group_code"),
                "participants": [
                    {"participant_code": code} for code in participants.get(group_id, [])
                ],
            })
        result.append({
            "session_id": session_id,
            "session_name": session.get("session_name"),
            "session_no": session.get("session_no"),
            "task_id": session.get("task_id"),
            "task_title": session.get("task_title"),
            "groups": group_items,
        })
    return result


def _generated_at():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _readme(export_scope, included_keys, warnings):
    questionnaire_only = included_keys == ["questionnaires"]
    questionnaire_included = "questionnaires" in included_keys
    lines = [
        "# SSRL-ESP 研究数据导出",
        "",
        "本 ZIP 为系统当前保存数据的全量、非盲化导出，不应用课次、小组、任务或时间筛选。",
        "",
        "## 目录结构",
        "",
        (
            "`sessions/{session_name}_session-{session_id}/questionnaire_items.csv` "
            "and `sessions/{session_name}_session-{session_id}/questionnaires/file`"
            if questionnaire_only
            else "`sessions/{session_name}_session-{session_id}/{group_code}/file`"
        ),
        "",
    ]
    if questionnaire_included:
        lines.extend([
            "`questionnaire_items.csv` is stored directly under each session directory before `questionnaires/`.",
            "问卷 CSV 按课次聚合，统一放在课次目录下的 `questionnaires/` 中，不再创建小组或成员目录。",
            "",
        ])
    if not questionnaire_only:
        lines.extend([
            "完整导出会为每个课次—小组固定生成 8 个小组级 CSV。只有表头的 CSV 表示该类数据为 0 条；文件缺失表示导出异常。",
            "",
        ])
    lines.extend(["## 文件说明", ""])
    for key in included_keys:
        lines.append("- `%s`：%s。" % (EXPORT_SCHEMAS.get(key, ("deliverable.md",))[0] or "问卷 CSV", EXPORT_DESCRIPTIONS[key]))
    lines.extend([
        "",
        "## 数据约定",
        "",
        "- CSV 编码：UTF-8 with BOM（`utf-8-sig`）。",
        "- 时间：数据库原始时间文本；包生成时间为带时区 ISO 8601。",
        "- 缺失值：空字符串。",
        "- JSON 数组：UTF-8 JSON 数组文本。",
        "- 问卷只含逐题原始回答，不含总分、均值、维度分、反向计分或变化值。",
        "- `state_assessments.csv` 只包含正式策略 pipeline 使用的第二阶段 canonical 状态判断；每个 pipeline 最多一行，不包含消息级标签、情绪反馈状态或导出回填状态。",
        "- `messages.csv.agent_reference_code`：策略消息为实际 strategy ID，情绪消息为 `emotion_feedback_state`，主动求助响应为固定值 `HELP_REQUEST_RESPONSE`。",
        "- `HELP_REQUEST_RESPONSE` 只表示该消息是主动求助的响应，不是策略 ID。",
        "- `state_assignment_source=detected` 表示系统已有检测状态；`state_assignment_source=export_fallback` 表示导出时将缺失的学生消息状态回填为 `standard`。",
        "- 导出回填只影响 CSV，不修改数据库，也不会在 `state_assessments.csv` 中伪造第二阶段判断。",
        "- `session_id`、`group_id`、消息正文和 `selected_strategy_id` 等重复字段为兼容分析和保持内容完整而有意保留。",
        "- 三类 Agent 的详细过程保留在 `interventions.csv`、`emotion_feedback.csv`、`help_requests.csv`，其实际发布消息同时保留在 `messages.csv`。",
        "- 最终成果为 Markdown；YAML Front Matter 保存必要的课次、小组、任务和提交时间。",
        "- 除问卷原始回答 CSV 外，CSV 公式注入保护会将以 `=`, `+`, `-`, `@` 开头的文本前置制表符；问卷 CSV 保留数据库原始回答并仅进行标准 CSV 转义。",
        "",
        "## 关键关联",
        "",
        "- `messages.message_id` → `interventions.message_id` / `help_requests.response_message_id`",
        "- `state_assessments.assessment_id` → `strategy_pipeline.state_assessment_id`",
        "- `strategy_pipeline.pipeline_run_id` → `interventions.pipeline_run_id` / `help_requests.pipeline_run_id`",
    ])
    if warnings:
        lines.extend(["", "## 警告", ""])
        lines.extend("- " + warning for warning in warnings)
    return "\n".join(lines).rstrip() + "\n"


def _package_filename(export_scope):
    prefix = {
        "all": "ssrl_esp_export",
        "state-assessments": "state_assessments",
        "strategy-pipeline": "strategy_pipeline",
        "emotion-checkins": "emotion_checkins",
        "emotion-feedback": "emotion_feedback",
        "help-requests": "help_requests",
    }.get(export_scope, export_scope)
    return build_export_filename(prefix)


def build_research_export(export_scope):
    """Build one full-scope research ZIP and return data, filename and manifest."""
    if export_scope == "all":
        included_keys = list(RESEARCH_EXPORT_KEYS)
    elif export_scope in RESEARCH_EXPORT_KEYS:
        included_keys = [export_scope]
    else:
        raise KeyError("unknown_research_export:%s" % export_scope)

    message_diagnostics = {
        "published_event_count": 0,
        "matched_event_count": 0,
        "unmatched_event_count": 0,
        "content_mismatches": 0,
        "warnings": [],
    }
    deduplication_statistics = {"state_assessments_removed": 0}
    loaded = {}
    for key in included_keys:
        if key == "messages":
            loaded[key], message_diagnostics = _load_messages_with_diagnostics()
        elif key == "state-assessments":
            loaded[key], deduplication_statistics = (
                _load_state_assessments_with_diagnostics()
            )
        else:
            loaded[key] = ROW_LOADERS[key]()
    excluded_rows = {key: 0 for key in included_keys}
    valid_rows = {}
    questionnaire_items_rows = []
    questionnaire_validation = {}
    scopes = _load_export_scopes() if export_scope == "all" else set()
    warnings = list(message_diagnostics.get("warnings", []))

    for key, rows in loaded.items():
        clean_rows = []
        duplicate_submission_ids = set()
        seen_message_sequences = set()
        for row in rows:
            if row.get("_duplicate_submission"):
                excluded_rows[key] += 1
                duplicate_submission_ids.add(row.get("submission_id"))
                continue
            session_id = _as_int(row.get("session_id"))
            group_id = _as_int(row.get("group_id"))
            participant_missing = key in {
                "questionnaires", "participation", "emotion-checkins", "help-requests"
            } and not row.get("participant_code")
            if key == "messages" and row.get("sender_role") == "student":
                participant_missing = not row.get("participant_code")
            duplicate_message_sequence = False
            if key == "messages" and session_id is not None and group_id is not None:
                sequence_key = (session_id, group_id, _as_int(row.get("sequence")))
                if sequence_key[2] is not None:
                    duplicate_message_sequence = sequence_key in seen_message_sequences
                    seen_message_sequences.add(sequence_key)
            if (
                session_id is None
                or group_id is None
                or participant_missing
                or duplicate_message_sequence
            ):
                excluded_rows[key] += 1
                logger.warning(
                    "Research export excluded %s row without complete scope: %r",
                    key,
                    {"session_id": row.get("session_id"), "group_id": row.get("group_id")},
                )
                continue
            row["session_id"] = session_id
            row["group_id"] = group_id
            scopes.add((session_id, group_id))
            clean_rows.append(row)
        if key == "strategy-pipeline":
            for row in clean_rows:
                publish_status = str(row.get("publish_status") or "").lower()
                if publish_status == "published" and not row.get("published_message_id"):
                    warnings.append(
                        "策略流水 %s 标记为已发布但缺少 published_message_id。"
                        % row.get("pipeline_run_id")
                    )
                if publish_status != "published" and not (
                    row.get("skip_reason") or row.get("failure_code")
                ):
                    warnings.append(
                        "策略流水 %s 未发布且缺少 skip_reason/failure_code。"
                        % row.get("pipeline_run_id")
                    )
        if duplicate_submission_ids:
            warnings.append(
                "%s 个重复问卷 submission 的题目行已排除，仅保留最新有效提交。"
                % len(duplicate_submission_ids)
            )
        valid_rows[key] = clean_rows
        if excluded_rows[key]:
            warnings.append(
                "%s 有 %s 行因无法解析课次、小组或参与者范围而未写入正式数据文件。"
                % (key, excluded_rows[key])
            )

    if "questionnaires" in valid_rows:
        (
            valid_rows["questionnaires"],
            questionnaire_items_rows,
            questionnaire_warnings,
            questionnaire_validation,
        ) = _pivot_questionnaire_rows(valid_rows["questionnaires"])
        warnings.extend(questionnaire_warnings)

    message_rows = valid_rows.get("messages", [])
    agent_message_statistics = _agent_message_statistics(message_rows)
    state_export_fallback = _state_export_fallback(message_rows)
    if agent_message_statistics["unclassified"]:
        warnings.append(
            "%s exported Agent messages could not be classified as strategy, emotion, or help."
            % agent_message_statistics["unclassified"]
        )
    if message_diagnostics.get("unmatched_event_count"):
        warnings.append(
            "%s published emotion feedback records could not be matched to an exported message."
            % message_diagnostics["unmatched_event_count"]
        )

    sessions, groups, participants = _load_metadata(scopes)
    included_files = []
    dataset_files = defaultdict(dict)
    inventory_mismatches = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        if "questionnaires" in included_keys:
            for session_id, item_rows in sorted(
                questionnaire_items_rows.items(),
                key=lambda value: value[0] or 0,
            ):
                item_path = (
                    _questionnaire_archive_base(session_id, sessions)
                    + "/questionnaire_items.csv"
                )
                archive.writestr(
                    item_path,
                    _csv_bytes(
                        QUESTIONNAIRE_ITEMS_EXPORT_COLUMNS,
                        item_rows,
                        protect_formulas=False,
                    ),
                )
                included_files.append(item_path)

        for key in included_keys:
            rows = valid_rows[key]
            if key == "deliverables":
                for row in rows:
                    base = _archive_base(row["session_id"], row["group_id"], sessions, groups)
                    path = base + "/deliverable.md"
                    archive.writestr(path, row["_content"].encode("utf-8"))
                    included_files.append(path)
                continue

            filename, columns = EXPORT_SCHEMAS[key]
            partitions = defaultdict(list)
            for row in rows:
                if key == "questionnaires":
                    partition_key = (
                        row["session_id"],
                        row["_questionnaire_filename"],
                    )
                else:
                    partition_key = (row["session_id"], row["group_id"])
                partitions[partition_key].append(row)
            if export_scope == "all" and key in GROUP_LEVEL_EXPORT_KEYS:
                partition_keys = sorted(
                    scopes, key=lambda value: (value[0] or 0, value[1] or 0)
                )
            elif key == "questionnaires":
                partition_keys = sorted(
                    partitions, key=lambda value: (value[0] or 0, str(value[1]))
                )
            else:
                partition_keys = sorted(
                    partitions, key=lambda value: tuple(str(v) for v in value)
                )
            for partition_key in partition_keys:
                if key == "questionnaires":
                    session_id, questionnaire_filename = partition_key
                    path = "%s/questionnaires/%s" % (
                        _questionnaire_archive_base(session_id, sessions),
                        questionnaire_filename,
                    )
                    rows_for_csv = sorted(
                        partitions[partition_key], key=_questionnaire_row_sort_key
                    )
                    columns_for_csv = list(QUESTIONNAIRE_EXPORT_COLUMNS)
                    columns_for_csv.extend(
                        rows_for_csv[0].get("_questionnaire_item_codes", [])
                    )
                else:
                    session_id, group_id = partition_key
                    base = _archive_base(session_id, group_id, sessions, groups)
                    path = base + "/" + filename
                    rows_for_csv = partitions[partition_key]
                    columns_for_csv = columns
                csv_data = _csv_bytes(
                    columns_for_csv,
                    rows_for_csv,
                    protect_formulas=key != "questionnaires",
                )
                archive.writestr(path, csv_data)
                included_files.append(path)
                if key in GROUP_LEVEL_EXPORT_KEYS:
                    actual_rows = _csv_data_row_count(csv_data)
                    expected_rows = len(partitions[partition_key])
                    if actual_rows != expected_rows:
                        inventory_mismatches += 1
                    dataset_files[(session_id, group_id)][filename] = {
                        "generated": True,
                        "rows": actual_rows,
                    }

        if not included_files:
            warnings.append("当前没有符合该导出类型的正式研究数据；ZIP 仅包含说明和 manifest。")

        dataset_inventory = []
        for session_id, group_id in sorted(
            dataset_files, key=lambda value: (value[0] or 0, value[1] or 0)
        ):
            dataset_inventory.append({
                "session_id": session_id,
                "group_id": group_id,
                "files": dataset_files[(session_id, group_id)],
            })

        validation_results = _validation_results(
            valid_rows, message_diagnostics, inventory_mismatches
        )
        for name, count in validation_results.items():
            if count:
                warnings.append("Export validation %s=%s." % (name, count))

        generated_at = _generated_at()
        manifest = {
            "package_format_version": PACKAGE_FORMAT_VERSION,
            "generated_at": generated_at,
            "export_scope": export_scope,
            "path_structure": _path_structure(included_keys),
            "csv_encoding": CSV_ENCODING,
            "export_mode": EXPORT_MODE,
            "sessions": _manifest_sessions(scopes, sessions, groups, participants),
            "included_files": included_files,
            "excluded_rows": excluded_rows,
            "warnings": warnings,
            "csv_formula_escape": (
                "questionnaire CSVs preserve raw values; other CSVs prefix leading =, +, -, @ characters with a tab"
            ),
            "schema_versions": SCHEMA_VERSIONS,
            "dataset_inventory": dataset_inventory,
            "state_export_fallback": state_export_fallback,
            "agent_message_statistics": agent_message_statistics,
            "deduplication_statistics": deduplication_statistics,
            "questionnaire_validation": questionnaire_validation,
            "validation_results": validation_results,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr(
            "README.md",
            _readme(export_scope, included_keys, warnings).encode("utf-8"),
        )

    return {
        "zip_data": buf.getvalue(),
        "filename": _package_filename(export_scope),
        "manifest": manifest,
    }
