# -*- coding: utf-8 -*-
"""MonitoringService：编排新版监测管线全部步骤。"""
import logging
import json
import uuid
from config import SSRL_AGENT_DEBUG
from datetime import datetime

from config import (
    DISCUSSION_PIPELINE_V2_ENABLED,
    DISCUSSION_PIPELINE_V2_SHADOW,
    PIPELINE_V2_ANALYZER_VERSION,
    PIPELINE_V2_SILENCE_DELAY_SECONDS,
    PIPELINE_V2_MIN_INTERVENTION_CONFIDENCE,
    AUTO_INTERVENTION_V2_ENABLED,
    ONLINE_SILENCE_NO_MSG_SECONDS,
    LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED,
)
from db import now_str, query_all, query_one

from db import get_active_session_id

from services.audit_log_service import safe_write_audit_log
from services.discussion_pipeline_v2.monitor_run_repo import MonitorRunRepo
from services.discussion_pipeline_v2.context_service import ContextService
from services.discussion_pipeline_v2.feature_service import FeatureService
from services.discussion_pipeline_v2.rule_detector import RuleDetector
from services.discussion_pipeline_v2.trigger_policy import TriggerPolicy
from services.discussion_pipeline_v2.decision_fusion import DecisionFusion

from services.state_assessment_service import persist_state_assessment
from services.help_request_coverage_service import HelpRequestCoverageService
from services.three_stage_stage1 import Stage1PipelineService
from services.agent_mode_service import pipeline_mode_from_session
from services.three_stage_latency import (
    elapsed_ms as latency_elapsed_ms,
    latency_timer,
    latency_timestamp,
    record_latency_event,
)

logger = logging.getLogger(__name__)


FINAL_STATE_CODES_FOR_AUDIT = (
    "positive_collaboration",
    "negative_silence",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
)

RISK_REVIEW_STATES = {
    "negative_silence",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
}
STUDENT_MESSAGE_TRIGGER_TYPES = {
    "new_message",
    "student_message",
    "student_help",
    "student_help_request",
}
STUDENT_HELP_TRIGGER_TYPES = {"student_help", "student_help_request"}
STATE_DETECTOR_MAX_NEW_MESSAGES = 8
STATE_DETECTOR_LOOKBACK_MESSAGES = 3


def _monitor_log(event: str, **fields):
    safe_fields = {
        key: value
        for key, value in fields.items()
        if value is not None
    }
    logger.info(
        "[monitoring_audit] %s %s",
        event,
        json.dumps(safe_fields, ensure_ascii=False, default=str, separators=(",", ":")),
    )


def _row_dict(row):
    return dict(row) if row else None


def _safe_int(value, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _message_sequence(row):
    return _safe_int((row or {}).get("sequence"))


def _context_messages(context: dict) -> list:
    rows = []
    for row in (context or {}).get("window_messages") or []:
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _student_context_messages(context: dict) -> list:
    rows = []
    for row in (context or {}).get("window_student_messages") or []:
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _sequence_bounds(rows: list[dict], cutoff_sequence: int):
    sequences = [
        _message_sequence(row)
        for row in rows
        if _message_sequence(row) is not None
    ]
    if not sequences:
        return None, cutoff_sequence
    return min(sequences), max(sequences)


def _context_message_ids(rows: list[dict]) -> list[int]:
    ids = []
    for row in rows:
        value = _safe_int(row.get("id"))
        if value is not None:
            ids.append(value)
    return ids


def _context_message_sequences(rows: list[dict]) -> list[int]:
    sequences = []
    for row in rows:
        value = _message_sequence(row)
        if value is not None:
            sequences.append(value)
    return sequences


def _rule_scores(rule_assessment: dict) -> dict:
    scores = {state: 0.0 for state in FINAL_STATE_CODES_FOR_AUDIT}
    for candidate in (rule_assessment or {}).get("candidates") or []:
        state_code = candidate.get("state_code")
        if state_code in scores:
            try:
                scores[state_code] = round(float(candidate.get("score") or 0.0), 3)
            except (TypeError, ValueError):
                scores[state_code] = 0.0
    return scores


def _best_non_unknown_score(rule_scores: dict) -> float:
    return max([float(value or 0.0) for value in (rule_scores or {}).values()] or [0.0])


def _rule_evidence_message_ids(context: dict, rule_assessment: dict) -> list[int]:
    assessment = rule_assessment or {}
    winning_state = assessment.get("winning_state_code")
    message_ids = []
    direct_ids = assessment.get("evidence_message_ids") or []
    if isinstance(direct_ids, list):
        message_ids.extend(direct_ids)
    evidence_by_state = assessment.get("evidence") or {}
    if isinstance(evidence_by_state, dict):
        state_ids = evidence_by_state.get(winning_state) or []
        if isinstance(state_ids, list):
            message_ids.extend(state_ids)

    sequences = assessment.get("evidence_sequences") or assessment.get(
        "trigger_sequences"
    ) or []
    sequence_set = {_safe_int(item) for item in sequences}
    sequence_set.discard(None)
    ids = _unique_sorted_ints(message_ids)
    for row in _student_context_messages(context):
        if _message_sequence(row) in sequence_set:
            row_id = _safe_int(row.get("id"))
            if row_id is not None:
                ids.append(row_id)
    return _unique_sorted_ints(ids)


def _latest_message_scope(group_id: int, cutoff_sequence: int) -> dict:
    row = _row_dict(
        query_one(
            """
            SELECT id, sequence, session_id, session_no, task_id, discussion_id,
                   scope_resolved_from, legacy_scope_fallback,
                   scope_fallback_reason
            FROM messages
            WHERE group_id=? AND sequence IS NOT NULL AND sequence<=?
            ORDER BY sequence DESC, id DESC
            LIMIT 1
            """,
            (group_id, cutoff_sequence),
        )
    )
    if row:
        return {
            "message_id": row.get("id"),
            "sequence": row.get("sequence"),
            "session_id": row.get("session_id"),
            "session_no": row.get("session_no"),
            "task_id": row.get("task_id"),
            "discussion_id": row.get("discussion_id"),
            "source": row.get("scope_resolved_from") or "message",
            "resolved_from": row.get("scope_resolved_from") or "message",
            "is_legacy_fallback": bool(row.get("legacy_scope_fallback"))
            or row.get("session_id") is None
            or row.get("discussion_id") is None,
            "fallback_reason": row.get("scope_fallback_reason")
            or (
                "legacy_message_missing_canonical_scope"
                if row.get("session_id") is None or row.get("discussion_id") is None
                else None
            ),
        }
    try:
        from db import get_current_running_session_context
        current = get_current_running_session_context()
    except Exception:
        current = None
    current = dict(current or {})
    return {
        "message_id": None,
        "sequence": None,
        "session_id": current.get("session_id"),
        "session_no": current.get("session_no"),
        "task_id": current.get("task_id"),
        "discussion_id": None,
        "source": "runtime",
        "resolved_from": "runtime",
        "is_legacy_fallback": False,
        "fallback_reason": "discussion_runtime_not_found",
    }


def _previous_completed_cutoff(group_id: int, cutoff_sequence: int):
    row = query_one(
        """
        SELECT MAX(cutoff_sequence) AS cutoff
        FROM monitor_runs
        WHERE group_id=? AND analyzer_version=? AND status='completed'
          AND cutoff_sequence IS NOT NULL AND cutoff_sequence<?
        """,
        (group_id, PIPELINE_V2_ANALYZER_VERSION, cutoff_sequence),
    )
    return _safe_int(row["cutoff"]) if row else None


def _count_new_student_messages(
    group_id: int,
    *,
    previous_cutoff: int = None,
    cutoff_sequence: int,
    session_id=None,
    session_no=None,
    task_id=None,
    discussion_id=None,
) -> int:
    clauses = [
        "m.group_id=?",
        "COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'",
        "m.sequence IS NOT NULL",
        "m.sequence<=?",
    ]
    params = [group_id, cutoff_sequence]
    if previous_cutoff is not None:
        clauses.append("m.sequence>?")
        params.append(previous_cutoff)
    if session_id is not None:
        clauses.append("m.session_id=?")
        params.append(session_id)
    elif session_no is not None:
        clauses.append("m.session_no=?")
        params.append(session_no)
    if task_id is not None:
        clauses.append("m.task_id=?")
        params.append(task_id)
    if discussion_id is not None:
        clauses.append("m.discussion_id=?")
        params.append(discussion_id)
    row = query_one(
        f"""
        SELECT COUNT(*) AS c
        FROM messages m
        JOIN users u ON u.id=m.user_id
        WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    )
    return int(row["c"]) if row else 0


def _find_existing_cutoff_run(group_id: int, cutoff_sequence: int, trigger_type: str):
    if trigger_type not in STUDENT_MESSAGE_TRIGGER_TYPES:
        return MonitorRunRepo.find_by_unique_key(group_id, cutoff_sequence, trigger_type=trigger_type)
    placeholders = ",".join(["?"] * len(STUDENT_MESSAGE_TRIGGER_TYPES))
    return query_one(
        f"""
        SELECT *
        FROM monitor_runs
        WHERE group_id=? AND cutoff_sequence=? AND analyzer_version=?
          AND COALESCE(trigger_type, 'new_message') IN ({placeholders})
        ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'completed' THEN 1 WHEN 'skipped' THEN 2 ELSE 3 END, id DESC
        LIMIT 1
        """,
        (
            group_id,
            cutoff_sequence,
            PIPELINE_V2_ANALYZER_VERSION,
            *sorted(STUDENT_MESSAGE_TRIGGER_TYPES),
        ),
    )


def _detector_scope_clause(scope: dict, prefix: str = "m"):
    clauses = []
    params = []
    session_id = (scope or {}).get("session_id")
    session_no = (scope or {}).get("session_no")
    task_id = (scope or {}).get("task_id")
    discussion_id = (scope or {}).get("discussion_id")
    if session_id is not None:
        clauses.append(f"{prefix}.session_id=?")
        params.append(session_id)
    elif session_no is not None:
        clauses.append(f"{prefix}.session_no=?")
        params.append(session_no)
    if task_id is not None:
        clauses.append(f"{prefix}.task_id=?")
        params.append(task_id)
    if discussion_id is not None:
        clauses.append(f"{prefix}.discussion_id=?")
        params.append(discussion_id)
    return clauses, params


def _load_detector_student_rows(
    group_id: int,
    *,
    cutoff_sequence: int,
    previous_cutoff: int = None,
    scope: dict = None,
) -> dict:
    base_clauses = [
        "m.group_id=?",
        "COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'",
        "m.sequence IS NOT NULL",
        "m.sequence<=?",
    ]
    params = [group_id, cutoff_sequence]
    scope_clauses, scope_params = _detector_scope_clause(scope or {}, "m")
    base_clauses.extend(scope_clauses)
    params.extend(scope_params)

    new_clauses = list(base_clauses)
    new_params = list(params)
    if previous_cutoff is not None:
        new_clauses.append("m.sequence>?")
        new_params.append(previous_cutoff)

    def _select(where_clauses, where_params, order_sql, limit):
        rows = query_all(
            f"""
            SELECT m.*, u.real_name, u.username, u.participant_code,
                   COALESCE(NULLIF(TRIM(m.role), ''), u.role) AS resolved_role
            FROM messages m
            JOIN users u ON u.id=m.user_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            tuple(where_params + [limit]),
        )
        return [dict(row) for row in rows or []]

    new_rows = _select(
        new_clauses,
        new_params,
        "m.sequence DESC, m.id DESC",
        STATE_DETECTOR_MAX_NEW_MESSAGES,
    )
    new_rows = list(reversed(new_rows))
    anchor_sequence = (
        min([_safe_int(row.get("sequence")) for row in new_rows if _safe_int(row.get("sequence")) is not None], default=None)
        or cutoff_sequence + 1
    )
    lookback_clauses = list(base_clauses) + ["m.sequence<?"]
    lookback_params = list(params) + [anchor_sequence]
    lookback_rows = _select(
        lookback_clauses,
        lookback_params,
        "m.sequence DESC, m.id DESC",
        STATE_DETECTOR_LOOKBACK_MESSAGES,
    )
    lookback_rows = list(reversed(lookback_rows))

    new_ids = {_safe_int(row.get("id")) for row in new_rows}
    new_ids.discard(None)
    combined = []
    seen_ids = set()
    for row in lookback_rows + new_rows:
        row_id = _safe_int(row.get("id"))
        if row_id is None or row_id in seen_ids:
            continue
        row["role"] = row.pop("resolved_role", None) or row.get("role") or "student"
        row["is_new_since_last_assessment"] = row_id in new_ids
        combined.append(row)
        seen_ids.add(row_id)
    return {
        "rows": combined,
        "new_rows": new_rows,
        "allowed_evidence_message_ids": [
            _safe_int(row.get("id"))
            for row in combined
            if _safe_int(row.get("id")) is not None
        ],
        "new_student_message_ids": sorted(new_ids),
    }


def _build_state_detector_context(
    context: dict,
    *,
    group_id: int,
    cutoff_sequence: int,
    previous_cutoff: int = None,
    scope: dict = None,
) -> dict:
    detector_window = _load_detector_student_rows(
        group_id,
        cutoff_sequence=cutoff_sequence,
        previous_cutoff=previous_cutoff,
        scope=scope,
    )
    detector_context = dict(context or {})
    detector_context["state_detector_messages"] = detector_window["rows"]
    detector_context["recent_student_messages"] = detector_window["rows"]
    detector_context["state_detector_allowed_evidence_message_ids"] = detector_window["allowed_evidence_message_ids"]
    detector_context["state_detector_new_student_message_ids"] = detector_window["new_student_message_ids"]
    detector_context["state_detector_candidate_sequences"] = [
        _safe_int(row.get("sequence"))
        for row in detector_window["new_rows"]
        if _safe_int(row.get("sequence")) is not None
    ]
    candidate_sequence_set = set(detector_context["state_detector_candidate_sequences"])
    detector_context["state_detector_context_sequences"] = [
        _safe_int(row.get("sequence"))
        for row in detector_window["rows"]
        if _safe_int(row.get("sequence")) is not None
        and _safe_int(row.get("sequence")) not in candidate_sequence_set
    ]
    detector_context["state_detector_cutoff_sequence"] = cutoff_sequence
    detector_context["state_detector_previous_cutoff_sequence"] = previous_cutoff
    detector_context["trigger_type"] = (
        (scope or {}).get("trigger_type")
        or detector_context.get("trigger_type")
    )
    if detector_context.get("trigger_type") == "post_intervention_observation":
        try:
            from services.three_stage_observation import enrich_state_detector_context

            detector_context = enrich_state_detector_context(
                detector_context,
                group_id=group_id,
                session_id=(scope or {}).get("session_id")
                or detector_context.get("session_id"),
                discussion_id=(scope or {}).get("discussion_id")
                or detector_context.get("discussion_id"),
                cutoff_sequence=cutoff_sequence,
            )
        except Exception as exc:
            logger.warning(
                "failed to enrich observation state detector context group=%s cutoff=%s: %s",
                group_id,
                cutoff_sequence,
                exc,
            )
    return detector_context


def _help_request_blocks_strategy(
    group_id: int,
    *,
    trigger_type: str,
    cutoff_sequence: int,
    window_start_sequence: int = None,
    scope: dict = None,
    target_state_code: str = None,
    target_segment_id: int = None,
    target_end_sequence: int = None,
    current_time=None,
) -> dict:
    if trigger_type in STUDENT_HELP_TRIGGER_TYPES:
        guard = HelpRequestCoverageService.bypassed("student_help_request_trigger")
        guard.update({"blocked": True, "guard_blocked": True})
        return {**guard, "reason": guard["reason_code"]}
    scope = scope or {}
    start_sequence = (
        int(window_start_sequence)
        if window_start_sequence is not None
        else int(cutoff_sequence)
    )
    guard = HelpRequestCoverageService.evaluate(
        group_id,
        scope.get("session_id"),
        target_state_code,
        target_segment_id,
        start_sequence,
        (
            int(target_end_sequence)
            if target_end_sequence is not None
            else int(cutoff_sequence)
        ),
        current_time,
    )
    return {**guard, "reason": guard.get("reason_code")}


def _llm_audit_error_fields(llm_meta: dict = None) -> dict:
    if not isinstance(llm_meta, dict) or llm_meta.get("analysis_skipped") or llm_meta.get("success") is True:
        return {}
    error_type = (
        llm_meta.get("schema_error")
        or llm_meta.get("failure_reason")
        or llm_meta.get("failure_type")
        or "llm_error"
    )
    fields = {"state_llm_error_type": error_type}
    if error_type == "parse_error":
        fields["parse_error"] = llm_meta.get("failure_message") or error_type
    elif error_type == "invalid_state":
        fields["invalid_state"] = llm_meta.get("failure_message") or error_type
    elif error_type == "invalid_evidence_ids":
        fields["invalid_evidence_ids"] = llm_meta.get("failure_message") or error_type
    else:
        fields["llm_error"] = llm_meta.get("failure_message") or error_type
    return fields


def _canonical_skip_reason(raw_reason: str = None, *, final_state: str = None) -> str:
    text = str(raw_reason or "").strip()
    if text in {"cutoff_already_detected", "same_cutoff_already_detected"}:
        return "duplicate_cutoff"
    if text in {"room_closed", "session_not_running", "no_active_experiment_session"}:
        return "session_not_active"
    if text in {"room_blocked", "ROOM_AI_INTERVENING"} or text.startswith("state_AI_INTERVENING"):
        return "document_locked"
    if "pending_recent_help_requests" in text or "pending_help" in text:
        return "pending_help_request"
    if "cooldown_active" in text:
        return "cooldown_active"
    if text.startswith("rule_candidate_confidence") and "below" in text:
        return "all_rule_scores_below_threshold"
    if "task_agent_intervention_disabled" in text or "strategy_agent_disabled" in text:
        return "auto_intervention_disabled"
    if "no_active_session" in text:
        return "session_not_active"
    if "task" in text and ("missing" in text or "not_available" in text):
        return "task_not_available"
    if final_state == "unknown":
        return "final_state_unknown"
    if final_state == "positive_collaboration":
        return "positive_state_no_intervention"
    return text or None


def _is_student_context_row(row: dict) -> bool:
    role = str((row or {}).get("role") or "").strip().lower()
    sender_type = str((row or {}).get("sender_type") or "").strip().lower()
    return role == "student" or (not role and sender_type == "student")


def _message_matches_detection_scope(
    row: dict,
    *,
    session_id=None,
    session_no=None,
    task_id=None,
    discussion_id=None,
) -> bool:
    if not row:
        return False
    if session_id is not None:
        actual_session_id = row.get("session_id")
        if actual_session_id is None or str(actual_session_id) != str(session_id):
            return False
    elif session_no is not None:
        actual_session_no = row.get("session_no")
        if actual_session_no is None or str(actual_session_no) != str(session_no):
            return False
    if task_id is not None:
        actual_task_id = row.get("task_id")
        if actual_task_id is None or str(actual_task_id) != str(task_id):
            return False
    if discussion_id is not None:
        actual_discussion_id = row.get("discussion_id")
        if (
            actual_discussion_id is None
            or str(actual_discussion_id) != str(discussion_id)
        ):
            return False
    return True


def _unique_sorted_ints(values) -> list[int]:
    result = set()
    for value in values or []:
        parsed = _safe_int(value)
        if parsed is not None:
            result.add(parsed)
    return sorted(result)


def _state_evidence_message_ids(context: dict, rule_assessment: dict, llm_result: dict, final_state: str) -> list[int]:
    ids = []
    llm_state = None
    if isinstance(llm_result, dict):
        llm_state = llm_result.get("primary_state") or llm_result.get("state_code")
    if llm_state == final_state:
        ids.extend(llm_result.get("evidence_message_ids") or [])
    if (rule_assessment or {}).get("winning_state_code") == final_state:
        ids.extend(_rule_evidence_message_ids(context, rule_assessment))
    return _unique_sorted_ints(ids)


def _state_evidence_sequence_bounds(
    *,
    group_id: int,
    context: dict,
    student_context_rows: list[dict],
    rule_assessment: dict,
    llm_result: dict,
    final_state: str,
    session_id=None,
    session_no=None,
    task_id=None,
    discussion_id=None,
    fallback_end=None,
) -> tuple:
    evidence_ids = _state_evidence_message_ids(
        context, rule_assessment, llm_result, final_state
    )
    sequence_map = _message_id_to_sequence_map(
        group_id=group_id,
        message_ids=evidence_ids,
        context_rows=student_context_rows,
        session_id=session_id,
        session_no=session_no,
        task_id=task_id,
        discussion_id=discussion_id,
    )
    sequences = _unique_sorted_ints(sequence_map.values())
    if sequences:
        return min(sequences), max(sequences)
    if final_state == "negative_silence" and fallback_end is not None:
        return fallback_end, fallback_end
    return None, None


def _message_id_to_sequence_map(
    *,
    group_id: int,
    message_ids: list[int],
    context_rows: list[dict],
    session_id=None,
    session_no=None,
    task_id=None,
    discussion_id=None,
) -> dict[int, int]:
    id_map = {}
    requested_ids = set(_unique_sorted_ints(message_ids))
    for row in context_rows or []:
        row_id = _safe_int(row.get("id"))
        seq = _message_sequence(row)
        if row_id is None or seq is None:
            continue
        if row_id not in requested_ids:
            continue
        if not _is_student_context_row(row):
            continue
        if not _message_matches_detection_scope(
            row,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            discussion_id=discussion_id,
        ):
            continue
        id_map[row_id] = seq
    missing = [mid for mid in requested_ids if mid not in id_map]
    if missing:
        placeholders = ",".join("?" for _ in missing)
        rows = query_all(
            f"""
            SELECT id, sequence, role, sender_type, session_id, session_no,
                   task_id, discussion_id
            FROM messages
            WHERE group_id=? AND id IN ({placeholders})
            """,
            (group_id, *missing),
        )
        for row in rows or []:
            data = dict(row)
            row_id = _safe_int(data.get("id"))
            seq = _message_sequence(data)
            if row_id is None or seq is None:
                continue
            if not _is_student_context_row(data):
                continue
            if not _message_matches_detection_scope(
                data,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
            ):
                continue
            id_map[row_id] = seq
    return id_map


def _monitor_message_segment_payload(
    *,
    group_id: int,
    context: dict,
    student_context_rows: list[dict],
    rule_assessment: dict,
    llm_result: dict,
    final_state: str,
    final_confidence,
    previous_cutoff,
    cutoff_sequence: int,
    session_id=None,
    session_no=None,
    task_id=None,
    discussion_id=None,
) -> dict:
    candidate_rows = []
    for row in student_context_rows or []:
        sequence = _message_sequence(row)
        if sequence is None or sequence > cutoff_sequence:
            continue
        if previous_cutoff is not None and sequence <= previous_cutoff:
            continue
        if not _is_student_context_row(row):
            continue
        if not _message_matches_detection_scope(
            row,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            discussion_id=discussion_id,
        ):
            continue
        candidate_rows.append(row)
    new_sequences = _unique_sorted_ints(_message_sequence(row) for row in candidate_rows)

    evidence_ids = _state_evidence_message_ids(context, rule_assessment, llm_result, final_state)
    id_to_sequence = _message_id_to_sequence_map(
        group_id=group_id,
        message_ids=evidence_ids,
        context_rows=student_context_rows,
        session_id=session_id,
        session_no=session_no,
        task_id=task_id,
        discussion_id=discussion_id,
    )
    evidence_sequences = _unique_sorted_ints(id_to_sequence.get(mid) for mid in evidence_ids)

    new_evidence_sequences = [
        sequence for sequence in evidence_sequences if sequence in set(new_sequences)
    ]
    if new_sequences:
        if not new_evidence_sequences:
            return {
                "skipped": True,
                "reason": "no_new_supporting_evidence",
                "evidence_sequences": evidence_sequences,
                "new_evidence_sequences": [],
                "covered_message_range": None,
                "trigger_sequence": cutoff_sequence,
            }
        start = min(new_evidence_sequences)
        end = max(new_sequences)
    elif evidence_sequences:
        start = min(evidence_sequences)
        end = max(evidence_sequences)
    else:
        return {"skipped": True, "reason": "no_student_evidence"}

    evidence_sequences = [seq for seq in evidence_sequences if start <= seq <= end]
    if not evidence_sequences:
        return {
            "skipped": True,
            "reason": "no_supporting_evidence_in_covered_range",
            "evidence_sequences": [],
            "new_evidence_sequences": [],
            "covered_message_range": [start, end],
            "trigger_sequence": cutoff_sequence,
        }
    return {
        "skipped": False,
        "state_code": final_state,
        "start_message_id": start,
        "end_message_id": end,
        "evidence_message_ids": _unique_sorted_ints(evidence_sequences),
        "evidence_sequences": _unique_sorted_ints(evidence_sequences),
        "new_evidence_sequences": _unique_sorted_ints(new_evidence_sequences),
        "covered_message_range": [start, end],
        "trigger_sequence": cutoff_sequence,
        "continuation_reason": (
            "explicit_evidence_with_following_student_messages"
            if end > max(evidence_sequences)
            else "explicit_evidence_range"
        ),
        "confidence": final_confidence if final_confidence is not None else 0.0,
    }


def _persist_state_monitor_segment(
    *,
    group_id: int,
    run_id: int,
    persisted_state: dict,
    final_state: str,
    final_confidence,
    trigger_type: str,
    silence_expected_sequence,
    silence_expected_message_at,
    silence_expected_session_id,
    silence_expected_task_id,
    context: dict,
    student_context_rows: list[dict],
    rule_assessment: dict,
    llm_result: dict,
    previous_cutoff,
    cutoff_sequence: int,
    window_start_sequence,
    window_end_sequence,
    audit: dict,
) -> dict:
    assessment_id = (persisted_state or {}).get("assessment_id")
    if final_state == "unknown":
        return {"skipped": True, "reason": "final_state_unknown"}

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    if final_state == "negative_silence":
        if trigger_type != "silence_check":
            return {"skipped": True, "reason": "negative_silence_requires_silence_check"}
        return CollaborationStateSegmentService.record_negative_silence_if_applicable(
            group_id=group_id,
            expected_sequence=silence_expected_sequence,
            expected_last_student_message_at=silence_expected_message_at,
            expected_session_id=silence_expected_session_id,
            expected_task_id=silence_expected_task_id,
            source_run_id=run_id,
            assessment_id=assessment_id,
        )

    payload = _monitor_message_segment_payload(
        group_id=group_id,
        context=context,
        student_context_rows=student_context_rows,
        rule_assessment=rule_assessment,
        llm_result=llm_result,
        final_state=final_state,
        final_confidence=final_confidence,
        previous_cutoff=previous_cutoff,
        cutoff_sequence=cutoff_sequence,
        session_id=audit.get("session_id"),
        session_no=audit.get("session_no"),
        task_id=audit.get("task_id"),
        discussion_id=audit.get("discussion_id"),
    )
    if payload.get("skipped"):
        return payload
    persisted = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=group_id,
        session_id=audit.get("session_id"),
        session_no=audit.get("session_no"),
        task_id=audit.get("task_id"),
        discussion_id=audit.get("discussion_id"),
        state_code=payload["state_code"],
        start_message_id=payload["start_message_id"],
        end_message_id=payload["end_message_id"],
        evidence_message_ids=payload["evidence_message_ids"],
        confidence=payload["confidence"],
        source_run_id=run_id,
        assessment_id=assessment_id,
        trigger_sequence=payload["trigger_sequence"],
        analysis_window_start_message_id=window_start_sequence,
        analysis_window_end_message_id=window_end_sequence,
    )
    persisted.update(
        {
            "evidence_sequences": payload["evidence_sequences"],
            "new_evidence_sequences": payload["new_evidence_sequences"],
            "covered_message_range": payload["covered_message_range"],
            "trigger_sequence": payload["trigger_sequence"],
            "detection_window": [
                window_start_sequence,
                window_end_sequence,
            ],
            "continuation_reason": payload["continuation_reason"],
        }
    )
    return persisted


def _derive_detection_skip_reason(
    *,
    final_state: str,
    new_student_message_count: int,
    rule_scores: dict,
    should_call: bool,
    call_reason: str,
    auto_enabled: bool,
    group_auto_enabled: bool,
    shadow: bool,
    state_persisted: bool,
) -> str:
    if not state_persisted:
        return "state_persistence_failed"
    if final_state == "unknown":
        if new_student_message_count <= 0:
            return "no_student_messages"
        if _best_non_unknown_score(rule_scores) < 0.55:
            return "all_rule_scores_below_threshold"
        return "final_state_unknown"
    if final_state == "positive_collaboration":
        return "positive_state_no_intervention"
    if not group_auto_enabled:
        return "group_auto_intervention_disabled"
    if not auto_enabled or shadow:
        return "auto_intervention_disabled"
    if final_state in RISK_REVIEW_STATES and not should_call:
        return _canonical_skip_reason(call_reason, final_state=final_state) or call_reason
    return None


def _build_state_handling_decision(
    *,
    final_state: str,
    assessment_status: str,
    decision_source: str,
    confidence,
    trigger_type: str,
    persist_state_segment,
    schedule_strategy,
    should_call: bool,
    call_reason: str,
    llm_result: dict,
    evidence_sequences: list[int],
    new_student_sequences: list[int] = None,
    autonomous_regulation_observed: bool = False,
) -> dict:
    """Keep state persistence and intervention scheduling as separate decisions."""
    confidence_value = float(confidence or 0.0)
    confirmed_state = (
        final_state in FINAL_STATE_CODES_FOR_AUDIT
        and (
            assessment_status == "confirmed"
            or (
                decision_source == "rule_high_confidence_fallback"
                and confidence_value >= 0.55
            )
        )
    )
    evidence_sequences = _unique_sorted_ints(evidence_sequences)
    new_student_sequences = _unique_sorted_ints(new_student_sequences)
    new_sequence_set = set(new_student_sequences)
    new_evidence_sequences = [
        sequence for sequence in evidence_sequences if sequence in new_sequence_set
    ]
    requires_message_evidence = final_state != "negative_silence"
    incremental_message_trigger = (
        trigger_type in STUDENT_MESSAGE_TRIGGER_TYPES
        and bool(new_student_sequences)
    )

    persist_enabled = bool(persist_state_segment)
    if not persist_enabled:
        should_persist = False
        persist_reason = "assessment_batch_transaction_owns_segments"
    elif final_state == "unknown":
        should_persist = False
        persist_reason = "final_state_unknown"
    elif not confirmed_state:
        should_persist = False
        persist_reason = "state_not_confirmed"
    elif requires_message_evidence and not evidence_sequences:
        should_persist = False
        persist_reason = "no_supporting_evidence"
    elif incremental_message_trigger and not new_evidence_sequences:
        should_persist = False
        persist_reason = "no_new_supporting_evidence"
    else:
        should_persist = True
        if (
            decision_source == "rule_high_confidence_fallback"
            and assessment_status != "confirmed"
        ):
            persist_reason = "confirmed_rule_fallback"
        elif final_state == "positive_collaboration":
            persist_reason = "confirmed_positive_state"
        else:
            persist_reason = "confirmed_state"

    strategy_enabled = bool(schedule_strategy)
    if schedule_strategy == "legacy_only":
        strategy_enabled = not (
            isinstance(llm_result, dict) and "segments" in llm_result
        )
    should_schedule = bool(strategy_enabled and should_call)
    if not strategy_enabled:
        schedule_reason = "strategy_scheduling_deferred"
    elif autonomous_regulation_observed and final_state in RISK_REVIEW_STATES:
        should_schedule = False
        schedule_reason = "autonomous_regulation_observed"
    elif requires_message_evidence and not evidence_sequences:
        should_schedule = False
        schedule_reason = "no_supporting_evidence"
    elif incremental_message_trigger and not new_evidence_sequences:
        should_schedule = False
        schedule_reason = "no_new_supporting_evidence"
    elif final_state == "positive_collaboration":
        schedule_reason = "positive_state_no_intervention"
    elif final_state == "unknown":
        schedule_reason = "unknown_state_no_intervention"
    elif should_schedule:
        schedule_reason = call_reason or "strategy_review_candidate"
    else:
        schedule_reason = call_reason or "strategy_not_required"

    return {
        "should_persist": should_persist,
        "should_schedule_strategy": should_schedule,
        "persist_reason": persist_reason,
        "schedule_reason": schedule_reason,
        "state_confidence": confidence_value,
        "evidence_sequences": evidence_sequences,
        "new_evidence_sequences": new_evidence_sequences,
        "covered_message_range": (
            [min(new_evidence_sequences), max(new_student_sequences)]
            if new_evidence_sequences and new_student_sequences
            else None
        ),
        "trigger_type": trigger_type,
        "assessment_status": assessment_status,
        "segment_persistence_enabled": persist_enabled,
        "strategy_scheduling_enabled": strategy_enabled,
    }


def _collect_context_for_scope(group_id: int, scope: dict) -> dict:
    try:
        return ContextService.collect(
            group_id,
            task_id=scope.get("task_id"),
            session_no=scope.get("session_no"),
            session_id=scope.get("session_id"),
            discussion_id=scope.get("discussion_id"),
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc) and "positional" not in str(exc):
            raise
        return ContextService.collect(group_id)


def _filter_context_to_cutoff(context: dict, cutoff_sequence: int) -> dict:
    """Remove messages newer than an immutable assessment batch window."""
    filtered = dict(context or {})
    message_keys = (
        "window_messages",
        "window_student_messages",
        "recent_student_messages",
        "low_window_student_messages",
    )
    for key in message_keys:
        rows = []
        for raw in (filtered.get(key) or []):
            row = dict(raw)
            sequence = _message_sequence(row)
            if sequence is not None and sequence <= int(cutoff_sequence):
                rows.append(row)
        filtered[key] = rows
    student_rows = filtered.get("window_student_messages") or filtered.get("recent_student_messages") or []
    filtered["student_message_count_session"] = len(
        {
            _message_sequence(row)
            for row in student_rows
            if _message_sequence(row) is not None
        }
    )
    if student_rows:
        filtered["last_student_message_time"] = student_rows[-1].get("created_at")
    return filtered


def _debug_log(msg, *args):
    if SSRL_AGENT_DEBUG:
        import logging
        logging.getLogger(__name__).info("[SSRL_AGENT] " + msg, *args)


def _silence_strategy_precheck(
    *,
    group_id: int,
    session_id,
    session_no,
    task_id,
    cutoff_sequence: int,
    segment_id,
    final_state: str,
) -> dict:
    """Run deterministic strategy gates before enqueueing a silence event."""
    group = query_one(
        """
        SELECT state, COALESCE(auto_intervention_enabled, 1) AS enabled
        FROM groups
        WHERE id=?
        """,
        (group_id,),
    )
    if not group:
        return {"allowed": False, "reason": "room_not_found"}
    if not bool(group["enabled"]):
        return {"allowed": False, "reason": "group_auto_intervention_disabled"}

    from services.intervention_pipeline_v2.agent_research_helper import (
        check_strategy_agent_enabled,
    )

    if not check_strategy_agent_enabled(group_id):
        return {"allowed": False, "reason": "strategy_agent_disabled"}

    from services.session_lifecycle import check_agent_allowed

    allowed, gate_reason = check_agent_allowed(
        group_id,
        session_id=session_id,
        task_id=task_id,
        session_no=session_no,
        agent_type="strategy",
    )
    if not allowed:
        return {
            "allowed": False,
            "reason": gate_reason or "session_not_active",
        }

    from db import get_agent_intervention_enabled_for_task

    if task_id and not get_agent_intervention_enabled_for_task(
        task_id,
        group_id=group_id,
    ):
        return {
            "allowed": False,
            "reason": "task_agent_intervention_disabled",
        }

    from services.intervention_pipeline_v2.intervention_validator import (
        InterventionValidator,
    )

    validation = InterventionValidator.validate(
        group_id,
        cutoff_sequence,
        {
            "final_state": final_state,
            "detected_state": final_state,
            "trigger_type": "silence_rule",
            "trigger_source": "silence_rule",
            "session_id": session_id,
            "cutoff_sequence": cutoff_sequence,
            "target_segment_id": segment_id,
            "target_start_sequence": cutoff_sequence,
            "target_end_sequence": cutoff_sequence,
            "evidence_sequences_json": json.dumps(
                [cutoff_sequence],
                ensure_ascii=False,
            ),
        },
    )
    if not validation.get("valid"):
        return {
            "allowed": False,
            "reason": validation.get("reason") or "silence_strategy_precheck_failed",
            "validation": validation,
        }
    return {
        "allowed": True,
        "reason": "silence_strategy_precheck_passed",
        "validation": validation,
    }


class MonitoringService:
    """新版监测管线编排器 V2。

    职责：
    1. 检查 Feature Flag；
    2. 检查房间存在且未 CLOSED；
    3. 读取 cutoff_sequence；
    4. 创建或取得 monitor_run（幂等）；
    5. 执行规则检测；
    6. 判断是否需要 LLM；
    7. 必要时调用 state_detector；
    8. 融合结果；
    9. 写入 monitor_run；
    10. 不锁房、不发送助手消息。
    """

    @staticmethod
    def is_enabled() -> bool:
        return bool(DISCUSSION_PIPELINE_V2_ENABLED)

    @staticmethod
    def is_shadow() -> bool:
        return bool(DISCUSSION_PIPELINE_V2_SHADOW)

    @staticmethod
    def run_detection(
        group_id: int,
        trigger_type: str = "new_message",
        silence_expected_sequence: int = None,
        silence_expected_message_at: str = None,
        silence_expected_session_id: int = None,
        silence_expected_task_id: int = None,
        *,
        assessment_batch_id: int = None,
        fixed_candidate_start_sequence: int = None,
        fixed_candidate_end_sequence: int = None,
        allow_state_llm: bool = True,
        persist_state_segment: bool = True,
        schedule_strategy: bool = True,
        pipeline_mode: str = None,
    ) -> dict:
        """执行一次完整的监测流程。返回检测结果摘要。"""
        if not DISCUSSION_PIPELINE_V2_ENABLED:
            return {
                "skipped": True,
                "reason": "auto_intervention_disabled",
                "raw_reason": "pipeline_v2_disabled",
            }

        is_shadow = bool(DISCUSSION_PIPELINE_V2_SHADOW)
        result = {
            "group_id": group_id,
            "shadow": is_shadow,
            "trigger_type": trigger_type,
            "monitor_run_id": None,
            "steps": {},
        }
        run_id = None
        audit = {
            "group_id": group_id,
            "session_id": None,
            "task_id": None,
            "discussion_id": None,
            "trigger_type": trigger_type,
            "window_start_sequence": None,
            "window_end_sequence": None,
            "cutoff_sequence": None,
            "new_student_message_count": 0,
            "context_message_ids": [],
            "context_message_sequences": [],
            "rule_scores": {state: 0.0 for state in FINAL_STATE_CODES_FOR_AUDIT},
            "rule_candidate": None,
            "rule_confidence": 0.0,
            "rule_evidence_message_ids": [],
            "state_llm_called": False,
            "final_state": None,
            "final_confidence": None,
            "state_assessment_written": False,
            "state_assessment_id": None,
            "group_state_written": False,
            "group_state_id": None,
            "segment_write_attempted": False,
            "segment_written": False,
            "segment_id": None,
            "strategy_review_enqueued": False,
            "help_request_guard_evaluated": False,
            "help_request_guard": HelpRequestCoverageService.bypassed(
                "guard_not_reached"
            ),
            "skip_reason": None,
            "analyzer_version": PIPELINE_V2_ANALYZER_VERSION,
            "assessment_batch_id": assessment_batch_id,
            "candidate_start_sequence": fixed_candidate_start_sequence,
            "candidate_end_sequence": fixed_candidate_end_sequence,
            "pipeline_mode": pipeline_mode,
        }

        # 1. 检查房间存在且未 CLOSED
        group = query_one(
            """
            SELECT id, state, last_message_sequence, cutoff_sequence,
                   COALESCE(auto_intervention_enabled, 1) AS auto_intervention_enabled
            FROM groups WHERE id=?
            """,
            (group_id,),
        )
        if not group:
            result["skipped"] = True
            result["reason"] = "room_not_found"
            return result
        if group["state"] == "CLOSED":
            result["skipped"] = True
            result["reason"] = "session_not_active"
            result["raw_reason"] = "room_closed"
            return result

        if (
            trigger_type == "silence_check"
            and silence_expected_sequence is not None
        ):
            cutoff_sequence = int(silence_expected_sequence)
        else:
            candidate_cutoff = (
                int(fixed_candidate_end_sequence)
                if fixed_candidate_end_sequence is not None
                else group["cutoff_sequence"]
                if group["cutoff_sequence"]
                else (group["last_message_sequence"] or 0)
            )
            if fixed_candidate_end_sequence is not None:
                cutoff_sequence = candidate_cutoff
            else:
                latest_student = query_one(
                    """
                    SELECT sequence
                    FROM messages
                    WHERE group_id=?
                      AND sequence IS NOT NULL
                      AND sequence<=?
                      AND COALESCE(role, '')='student'
                    ORDER BY sequence DESC, id DESC
                    LIMIT 1
                    """,
                    (group_id, candidate_cutoff),
                )
                cutoff_sequence = (
                    int(latest_student["sequence"])
                    if latest_student
                    else candidate_cutoff
                )
        result["cutoff_sequence"] = cutoff_sequence
        audit["cutoff_sequence"] = cutoff_sequence
        audit["idempotency_key"] = f"{group_id}:{cutoff_sequence}:{PIPELINE_V2_ANALYZER_VERSION}:{trigger_type}"
        scope = _latest_message_scope(group_id, cutoff_sequence)
        audit["session_id"] = scope.get("session_id")
        audit["session_no"] = scope.get("session_no")
        audit["task_id"] = scope.get("task_id")
        audit["discussion_id"] = scope.get("discussion_id")
        audit["scope_source"] = scope.get("source")
        audit["scope_message_id"] = scope.get("message_id")
        if pipeline_mode is None and audit.get("session_id") is not None:
            session = query_one(
                "SELECT * FROM experiment_sessions WHERE id=?",
                (int(audit["session_id"]),),
            )
            pipeline_mode = pipeline_mode_from_session(dict(session)) if session else None
        elif pipeline_mode not in {"strategy", "state_only"}:
            raise ValueError("invalid_pipeline_mode")
        if pipeline_mode == "state_only":
            schedule_strategy = False
        result["pipeline_mode"] = pipeline_mode
        audit["pipeline_mode"] = pipeline_mode
        _debug_log("run_detection START group=%s trigger=%s cutoff_seq=%s", group_id, trigger_type, cutoff_sequence)
        _monitor_log(
            "task_start",
            group_id=group_id,
            session_id=audit.get("session_id"),
            cutoff_sequence=cutoff_sequence,
            trigger_type=trigger_type,
            monitor_run_id=None,
            final_state=None,
            skip_reason=None,
        )

        # 2. 幂等检测：同一 cutoff 是否已检测
        existing = None if assessment_batch_id is not None else _find_existing_cutoff_run(group_id, cutoff_sequence, trigger_type)
        compatibility_segment_missing = False
        if (
            existing
            and existing["status"] == "completed"
            and allow_state_llm
            and persist_state_segment
            and existing["final_state"] not in (None, "unknown")
        ):
            compatibility_segment_missing = not bool(
                query_one(
                    """
                    SELECT id FROM collaboration_state_segments
                    WHERE group_id=?
                      AND (
                            source='state_monitor'
                            OR (source='llm' AND assessment_status='confirmed')
                          )
                      AND (? IS NULL OR session_id=?)
                    LIMIT 1
                    """,
                    (group_id, audit.get("session_id"), audit.get("session_id")),
                )
            )
        if existing and existing["status"] in ("completed", "skipped") and not compatibility_segment_missing:
            if (
                trigger_type == "silence_check"
                and persist_state_segment
                and existing["status"] == "completed"
                and existing["final_state"] == "negative_silence"
            ):
                from services.collaboration_state_segment_service import (
                    CollaborationStateSegmentService,
                )

                result["state_segment_result"] = (
                    CollaborationStateSegmentService.record_negative_silence_if_applicable(
                        group_id=group_id,
                        expected_sequence=silence_expected_sequence,
                        expected_last_student_message_at=silence_expected_message_at,
                        expected_session_id=silence_expected_session_id,
                        expected_task_id=silence_expected_task_id,
                        source_run_id=existing["id"],
                    )
                )
            result["skipped"] = True
            result["reason"] = "duplicate_cutoff"
            result["raw_reason"] = "cutoff_already_detected"
            result["existing_run_id"] = existing["id"]
            _monitor_log(
                "task_skipped",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=cutoff_sequence,
                monitor_run_id=existing["id"],
                final_state=existing["final_state"] if "final_state" in existing.keys() else None,
                skip_reason="duplicate_cutoff",
            )
            return result
        if compatibility_segment_missing:
            audit["compatibility_reprocess_reason"] = "completed_preflight_missing_state_segment"
            existing = None
        if existing and existing["status"] == "running":
            result["skipped"] = True
            result["reason"] = "duplicate_cutoff"
            result["raw_reason"] = "run_already_in_progress"
            result["existing_run_id"] = existing["id"]
            return result

        # 3. 创建 monitor_run（PENDING）
        run_id = MonitorRunRepo.create(
            group_id,
            cutoff_sequence,
            trigger_type=trigger_type,
            shadow=is_shadow,
            scope=scope,
        )
        result["monitor_run_id"] = run_id
        audit["monitor_run_id"] = run_id

        # 4. 认领（PENDING -> RUNNING）
        if not MonitorRunRepo.claim(run_id):
            result["skipped"] = True
            result["reason"] = "internal_error"
            result["raw_reason"] = "claim_failed"
            audit["skip_reason"] = "internal_error"
            MonitorRunRepo.skip(run_id, "claim_failed", audit_json=audit)
            return result
        result["steps"]["claim"] = True

        stage1_started_at = None
        stage1_timer = None
        try:
            # 5. 收集上下文
            context = _collect_context_for_scope(group_id, scope)
            if fixed_candidate_end_sequence is not None:
                context = _filter_context_to_cutoff(context, cutoff_sequence)
            if context.get("session_id") is not None:
                audit["session_id"] = context.get("session_id")
            if context.get("session_no") is not None:
                audit["session_no"] = context.get("session_no")
            if context.get("task_id") is not None:
                audit["task_id"] = context.get("task_id")
            if context.get("discussion_id") is not None:
                audit["discussion_id"] = context.get("discussion_id")
            context["monitor_run_id"] = run_id
            result["steps"]["context_collected"] = True
            context_rows = _context_messages(context)
            student_context_rows = _student_context_messages(context)
            window_start_sequence, window_end_sequence = _sequence_bounds(context_rows, cutoff_sequence)
            input_sequences = _context_message_sequences(context_rows)
            previous_cutoff = (
                int(fixed_candidate_start_sequence) - 1
                if fixed_candidate_start_sequence is not None
                else _previous_completed_cutoff(group_id, cutoff_sequence)
            )
            new_student_count = _count_new_student_messages(
                group_id,
                previous_cutoff=previous_cutoff,
                cutoff_sequence=cutoff_sequence,
                session_id=audit.get("session_id"),
                session_no=audit.get("session_no"),
                task_id=audit.get("task_id"),
                discussion_id=audit.get("discussion_id"),
            )
            audit.update(
                {
                    "window_start_sequence": window_start_sequence,
                    "window_end_sequence": window_end_sequence,
                    "new_student_message_count": new_student_count,
                    "context_message_ids": _context_message_ids(context_rows),
                    "context_student_message_ids": _context_message_ids(student_context_rows),
                    "context_message_sequences": input_sequences,
                    "previous_completed_cutoff_sequence": previous_cutoff,
                }
            )
            _monitor_log(
                "context_window",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=cutoff_sequence,
                monitor_run_id=run_id,
                final_state=None,
                skip_reason=None,
                window_start_sequence=window_start_sequence,
                window_end_sequence=window_end_sequence,
                context_message_count=len(context_rows),
                new_student_message_count=new_student_count,
            )

            # 6. 提取特征
            if assessment_batch_id is None and pipeline_mode is not None:
                stage1_started_at = latency_timestamp()
                stage1_timer = latency_timer()
            features = FeatureService.extract(context)
            result["steps"]["features_extracted"] = True

            # 7. 规则检测
            rule_assessment = RuleDetector.detect(context, features)
            if trigger_type == "silence_check" and rule_assessment.get("winning_state_code") == "negative_silence":
                rule_assessment["state_source_hint"] = "silence_rule"
            result["steps"]["rule_detected"] = True
            result["rule_winning_state"] = rule_assessment.get("winning_state_code")
            scores = _rule_scores(rule_assessment)
            evidence_message_ids = _rule_evidence_message_ids(context, rule_assessment)
            audit.update(
                {
                    "rule_scores": scores,
                    "rule_candidate": rule_assessment.get("winning_state_code"),
                    "rule_confidence": rule_assessment.get("winning_score"),
                    "rule_evidence_message_ids": evidence_message_ids,
                    "rule_assessment_status": rule_assessment.get("assessment_status"),
                }
            )
            _monitor_log(
                "rule_scored",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=cutoff_sequence,
                monitor_run_id=run_id,
                final_state=None,
                skip_reason=None,
                rule_candidate=audit.get("rule_candidate"),
                rule_confidence=audit.get("rule_confidence"),
                rule_scores=scores,
            )

            llm_result = None
            llm_envelope = None
            llm_meta = None
            result["llm_called"] = False
            result["steps"]["state_detector_skipped"] = True
            audit["state_llm_called"] = False

            # 8. 状态确认器门控：规则只做初筛，LLM 只在条件满足时确认状态
            state_gate = TriggerPolicy.should_run_state_detector(
                group_id,
                cutoff_sequence,
                rule_assessment,
                trigger_type=trigger_type,
                new_student_message_count=new_student_count,
                context=context,
            )
            should_run_state_detector = bool(state_gate.get("gate"))
            gate_reason = state_gate.get("gate_reason")
            if assessment_batch_id is not None:
                should_run_state_detector = True
                gate_reason = "assessment_batch_claimed"
            result["steps"]["state_detector_gate_evaluated"] = True
            result["state_detector_gate"] = state_gate
            audit.update(
                {
                    "state_llm_gate": should_run_state_detector,
                    "gate": should_run_state_detector,
                    "state_llm_gate_reason": gate_reason,
                    "gate_reason": gate_reason,
                    "max_rule_score": state_gate.get("max_rule_score"),
                    "new_student_message_count": state_gate.get("new_student_message_count", new_student_count),
                }
            )
            if assessment_batch_id is None:
                try:
                    stage1_result = Stage1PipelineService.build_result(
                        group_id=group_id,
                        monitor_run_id=run_id,
                        trigger_type=trigger_type,
                        cutoff_sequence=cutoff_sequence,
                        scope=audit,
                        window_start_sequence=window_start_sequence,
                        window_end_sequence=window_end_sequence,
                        new_student_message_count=new_student_count,
                        rule_assessment=rule_assessment,
                        rule_scores=scores,
                        evidence_message_ids=evidence_message_ids,
                        features=features,
                        state_gate=state_gate,
                        pipeline_mode=pipeline_mode,
                    )
                    stage1_result = Stage1PipelineService.persist(stage1_result)
                    if stage1_result.persisted:
                        record_latency_event(
                            stage="pipeline",
                            event="pipeline_created",
                            pipeline_run_id=stage1_result.pipeline_run_id,
                            occurred_at=stage1_started_at,
                        )
                        record_latency_event(
                            stage="stage1",
                            event="stage1_started",
                            pipeline_run_id=stage1_result.pipeline_run_id,
                            occurred_at=stage1_started_at,
                        )
                        record_latency_event(
                            stage="stage1",
                            event="stage1_finished",
                            pipeline_run_id=stage1_result.pipeline_run_id,
                            elapsed=latency_elapsed_ms(stage1_timer),
                        )
                    result["stage1_result"] = stage1_result.to_dict()
                    audit["strategy_pipeline_run_id"] = stage1_result.pipeline_run_id
                    audit["stage1_result"] = stage1_result.to_dict()
                    if not allow_state_llm:
                        # Per-message/silence Stage 1 is preliminary scheduling
                        # evidence only.  It has no Stage 2 consumer in this
                        # task, so it must never own the student-input lease.
                        lock_result = Stage1PipelineService.acquire_room_lock(
                            stage1_result,
                            enabled=False,
                        )
                        safe_lock_result = {
                            key: value
                            for key, value in lock_result.items()
                            if key != "lock_token"
                        }
                        result["stage1_lock_result"] = safe_lock_result
                        audit["stage1_lock_result"] = safe_lock_result
                        if trigger_type == "silence_check":
                            terminal_result = (
                                Stage1PipelineService.finalize_without_stage2(
                                    stage1_result,
                                    reason="SILENCE_STAGE1_NO_STAGE2_CONSUMER",
                                )
                            )
                            result["stage1_terminal_result"] = terminal_result
                            audit["stage1_terminal_result"] = terminal_result
                except Exception as stage1_exc:
                    logger.warning(
                        "Failed to persist stage1 pipeline run for group %s run=%s: %s",
                        group_id,
                        run_id,
                        stage1_exc,
                        exc_info=True,
                    )
                    result["stage1_result"] = {
                        "persisted": False,
                        "error": str(stage1_exc)[:200],
                    }
                    audit["stage1_result_error"] = str(stage1_exc)[:500]

            if should_run_state_detector and allow_state_llm:
                detector_context = _build_state_detector_context(
                    context,
                    group_id=group_id,
                    cutoff_sequence=cutoff_sequence,
                    previous_cutoff=previous_cutoff,
                    scope=audit,
                )
                detector_context.update(
                    {
                        "assessment_batch_id": assessment_batch_id,
                        "candidate_start_sequence": fixed_candidate_start_sequence,
                        "candidate_end_sequence": fixed_candidate_end_sequence,
                        "pipeline_run_id": audit.get("strategy_pipeline_run_id"),
                    }
                )
                if assessment_batch_id is not None:
                    batch_scope = query_one(
                        """
                        SELECT discussion_id, attempt_count, max_attempts
                        FROM state_assessment_batches
                        WHERE id=?
                        """,
                        (assessment_batch_id,),
                    )
                    if batch_scope:
                        detector_context["discussion_id"] = batch_scope["discussion_id"]
                        detector_context["batch_scheduler_attempt_count"] = int(
                            batch_scope["attempt_count"] or 0
                        )
                        detector_context["batch_scheduler_max_attempts"] = int(
                            batch_scope["max_attempts"] or 0
                        )
                audit.update(
                    {
                        "state_detector_context_message_ids": detector_context.get("state_detector_allowed_evidence_message_ids") or [],
                        "state_detector_new_student_message_ids": detector_context.get("state_detector_new_student_message_ids") or [],
                    }
                )
                try:
                    from services.discussion_pipeline_v2.llm_state_detector import LLMStateDetector

                    stage2_call_id = str(uuid.uuid4())
                    stage2_llm_timer = latency_timer()
                    record_latency_event(
                        stage="stage2",
                        event="stage2_llm_started",
                        assessment_batch_id=assessment_batch_id,
                        call_id=stage2_call_id,
                        attempt=1,
                    )
                    try:
                        llm_envelope = LLMStateDetector.detect(
                            detector_context, rule_assessment, features
                        )
                    finally:
                        record_latency_event(
                            stage="stage2",
                            event="stage2_llm_finished",
                            assessment_batch_id=assessment_batch_id,
                            call_id=stage2_call_id,
                            attempt=1,
                            elapsed=latency_elapsed_ms(stage2_llm_timer),
                        )
                    llm_result = (llm_envelope or {}).get("result")
                    llm_meta = (llm_envelope or {}).get("meta") or {}
                    result["llm_called"] = not bool(llm_meta.get("analysis_skipped"))
                    result["steps"]["state_detector_skipped"] = bool(llm_meta.get("analysis_skipped"))
                    result["steps"]["state_detector_called"] = result["llm_called"]
                    audit.update(
                        {
                            "state_llm_called": result["llm_called"],
                            "state_llm_result_state": (llm_result or {}).get("primary_state") or (llm_result or {}).get("state_code"),
                            "state_llm_confidence": (llm_result or {}).get("confidence") if isinstance(llm_result, dict) else None,
                            "state_llm_validation_status": llm_meta.get("validation_status"),
                            "state_llm_failure_reason": (
                                llm_meta.get("failure_reason")
                                or llm_meta.get("failure_type")
                                or llm_meta.get("schema_error")
                            ),
                        }
                    )
                    audit.update(_llm_audit_error_fields(llm_meta))
                except Exception as llm_exc:
                    logger.warning(
                        "State detector failed for group %s run=%s: %s",
                        group_id,
                        run_id,
                        llm_exc,
                        exc_info=True,
                    )
                    llm_result = {
                        "primary_state": "unknown",
                        "state_code": "unknown",
                        "confidence": 0.0,
                        "evidence_message_ids": [],
                        "secondary_state": None,
                        "reason": str(llm_exc)[:200],
                        "detector_error": True,
                    }
                    llm_meta = {
                        "analysis_failed": True,
                        "analysis_skipped": False,
                        "llm_required": True,
                        "success": False,
                        "failure_type": "llm_error",
                        "failure_reason": "llm_error",
                        "failure_message": str(llm_exc)[:200],
                        "validation_status": "failed",
                        "schema_valid": False,
                        "fallback_required": True,
                    }
                    llm_envelope = {"result": llm_result, "meta": llm_meta}
                    result["llm_called"] = True
                    result["steps"]["state_detector_called"] = True
                    result["steps"]["state_detector_skipped"] = False
                    audit.update(
                        {
                            "state_llm_called": True,
                            "state_llm_validation_status": "failed",
                            "state_llm_failure_reason": "llm_error",
                            "llm_error": str(llm_exc)[:200],
                        }
                    )
            else:
                result["state_detector_skip_reason"] = (
                    "state_llm_deferred_to_batch_scheduler"
                    if should_run_state_detector and not allow_state_llm
                    else gate_reason
                )

            result["state_llm_result"] = llm_result
            result["state_llm_meta"] = llm_meta or {
                "analysis_skipped": True,
                "success": False,
                "skip_reason": result.get("state_detector_skip_reason"),
            }

            # 9. 融合结果
            fusion = DecisionFusion.fuse(rule_assessment, llm_result, llm_meta)
            result["steps"]["fusion_done"] = True
            result["fused_state"] = fusion.get("fused_state_code")
            result["fused_confidence"] = fusion.get("confidence")
            final_state = fusion.get("fused_state_code")
            final_confidence = fusion.get("confidence")
            audit["final_state"] = final_state
            audit["final_confidence"] = final_confidence
            audit["decision_source"] = fusion.get("decision_source")
            _monitor_log(
                "final_state",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=cutoff_sequence,
                monitor_run_id=run_id,
                final_state=final_state,
                skip_reason=None,
                final_confidence=final_confidence,
                decision_source=fusion.get("decision_source"),
            )

            try:
                should_call, call_reason = TriggerPolicy.should_enqueue_strategy_review(
                    group_id,
                    cutoff_sequence,
                    rule_assessment,
                    final_state=final_state,
                    confidence=final_confidence,
                    trigger_type=trigger_type,
                )
            except AttributeError:
                should_call, call_reason = TriggerPolicy.should_call_llm(
                    group_id, cutoff_sequence, rule_assessment, trigger_type=trigger_type,
                )
            if should_call and not bool(fusion.get("should_intervene")):
                should_call = False
                call_reason = (
                    "autonomous_regulation_observed"
                    if fusion.get("self_regulation_detected")
                    else "fusion_should_intervene_false"
                )
            result["steps"]["strategy_trigger_evaluated"] = True
            result["strategy_review_candidate"] = bool(should_call)
            result["strategy_review_trigger_reason"] = call_reason
            audit["trigger_policy_reason"] = call_reason
            audit["strategy_review_reason"] = call_reason
            audit["strategy_review_candidate"] = bool(should_call)
            handling_evidence_ids = _state_evidence_message_ids(
                context,
                rule_assessment,
                llm_result,
                final_state,
            )
            handling_evidence_sequences = _unique_sorted_ints(
                _message_id_to_sequence_map(
                    group_id=group_id,
                    message_ids=handling_evidence_ids,
                    context_rows=student_context_rows,
                    session_id=audit.get("session_id"),
                    session_no=audit.get("session_no"),
                    task_id=audit.get("task_id"),
                    discussion_id=audit.get("discussion_id"),
                ).values()
            )
            handling_new_student_sequences = _unique_sorted_ints(
                _message_sequence(row)
                for row in student_context_rows
                if _message_sequence(row) is not None
                and _message_sequence(row) <= cutoff_sequence
                and (
                    previous_cutoff is None
                    or _message_sequence(row) > previous_cutoff
                )
                and _message_matches_detection_scope(
                    row,
                    session_id=audit.get("session_id"),
                    session_no=audit.get("session_no"),
                    task_id=audit.get("task_id"),
                    discussion_id=audit.get("discussion_id"),
                )
            )
            handling_decision = _build_state_handling_decision(
                final_state=final_state,
                assessment_status=fusion.get("assessment_status"),
                decision_source=fusion.get("decision_source"),
                confidence=final_confidence,
                trigger_type=trigger_type,
                persist_state_segment=persist_state_segment,
                schedule_strategy=schedule_strategy,
                should_call=should_call,
                call_reason=call_reason,
                llm_result=llm_result,
                evidence_sequences=handling_evidence_sequences,
                new_student_sequences=handling_new_student_sequences,
                autonomous_regulation_observed=bool(
                    fusion.get("self_regulation_detected")
                ),
            )
            should_call = handling_decision["should_schedule_strategy"]
            call_reason = handling_decision["schedule_reason"]
            result["strategy_review_candidate"] = bool(should_call)
            result["strategy_review_trigger_reason"] = call_reason
            audit["trigger_policy_reason"] = call_reason
            audit["strategy_review_reason"] = call_reason
            audit["strategy_review_candidate"] = bool(should_call)
            result["state_handling_decision"] = handling_decision
            audit["state_handling_decision"] = handling_decision
            audit["persist_state_segment"] = handling_decision["should_persist"]
            audit["persist_reason"] = handling_decision["persist_reason"]
            audit["schedule_strategy"] = handling_decision[
                "should_schedule_strategy"
            ]
            audit["schedule_reason"] = handling_decision["schedule_reason"]

            # 10a. persist detection results to state_assessments / group_states
            persisted_state = None

            try:
                resolved_session_id = (
                    audit.get("session_id")
                    or context.get("session_id")
                    or get_active_session_id()
                )

                persist_rule_state = {

                    "group_id": group_id,

                    "task_id": context.get("task_id"),

                    "session_no": context.get("session_no"),

                    "session_id": resolved_session_id,

                    "monitor_run_id": run_id,

                    "state_code": rule_assessment.get("winning_state_code"),

                    "state_score": rule_assessment.get("winning_score"),

                    "rule_assessment": rule_assessment,

                    "state_source_hint": rule_assessment.get("state_source_hint"),

                    "context_json": context,

                    "feature_json": features,

                    "evidence": str(fusion),

                    "analysis_started_at": now_str(),

                    "analysis_finished_at": now_str(),

                    "detector_version": PIPELINE_V2_ANALYZER_VERSION,

                }

                persisted_state = persist_state_assessment(

                    persist_rule_state,

                    llm_result=llm_result,

                    llm_meta=llm_meta,

                )

                result["steps"]["state_persisted"] = True
                result["state_assessment_id"] = persisted_state.get("assessment_id")
                result["group_state_id"] = persisted_state.get("group_state_id")
                audit.update(
                    {
                        "state_assessment_written": True,
                        "state_assessment_id": persisted_state.get("assessment_id"),
                        "group_state_written": bool(persisted_state.get("group_state_id")),
                        "group_state_id": persisted_state.get("group_state_id"),
                    }
                )
                _monitor_log(
                    "state_assessment_written",
                    group_id=group_id,
                    session_id=audit.get("session_id"),
                    cutoff_sequence=cutoff_sequence,
                    monitor_run_id=run_id,
                    final_state=final_state,
                    skip_reason=None,
                    state_assessment_id=persisted_state.get("assessment_id"),
                    group_state_id=persisted_state.get("group_state_id"),
                )

            except Exception as persist_exc:

                logger.warning(
                    "Failed to persist state_assessment for group %s run=%s: %s",
                    group_id,
                    run_id,
                    persist_exc,
                    exc_info=True,
                )

                result["steps"]["state_persisted"] = False

                result["state_persist_error"] = str(persist_exc)
                audit.update(
                    {
                        "state_assessment_written": False,
                        "group_state_written": False,
                        "state_persistence_error": str(persist_exc),
                        "skip_reason": "state_persistence_failed",
                    }
                )
                _monitor_log(
                    "state_assessment_failed",
                    group_id=group_id,
                    session_id=audit.get("session_id"),
                    cutoff_sequence=cutoff_sequence,
                    monitor_run_id=run_id,
                    final_state=final_state,
                    skip_reason="state_persistence_failed",
                    error_type=persist_exc.__class__.__name__,
                )


            # 10a.1. Persist teacher-facing state segment immediately after state confirmation.
            if (
                audit.get("state_assessment_written")
                and handling_decision["should_persist"]
            ):
                segment_result = None
                segment_audit = {
                    "segment_write_attempted": final_state != "unknown",
                    "segment_written": False,
                    "segment_id": None,
                    "segment_ids": [],
                    "segment_skip_reason": None,
                }
                try:
                    segment_result = _persist_state_monitor_segment(
                        group_id=group_id,
                        run_id=run_id,
                        persisted_state=persisted_state,
                        final_state=final_state,
                        final_confidence=final_confidence,
                        trigger_type=trigger_type,
                        silence_expected_sequence=silence_expected_sequence,
                        silence_expected_message_at=silence_expected_message_at,
                        silence_expected_session_id=silence_expected_session_id,
                        silence_expected_task_id=silence_expected_task_id,
                        context=context,
                        student_context_rows=student_context_rows,
                        rule_assessment=rule_assessment,
                        llm_result=llm_result,
                        previous_cutoff=previous_cutoff,
                        cutoff_sequence=cutoff_sequence,
                        window_start_sequence=window_start_sequence,
                        window_end_sequence=window_end_sequence,
                        audit=audit,
                    )
                    result["state_segment_result"] = segment_result
                    result["steps"]["state_segment_persisted"] = not bool(segment_result.get("skipped"))
                    segment_ids = segment_result.get("segment_ids") or []
                    if segment_result.get("segment_id") and segment_result.get("segment_id") not in segment_ids:
                        segment_ids = [segment_result.get("segment_id"), *segment_ids]
                    represented = not bool(segment_result.get("skipped")) and bool(
                        segment_ids or segment_result.get("saved_count", 0) > 0
                    )
                    segment_audit.update(
                        {
                            "segment_write_attempted": final_state != "unknown",
                            "segment_written": represented,
                            "segment_id": segment_ids[0] if segment_ids else None,
                            "segment_ids": segment_ids,
                            "segment_skip_reason": segment_result.get("reason") if segment_result.get("skipped") else None,
                            "segment_state_code": segment_result.get("state_code") or final_state,
                            "segment_range_type": segment_result.get("range_type"),
                            "segment_start_message_id": segment_result.get("start_message_id"),
                            "segment_end_message_id": segment_result.get("end_message_id"),
                        }
                    )
                except Exception as segment_exc:
                    logger.warning(
                        "Failed to persist state_monitor segment for group %s run=%s state=%s: %s",
                        group_id,
                        run_id,
                        final_state,
                        segment_exc,
                        exc_info=True,
                    )
                    result["steps"]["state_segment_persisted"] = False
                    result["state_segment_error"] = str(segment_exc)
                    segment_audit.update(
                        {
                            "segment_write_attempted": final_state != "unknown",
                            "segment_written": False,
                            "segment_persistence_error": str(segment_exc),
                            "segment_skip_reason": "segment_persistence_failed",
                        }
                    )
                    if not audit.get("skip_reason"):
                        audit["skip_reason"] = "segment_persistence_failed"
                    safe_write_audit_log(
                        action_type="collaboration_state_segments.persist_failed",
                        actor_type="system",
                        actor_id="pipeline_v2",
                        target_type="monitor_run",
                        target_id=run_id,
                        metadata={
                            "group_id": group_id,
                            "session_id": audit.get("session_id") or get_active_session_id(),
                            "source_run_id": run_id,
                            "final_state": final_state,
                            "error_type": segment_exc.__class__.__name__,
                        },
                    )
                audit.update(segment_audit)
                _monitor_log(
                    "segment_write_result",
                    group_id=group_id,
                    session_id=audit.get("session_id"),
                    cutoff_sequence=cutoff_sequence,
                    monitor_run_id=run_id,
                    final_state=final_state,
                    skip_reason=audit.get("skip_reason"),
                    segment_written=audit.get("segment_written"),
                    segment_id=audit.get("segment_id"),
                    segment_skip_reason=audit.get("segment_skip_reason"),
                )
            elif audit.get("state_assessment_written"):
                audit.update(
                    {
                        "segment_write_attempted": False,
                        "segment_written": False,
                        "segment_skip_reason": handling_decision["persist_reason"],
                    }
                )

            # 10b. persist autonomous regulation events
            try:
                from services.autonomous_regulation_service import persist_autonomous_regulation_event
                session_id_val = audit.get("session_id") or get_active_session_id()
                persist_autonomous_regulation_event(
                    group_id,
                    fusion,
                    session_id=session_id_val,
                    task_id=context.get("task_id"),
                    session_no=context.get("session_no"),
                    monitor_run_id=run_id,
                    context=context,
                )
            except Exception as are_exc:
                logger.warning("Failed to persist autonomous regulation event for group %s: %s", group_id, are_exc)
            # 10. 写入 monitor_run
            initial_skip_reason = _derive_detection_skip_reason(
                final_state=final_state,
                new_student_message_count=new_student_count,
                rule_scores=scores,
                should_call=should_call,
                call_reason=call_reason,
                auto_enabled=bool(AUTO_INTERVENTION_V2_ENABLED),
                group_auto_enabled=bool(group["auto_intervention_enabled"]),
                shadow=is_shadow,
                state_persisted=bool(audit.get("state_assessment_written")),
            )
            if not audit.get("skip_reason"):
                audit["skip_reason"] = initial_skip_reason
            if final_state == "unknown" and not audit.get("segment_skip_reason"):
                audit["segment_skip_reason"] = "final_state_unknown"
            MonitorRunRepo.complete(
                run_id,
                final_state=final_state,
                confidence=final_confidence,
                rule_result_json=rule_assessment,
                llm_result_json=llm_envelope if llm_envelope else None,
                audit_json=audit,
                context_from_sequence=window_start_sequence,
                context_to_sequence=window_end_sequence,
                input_message_sequences=input_sequences,
                evidence_sequences=handling_evidence_sequences,
            )
            _monitor_log(
                "monitor_run_completed",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=cutoff_sequence,
                monitor_run_id=run_id,
                final_state=final_state,
                skip_reason=audit.get("skip_reason"),
            )
            # Write audit log for detection completion
            if run_id:
                used_llm = bool(llm_result) or result.get("llm_called", False)
                audit_meta = {
                    "group_id": group_id,
                    "task_id": context.get("task_id"),
                    "session_id": audit.get("session_id") or get_active_session_id(),
                    "final_state": final_state,
                    "confidence": final_confidence,
                    "used_llm": used_llm,
                    "monitor_run_id": run_id,
                    "shadow": is_shadow,
                    "skip_reason": audit.get("skip_reason"),
                }
                safe_write_audit_log(
                    action_type="detection.complete",
                    actor_type="system",
                    actor_id="pipeline_v2",
                    target_type="monitor_run",
                    target_id=run_id,
                    metadata=audit_meta,
                )
            # 10b. 如果 V2 自动介入已启用，调度介入任务
            _debug_log("intervention_scheduling group=%s run=%s enabled=%s shadow=%s",
                group_id, run_id, AUTO_INTERVENTION_V2_ENABLED, is_shadow)
            batch_llm_failed = bool(
                assessment_batch_id is not None
                and (
                    not (llm_meta or {}).get("success")
                    or (llm_meta or {}).get("analysis_failed")
                    or (llm_meta or {}).get("analysis_skipped")
                )
            )
            strategy_scheduling_enabled = handling_decision[
                "strategy_scheduling_enabled"
            ]
            if not allow_state_llm and not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
                if strategy_scheduling_enabled:
                    result["stage1_direct_strategy_scheduling_blocked"] = True
                    audit["stage1_direct_strategy_scheduling_blocked"] = True
                strategy_scheduling_enabled = False
                if bool(should_call):
                    result["no_intervention_reason"] = "stage1_deferred_to_stage2"
                    audit["skip_reason"] = audit.get("skip_reason") or "stage1_deferred_to_stage2"
            if (
                strategy_scheduling_enabled
                and AUTO_INTERVENTION_V2_ENABLED
                and bool(group["auto_intervention_enabled"])
                and not is_shadow
                and not batch_llm_failed
            ):
                _fs = final_state or "unknown"
                _fc = float(final_confidence or 0)
                target_evidence_start, target_evidence_end = (
                    _state_evidence_sequence_bounds(
                        group_id=group_id,
                        context=context,
                        student_context_rows=student_context_rows,
                        rule_assessment=rule_assessment,
                        llm_result=llm_result,
                        final_state=_fs,
                        session_id=audit.get("session_id"),
                        session_no=audit.get("session_no"),
                        task_id=audit.get("task_id"),
                        discussion_id=audit.get("discussion_id"),
                        fallback_end=cutoff_sequence,
                    )
                )
                help_guard = _help_request_blocks_strategy(
                    group_id,
                    trigger_type=trigger_type,
                    cutoff_sequence=cutoff_sequence,
                    window_start_sequence=target_evidence_start,
                    scope=audit,
                    target_state_code=_fs,
                    target_segment_id=audit.get("segment_id"),
                    target_end_sequence=target_evidence_end,
                )
                audit.update(
                    {
                        "help_request_guard_evaluated": bool(
                            help_guard.get("guard_evaluated")
                        ),
                        "help_request_guard": {
                            key: value
                            for key, value in help_guard.items()
                            if key != "reason"
                        },
                        "help_request_strategy_blocked": bool(help_guard.get("blocked")),
                        "help_request_strategy_block_reason": help_guard.get("reason_code"),
                        "help_request_strategy_block_ids": help_guard.get("help_request_ids") or [],
                    }
                )
                was_strategy_candidate = bool(should_call)
                _conf_ok = _fc >= PIPELINE_V2_MIN_INTERVENTION_CONFIDENCE
                if help_guard.get("blocked"):
                    if was_strategy_candidate and _fs not in ("unknown", "positive_collaboration") and _conf_ok:
                        try:
                            from services.intervention_pipeline_v2.intervention_service import InterventionService

                            skip_result = InterventionService.record_skipped_for_monitor(
                                run_id,
                                state_assessment_id=(persisted_state or {}).get("assessment_id"),
                                group_id=group_id,
                                session_id=audit.get("session_id") or context.get("session_id"),
                                task_id=context.get("task_id"),
                                cutoff_sequence=cutoff_sequence,
                                trigger_source="auto_state",
                                reason=help_guard.get("reason_code")
                                or "pending_help_request",
                            )
                            result["strategy_review_enqueue_result"] = skip_result
                            audit["strategy_review_skip_run_id"] = skip_result.get("intervention_run_id")
                        except Exception as skip_exc:
                            logger.warning(
                                "Failed to record skipped intervention for help request window %s: %s",
                                run_id,
                                skip_exc,
                            )
                    should_call = False
                    call_reason = help_guard.get("reason_code") or "pending_or_recent_help_request"
                    result["no_intervention_reason"] = call_reason
                    audit["skip_reason"] = call_reason
                    audit["strategy_review_candidate"] = False
                    result["strategy_review_candidate"] = False
                silence_claim = None
                if (
                    should_call
                    and _fs == "negative_silence"
                    and trigger_type == "silence_check"
                ):
                    silence_precheck = _silence_strategy_precheck(
                        group_id=group_id,
                        session_id=audit.get("session_id"),
                        session_no=audit.get("session_no"),
                        task_id=audit.get("task_id") or context.get("task_id"),
                        cutoff_sequence=cutoff_sequence,
                        segment_id=audit.get("segment_id"),
                        final_state=_fs,
                    )
                    audit["silence_strategy_precheck"] = silence_precheck
                    result["silence_strategy_precheck"] = silence_precheck
                    if not silence_precheck.get("allowed"):
                        should_call = False
                        call_reason = (
                            silence_precheck.get("reason")
                            or "silence_strategy_precheck_failed"
                        )
                        result["no_intervention_reason"] = call_reason
                        audit["skip_reason"] = call_reason
                        audit["strategy_review_candidate"] = False
                        result["strategy_review_candidate"] = False
                        if audit.get("segment_id"):
                            from services.collaboration_state_segment_service import (
                                CollaborationStateSegmentService,
                            )

                            CollaborationStateSegmentService.record_silence_intervention_disposition(
                                segment_id=audit["segment_id"],
                                disposition=f"SKIPPED:{call_reason}",
                            )
                    elif not audit.get("segment_id"):
                        should_call = False
                        call_reason = "silence_segment_missing"
                        result["no_intervention_reason"] = call_reason
                        audit["skip_reason"] = call_reason
                        audit["strategy_review_candidate"] = False
                        result["strategy_review_candidate"] = False
                    else:
                        from services.collaboration_state_segment_service import (
                            CollaborationStateSegmentService,
                        )

                        silence_claim = (
                            CollaborationStateSegmentService.claim_silence_intervention(
                                segment_id=audit["segment_id"],
                                monitor_run_id=run_id,
                            )
                        )
                        audit["silence_intervention_claim"] = silence_claim
                        result["silence_intervention_claim"] = silence_claim
                        audit["silence_event_key"] = silence_claim.get(
                            "silence_event_key"
                        )
                        if not silence_claim.get("claimed"):
                            should_call = False
                            call_reason = silence_claim.get(
                                "reason"
                            ) or "silence_intervention_already_scheduled"
                            result["no_intervention_reason"] = call_reason
                            audit["skip_reason"] = call_reason
                            audit["strategy_review_candidate"] = False
                            result["strategy_review_candidate"] = False
                _triggerable = bool(should_call) and _fs not in ("unknown", "positive_collaboration")
                _debug_log("intervention_scheduling_check group=%s state=%s triggerable=%s conf=%s conf_ok=%s will_schedule=%s",
                    group_id, _fs, _triggerable, _fc, _conf_ok, (_triggerable and _conf_ok))
                if _triggerable and _conf_ok:
                    _debug_log("intervention_scheduling_schedule group=%s run=%s", group_id, run_id)
                    schedule_result = MonitoringService._schedule_v2_intervention(
                        run_id,
                        state_assessment_id=(persisted_state or {}).get("assessment_id"),
                        group_id=group_id,
                        session_id=audit.get("session_id") or context.get("session_id"),
                        task_id=context.get("task_id"),
                        cutoff_sequence=cutoff_sequence,
                        trigger_source=(
                            "silence_rule"
                            if _fs == "negative_silence"
                            and trigger_type == "silence_check"
                            else "auto_state"
                        ),
                    ) or {}
                    result["strategy_review_enqueue_result"] = schedule_result
                    if schedule_result.get("enqueued"):
                        audit["strategy_review_enqueued"] = True
                        audit["skip_reason"] = None
                        if silence_claim and audit.get("segment_id"):
                            CollaborationStateSegmentService.record_silence_intervention_disposition(
                                segment_id=audit["segment_id"],
                                disposition="ENQUEUED",
                            )
                    else:
                        audit["strategy_review_enqueued"] = False
                        audit["skip_reason"] = "strategy_enqueue_failed"
                        audit["strategy_enqueue_error"] = schedule_result.get("error")
                        if silence_claim and audit.get("segment_id"):
                            CollaborationStateSegmentService.record_silence_intervention_disposition(
                                segment_id=audit["segment_id"],
                                disposition="ENQUEUE_FAILED",
                                clear_schedule_claim=True,
                            )
                elif should_call and not _conf_ok:
                    result["no_intervention_reason"] = "rule_candidate_confidence_below_minimum"
                    audit["skip_reason"] = audit.get("skip_reason") or "all_rule_scores_below_threshold"
            else:
                if batch_llm_failed:
                    audit["skip_reason"] = audit.get("skip_reason") or "state_assessment_batch_failed"
                elif not strategy_scheduling_enabled:
                    audit["skip_reason"] = audit.get("skip_reason") or "strategy_scheduling_deferred"
                elif not bool(group["auto_intervention_enabled"]):
                    audit["skip_reason"] = (
                        audit.get("skip_reason")
                        or "group_auto_intervention_disabled"
                    )
                else:
                    audit["skip_reason"] = audit.get("skip_reason") or "auto_intervention_disabled"
            MonitorRunRepo.update_monitor_audit(
                run_id,
                {
                    "strategy_review_enqueued": audit.get("strategy_review_enqueued", False),
                    "skip_reason": audit.get("skip_reason"),
                    "strategy_enqueue_error": audit.get("strategy_enqueue_error"),
                    "help_request_strategy_blocked": audit.get("help_request_strategy_blocked", False),
                    "help_request_strategy_block_reason": audit.get("help_request_strategy_block_reason"),
                    "help_request_strategy_block_ids": audit.get("help_request_strategy_block_ids") or [],
                    "help_request_guard_evaluated": audit.get(
                        "help_request_guard_evaluated", False
                    ),
                    "help_request_guard": audit.get("help_request_guard"),
                    "silence_strategy_precheck": audit.get(
                        "silence_strategy_precheck"
                    ),
                    "silence_intervention_claim": audit.get(
                        "silence_intervention_claim"
                    ),
                    "silence_event_key": audit.get("silence_event_key"),
                },
            )
            _monitor_log(
                "strategy_enqueue_result",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=cutoff_sequence,
                monitor_run_id=run_id,
                final_state=final_state,
                skip_reason=audit.get("skip_reason"),
                strategy_review_enqueued=audit.get("strategy_review_enqueued", False),
            )
            result["steps"]["completed"] = True

        except Exception as exc:
            logger.exception("MonitoringService.run_detection failed for group %s", group_id)
            audit.update(
                {
                    "skip_reason": "internal_error",
                    "internal_error": str(exc),
                    "error_type": exc.__class__.__name__,
                }
            )
            if run_id:
                MonitorRunRepo.fail(run_id, str(exc)[:500], audit_json=audit)
            result["error"] = str(exc)
            result["steps"]["failed"] = True
            _monitor_log(
                "task_failed",
                group_id=group_id,
                session_id=audit.get("session_id"),
                cutoff_sequence=audit.get("cutoff_sequence"),
                monitor_run_id=run_id,
                final_state=audit.get("final_state"),
                skip_reason="internal_error",
                error_type=exc.__class__.__name__,
            )

        return result

    @staticmethod
    def diagnose_detection(
        group_id: int,
        *,
        session_id: int = None,
        cutoff_sequence: int = None,
        trigger_type: str = "new_message",
    ) -> dict:
        """Development/test helper: inspect one detection path without writes."""
        if cutoff_sequence is None:
            row = query_one(
                """
                SELECT sequence
                FROM messages
                WHERE group_id=? AND COALESCE(role, '')='student'
                  AND sequence IS NOT NULL
                ORDER BY sequence DESC, id DESC
                LIMIT 1
                """,
                (group_id,),
            )
            cutoff_sequence = _safe_int(row["sequence"], 0) if row else 0
        scope = _latest_message_scope(group_id, cutoff_sequence)
        if session_id is not None:
            scope["session_id"] = session_id
            session = query_one("SELECT session_no, task_id FROM experiment_sessions WHERE id=?", (session_id,))
            if session:
                scope["session_no"] = session["session_no"]
                scope["task_id"] = session["task_id"]
        context = _collect_context_for_scope(group_id, scope)
        features = FeatureService.extract(context)
        rule_assessment = RuleDetector.detect(context, features)
        fusion = DecisionFusion.fuse(rule_assessment, None, None)
        context_rows = _context_messages(context)
        previous_cutoff = _previous_completed_cutoff(group_id, cutoff_sequence)
        existing = MonitorRunRepo.find_by_unique_key(
            group_id,
            cutoff_sequence,
            trigger_type=trigger_type,
        )
        existing_audit = {}
        if existing and existing["rule_result_json"]:
            existing_payload = MonitorRunRepo._load_json(existing["rule_result_json"])
            existing_audit = existing_payload.get("monitor_audit") or {}
        return {
            "group_id": group_id,
            "session_id": context.get("session_id") or scope.get("session_id"),
            "session_no": context.get("session_no") or scope.get("session_no"),
            "task_id": context.get("task_id") or scope.get("task_id"),
            "cutoff_sequence": cutoff_sequence,
            "trigger_type": trigger_type,
            "context_message_ids": _context_message_ids(context_rows),
            "context_message_sequences": _context_message_sequences(context_rows),
            "new_student_message_count": _count_new_student_messages(
                group_id,
                previous_cutoff=previous_cutoff,
                cutoff_sequence=cutoff_sequence,
                session_id=context.get("session_id") or scope.get("session_id"),
                session_no=context.get("session_no") or scope.get("session_no"),
                task_id=context.get("task_id") or scope.get("task_id"),
                discussion_id=context.get("discussion_id")
                or scope.get("discussion_id"),
            ),
            "rule_scores": _rule_scores(rule_assessment),
            "rule_candidate": rule_assessment.get("winning_state_code"),
            "rule_confidence": rule_assessment.get("winning_score"),
            "final_state": fusion.get("fused_state_code"),
            "final_confidence": fusion.get("confidence"),
            "state_llm_called": False,
            "existing_monitor_run_id": existing["id"] if existing else None,
            "existing_monitor_audit": existing_audit,
        }

    @staticmethod
    def schedule_silence_check(
        group_id: int,
        expected_sequence: int,
        *,
        expected_last_student_message_at: str = None,
        expected_session_id: int = None,
        expected_task_id: int = None,
    ):
        """调度沉默检测任务。"""
        if not DISCUSSION_PIPELINE_V2_ENABLED:
            return

        from huey_instance import huey
        from agent.monitoring_tasks import check_room_silence

        # 用 delay 替代 schedule 以便参数立即传递
        silence_delay = max(
            int(PIPELINE_V2_SILENCE_DELAY_SECONDS or 0),
            int(ONLINE_SILENCE_NO_MSG_SECONDS or 0) + 5,
        )
        if (
            expected_last_student_message_at is None
            or expected_session_id is None
            or expected_task_id is None
        ):
            student_message = query_one(
                """
                SELECT created_at, session_id, task_id
                FROM messages
                WHERE group_id=? AND sequence=?
                  AND COALESCE(role, '')='student'
                ORDER BY id DESC
                LIMIT 1
                """,
                (group_id, expected_sequence),
            )
            if student_message:
                expected_last_student_message_at = (
                    expected_last_student_message_at
                    or student_message["created_at"]
                )
                expected_session_id = (
                    expected_session_id
                    if expected_session_id is not None
                    else student_message["session_id"]
                )
                expected_task_id = (
                    expected_task_id
                    if expected_task_id is not None
                    else student_message["task_id"]
                )
        check_room_silence.schedule(
            args=(
                group_id,
                expected_sequence,
                expected_last_student_message_at,
                expected_session_id,
                expected_task_id,
            ),
            delay=silence_delay,
        )

    @staticmethod
    def process_new_message(
        message_id: int,
        group_id: int,
        sequence: int,
        is_student_msg: bool = True,
        trigger_type: str = "new_message",
    ):
        """新消息写入成功后调用此方法：
        - 提交监测任务（低优先级）；
        - 调度沉默检查（45秒后）。
        """
        if not DISCUSSION_PIPELINE_V2_ENABLED:
            return

        from agent.monitoring_tasks import process_new_message_task
        from huey_instance import huey

        # 监测任务低优先级，排在主动求助之后
        process_new_message_task.schedule(
            args=(group_id, sequence, trigger_type or "new_message"),
            delay=0,
            priority=50,
        )

        # 沉默检查
        if is_student_msg:
            try:
                from services.collaboration_state_segment_service import CollaborationStateSegmentService
                CollaborationStateSegmentService.close_open_silence_on_student_message(
                    message_id=message_id,
                )
            except Exception as silence_close_exc:
                logger.warning(
                    "Failed to close negative_silence segment for message=%s group=%s seq=%s: %s",
                    message_id,
                    group_id,
                    sequence,
                    silence_close_exc,
                )
                safe_write_audit_log(
                    action_type="collaboration_state_segments.close_failed",
                    actor_type="system",
                    actor_id="pipeline_v2",
                    target_type="message",
                    target_id=message_id,
                    metadata={
                        "group_id": group_id,
                        "session_id": get_active_session_id(),
                        "anchor": sequence,
                        "window_end": sequence,
                        "error_type": silence_close_exc.__class__.__name__,
                    },
                )
            MonitoringService.schedule_silence_check(group_id, sequence)
    @staticmethod
    def _schedule_v2_intervention(
        monitor_run_id: int,
        *,
        state_assessment_id: int = None,
        group_id: int = None,
        session_id: int = None,
        task_id: int = None,
        cutoff_sequence: int = None,
        trigger_source: str = "auto_state",
    ):
        """调度 V2 自动介入任务。"""
        try:
            from agent.intervention_tasks import execute_intervention_v2
            execute_intervention_v2.schedule(
                args=(
                    monitor_run_id,
                    state_assessment_id,
                    group_id,
                    session_id,
                    task_id,
                    cutoff_sequence,
                    trigger_source or "auto_state",
                ),
                delay=0,
                priority=30,
            )
            logger.info("[SSRL_AGENT] Scheduled execute_intervention_v2 for monitor_run %s", monitor_run_id)
            return {
                "enqueued": True,
                "monitor_run_id": monitor_run_id,
                "state_assessment_id": state_assessment_id,
            }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to schedule v2 intervention for monitor_run %s: %s",
                monitor_run_id, exc,
            )
            return {
                "enqueued": False,
                "reason": "strategy_enqueue_failed",
                "error": str(exc),
                "monitor_run_id": monitor_run_id,
            }
