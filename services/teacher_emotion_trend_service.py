# -*- coding: utf-8 -*-
"""
T4 teacher-facing collaboration state timeline service.

This module reads normalized `collaboration_state_segments` only. Detector
snapshots in state_assessments/group_states remain available for audit, but
they are no longer used to infer per-message teacher-page states.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from config import OBSERVATION_MAX_ASSESSMENT_ROUNDS
from db import now_str, query_all, query_one
from services.discussion_window_service import resolve_discussion_window
from services.three_stage_schema import (
    CANONICAL_SUB_STATE_LABELS,
    FINAL_SUB_STATE_CODES,
    LEGACY_STATE_CODES,
    LEGACY_STATE_LABELS,
    FinalMessageStateResolver,
    is_primary_sub_state,
    legacy_state_for_sub_state,
)
from services.message_state_assignment_service import (
    MESSAGE_STATE_ASSIGNMENT_POLICY,
    assign_message_states,
    message_processing_status,
    winning_primary_segment_for_sequence,
)

OBSERVING_STATE = "observing"
UNCLASSIFIED_STATE = "unclassified"
SILENCE_STATE = "negative_silence"

FORMAL_STATE_ORDER = list(FINAL_SUB_STATE_CODES)
DETAILED_STATE_ORDER = list(FINAL_SUB_STATE_CODES)
PROCESS_DISPLAY_STATE_ORDER = [OBSERVING_STATE, UNCLASSIFIED_STATE]
LEGACY_COARSE_STATE_ORDER = list(LEGACY_STATE_CODES)
MESSAGE_RANGE_STATES = {UNCLASSIFIED_STATE} | set(FINAL_SUB_STATE_CODES)

STATE_LABEL_MAP = {
    SILENCE_STATE: "消极沉默区间",
    OBSERVING_STATE: "观察中",
    UNCLASSIFIED_STATE: "未分类",
}
STATE_LABEL_MAP.update(CANONICAL_SUB_STATE_LABELS)

STATE_ALIASES = {
    UNCLASSIFIED_STATE: UNCLASSIFIED_STATE,
    OBSERVING_STATE: OBSERVING_STATE,
    "unknown_sub_state": UNCLASSIFIED_STATE,
    "unknown": UNCLASSIFIED_STATE,
    "insufficient_evidence": UNCLASSIFIED_STATE,
    "participation_imbalance": UNCLASSIFIED_STATE,
    "": OBSERVING_STATE,
}
STATE_ALIASES.update({code: code for code in FINAL_SUB_STATE_CODES})

COARSE_STATE_ALIASES = {
    "positive_collaboration": "positive_collaboration",
    "negative_silence": "negative_silence",
    "conflict_tension": "conflict_tension",
    "blocked_frustration": "blocked_frustration",
    "frustration_stuck": "blocked_frustration",
    "coordination_disorder": "blocked_frustration",
    "task_detached": "task_detached",
    "off_task": "task_detached",
    "unknown": "unknown",
    "insufficient_evidence": "unknown",
    "participation_imbalance": "unknown",
    "conflict_repair": "conflict_tension",
    "positive_recovery": "positive_collaboration",
}

_DISTRIBUTION_KEYS = list(FORMAL_STATE_ORDER) + list(PROCESS_DISPLAY_STATE_ORDER)
CANONICAL_STATE_SOURCE_POLICY = "canonical_segment_merge_v1"
_SOURCE_PRIORITY = {
    "llm": 4,
    "session_finalizer": 3,
    "state_monitor": 3,
    "strategy_llm": 2,
    "rule": 2,
    "silence_rule": 1,
    "legacy": 0,
}


def _normalize_display_state(code, assessment_status=None):
    raw = str(code or "").strip()
    raw_key = raw.lower()
    if str(assessment_status or "").strip().lower() == UNCLASSIFIED_STATE:
        raw_key = UNCLASSIFIED_STATE
    if str(assessment_status or "").strip().lower() == "insufficient_evidence":
        raw_key = "insufficient_evidence"
    state_code = STATE_ALIASES.get(raw_key, OBSERVING_STATE)
    legacy_state_code = raw_key if raw_key and raw_key != state_code else None
    reason = None
    if legacy_state_code:
        reason = "teacher_display_state_normalized"
    elif not raw_key:
        reason = "empty_state_code_normalized_to_observing"
    return {
        "state_code": state_code,
        "state_label": STATE_LABEL_MAP.get(state_code, STATE_LABEL_MAP[OBSERVING_STATE]),
        "legacy_state_code": legacy_state_code,
        "normalization_reason": reason,
    }


def _map_state_code(code):
    """Map a raw or legacy state code into the teacher display state system."""
    return _normalize_display_state(code)["state_code"]


def _map_state_label(code):
    """Return the Chinese label for a teacher display state code."""
    return STATE_LABEL_MAP.get(_map_state_code(code), STATE_LABEL_MAP[OBSERVING_STATE])


def _parse_dt(s):
    if not s:
        return None
    text = str(s).replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _duration_seconds(start_at, end_at):
    start = _parse_dt(start_at)
    end = _parse_dt(end_at) or start
    if not start or not end:
        return 0
    return max(0, int((end - start).total_seconds()))


def _overlaps_range(start_at, end_at, ws, we):
    start = _parse_dt(start_at)
    end = _parse_dt(end_at) or start
    range_start = _parse_dt(ws)
    range_end = _parse_dt(we)
    if not start or not end or not range_start or not range_end:
        return False
    if end < start:
        start, end = end, start
    return start <= range_end and end >= range_start


def _safe_evidence_ids(value):
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    result = []
    for item in parsed:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _session_scope(session_id):
    if not session_id:
        return {"session_id": None, "session_no": None, "task_id": None}
    row = query_one(
        """
        SELECT id, session_no, task_id, session_role, status, start_time, end_time
        FROM experiment_sessions
        WHERE id=?
        """,
        (session_id,),
    )
    if not row:
        return {"session_id": session_id, "session_no": None, "task_id": None}
    return dict(row)


def _resolve_session_scope(
    group_id,
    requested_session_id,
    start_time=None,
    end_time=None,
):
    """Resolve an omitted session to the group's active or most recent session.

    The resolved session controls scope only. It never selects a different
    state-source policy.
    """
    if requested_session_id:
        return {
            "requested_session_id": requested_session_id,
            "resolved_session_id": requested_session_id,
            "scope_mode": "explicit_session",
            "resolved_from": "request",
            "fallback_reason": None,
        }

    active = query_one(
        """
        SELECT d.session_id
        FROM group_session_discussions AS d
        WHERE d.group_id=? AND d.status='running' AND d.session_id IS NOT NULL
        ORDER BY COALESCE(d.updated_at, d.created_at) DESC, d.id DESC
        LIMIT 1
        """,
        (group_id,),
    )
    if active:
        return {
            "requested_session_id": None,
            "resolved_session_id": int(active["session_id"]),
            "scope_mode": "resolved_active_session",
            "resolved_from": "active_discussion",
            "fallback_reason": None,
        }

    time_filters = []
    time_params = []
    if start_time:
        time_filters.append("created_at>=?")
        time_params.append(start_time)
    if end_time:
        time_filters.append("created_at<=?")
        time_params.append(end_time)
    message = query_one(
        """
        SELECT session_id
        FROM messages
        WHERE group_id=? AND session_id IS NOT NULL
          {time_filter}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """.format(
            time_filter=(
                "AND " + " AND ".join(time_filters) if time_filters else ""
            )
        ),
        tuple([group_id] + time_params),
    )
    if message:
        return {
            "requested_session_id": None,
            "resolved_session_id": int(message["session_id"]),
            "scope_mode": "resolved_recent_session",
            "resolved_from": "latest_group_message",
            "fallback_reason": None,
        }

    segment = query_one(
        """
        SELECT session_id
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id IS NOT NULL
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
        LIMIT 1
        """,
        (group_id,),
    )
    if segment:
        return {
            "requested_session_id": None,
            "resolved_session_id": int(segment["session_id"]),
            "scope_mode": "resolved_recent_session",
            "resolved_from": "latest_group_segment",
            "fallback_reason": None,
        }

    discussion = query_one(
        """
        SELECT session_id
        FROM group_session_discussions
        WHERE group_id=? AND session_id IS NOT NULL
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
        LIMIT 1
        """,
        (group_id,),
    )
    if discussion:
        return {
            "requested_session_id": None,
            "resolved_session_id": int(discussion["session_id"]),
            "scope_mode": "resolved_recent_session",
            "resolved_from": "latest_discussion",
            "fallback_reason": None,
        }

    return {
        "requested_session_id": None,
        "resolved_session_id": None,
        "scope_mode": "group_time_range",
        "resolved_from": "group_time_range",
        "fallback_reason": "no_session_scoped_group_data",
    }


def _normalize_coarse_state(code):
    raw = str(code or "").strip().lower()
    coarse_code = COARSE_STATE_ALIASES.get(raw)
    return {
        "state_code": coarse_code,
        "state_label": LEGACY_STATE_LABELS.get(coarse_code),
        "raw_state_code": raw or None,
    }


def _resolve_time_range(session_id, start_time, end_time, group_id=None):
    """Determine (window_start, window_end, legacy_warning) from inputs."""
    legacy_warning = False
    now = datetime.now()

    if start_time and end_time:
        ws = _parse_dt(start_time)
        we = _parse_dt(end_time)
        return (
            _fmt_dt(ws) if ws else start_time,
            _fmt_dt(we) if we else end_time,
            legacy_warning,
        )

    if session_id and group_id:
        return resolve_discussion_window(
            group_id=group_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
        )

    if session_id:
        sess = query_one(
            "SELECT start_time FROM experiment_sessions WHERE id=?",
            (session_id,),
        )
        if sess and sess["start_time"]:
            ws = _parse_dt(sess["start_time"])
            if ws:
                return (_fmt_dt(ws), _fmt_dt(now), True)
        return (_fmt_dt(now - timedelta(hours=1)), _fmt_dt(now), True)

    return (_fmt_dt(now - timedelta(hours=1)), _fmt_dt(now), legacy_warning)


def _session_filter_sql(alias, session_id):
    if not session_id:
        return "", ()
    prefix = f"{alias}." if alias else ""
    return (
        f"AND ({prefix}session_id=? OR ({prefix}session_id IS NULL AND (? IS NULL OR {prefix}session_no=?)))",
        None,
    )


def _student_messages_by_sequence(
    group_id, session_id, scope, ws, we, *, include_legacy_scope=False
):
    params = [group_id]
    where = [
        "group_id=?",
        "sequence IS NOT NULL",
        "(COALESCE(role, '')='student' OR COALESCE(sender_type, '')='student')",
    ]
    if session_id:
        if include_legacy_scope:
            where.append(
                "(session_id=? OR (session_id IS NULL AND (? IS NULL OR session_no=?)))"
            )
            params.extend(
                [session_id, scope.get("session_no"), scope.get("session_no")]
            )
        else:
            where.append("session_id=?")
            params.append(session_id)
    where.extend(["created_at>=?", "created_at<=?"])
    params.extend([ws, we])
    rows = query_all(
        f"""
        SELECT id, sequence, created_at, user_id, session_id, session_no, task_id
        FROM messages
        WHERE {' AND '.join(where)}
        ORDER BY sequence ASC, id ASC
        """,
        tuple(params),
    )
    result = {}
    for row in rows:
        if row["sequence"] is not None:
            result[int(row["sequence"])] = dict(row)
    return result


def _read_segment_rows(
    group_id, session_id, scope, *, include_legacy_scope=False
):
    params = [group_id]
    where = ["s.group_id=?"]
    if session_id:
        if include_legacy_scope:
            where.append(
                "(s.session_id=? OR (s.session_id IS NULL AND (? IS NULL OR s.session_no=?)))"
            )
            params.extend(
                [session_id, scope.get("session_no"), scope.get("session_no")]
            )
        else:
            where.append("s.session_id=?")
            params.append(session_id)
    rows = query_all(
        f"""
        SELECT s.*,
               b.status AS batch_status,
               b.terminal_status AS batch_terminal_status,
               b.completed_at AS batch_completed_at,
               b.discussion_id AS batch_discussion_id
        FROM collaboration_state_segments AS s
        LEFT JOIN state_assessment_batches AS b
          ON b.id=s.assessment_batch_id
        WHERE {' AND '.join(where)}
        ORDER BY
            COALESCE(s.start_sequence, s.start_message_id, s.previous_student_message_id, 0),
            COALESCE(s.start_at, s.created_at),
            s.is_finalized DESC,
            s.id ASC
        """,
        tuple(params),
    )
    return [dict(row) for row in rows]


def _assessment_display_context(group_id, session_id, student_index):
    context = {
        "discussion_id": None,
        "state_source_mode": "canonical",
        "state_source_policy": CANONICAL_STATE_SOURCE_POLICY,
        "processing_mode": "awaiting_detection",
        "has_assessment_pipeline": False,
        "batch_windows": [],
        "last_finalized_student_sequence": None,
        "last_scheduled_student_sequence": None,
        "observation_status": "inactive",
        "observation_started_sequence": None,
        "last_intervention_sequence": None,
        "observation_expired_through_sequence": None,
        "observation_assessment_rounds": 0,
        "observation_max_assessment_rounds": OBSERVATION_MAX_ASSESSMENT_ROUNDS,
    }
    if not session_id:
        return context
    discussion = query_one(
        """
        SELECT id, status
        FROM group_session_discussions
        WHERE group_id=? AND session_id=?
        ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (group_id, session_id),
    )
    if not discussion:
        return context
    discussion_id = int(discussion["id"])
    context["discussion_id"] = discussion_id
    cursor = query_one(
        """
        SELECT *
        FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (group_id, session_id, discussion_id),
    )
    batches = [
        dict(row)
        for row in query_all(
            """
            SELECT id, status, terminal_status, error_code, fallback_action,
                   fallback_segment_count,
                   candidate_start_sequence, candidate_end_sequence,
                   completed_at, created_at
            FROM state_assessment_batches
            WHERE group_id=? AND session_id=? AND discussion_id=?
            ORDER BY COALESCE(completed_at, created_at), id
            """,
            (group_id, session_id, discussion_id),
        )
    ]
    context["has_assessment_pipeline"] = bool(cursor or batches)
    if context["has_assessment_pipeline"]:
        context["processing_mode"] = "assessment_pipeline"
    context["batch_windows"] = [
        {
            "batch_id": batch.get("id"),
            "status": batch.get("status"),
            "terminal_status": batch.get("terminal_status"),
            "error_code": batch.get("error_code"),
            "fallback_action": batch.get("fallback_action"),
            "fallback_segment_count": batch.get("fallback_segment_count"),
            "start_sequence": batch.get("candidate_start_sequence"),
            "end_sequence": batch.get("candidate_end_sequence"),
        }
        for batch in batches
    ]
    if not cursor:
        return context

    cursor_data = dict(cursor)
    context["last_finalized_student_sequence"] = cursor_data.get(
        "last_finalized_student_sequence"
    )
    context["last_scheduled_student_sequence"] = cursor_data.get(
        "last_scheduled_student_sequence"
    )
    context["observation_status"] = cursor_data.get("observation_status") or "inactive"
    context["observation_started_sequence"] = cursor_data.get("observation_started_sequence")
    context["last_intervention_sequence"] = cursor_data.get("last_intervention_sequence")
    context["observation_updated_at"] = cursor_data.get("updated_at")
    if context["observation_status"] != "observing":
        return context

    start_sequence = context["observation_started_sequence"]
    if start_sequence is None and context["last_intervention_sequence"] is not None:
        later = [
            sequence
            for sequence in student_index
            if sequence > int(context["last_intervention_sequence"])
        ]
        if later:
            start_sequence = min(later)
            context["observation_started_sequence"] = start_sequence

    if start_sequence is None:
        return context
    completed_rounds = [
        batch
        for batch in batches
        if batch.get("status") == "succeeded"
        and batch.get("candidate_end_sequence") is not None
        and int(batch["candidate_end_sequence"]) >= int(start_sequence)
    ]
    context["observation_assessment_rounds"] = len(completed_rounds)
    if len(completed_rounds) >= OBSERVATION_MAX_ASSESSMENT_ROUNDS:
        context["observation_expired_through_sequence"] = int(
            completed_rounds[OBSERVATION_MAX_ASSESSMENT_ROUNDS - 1][
                "candidate_end_sequence"
            ]
        )
    return context


def _segment_assignment_key(segment):
    successful_llm = bool(
        segment.get("assessment_batch_id") is not None
        and segment.get("source") == "llm"
        and segment.get("assessment_status") == "confirmed"
        and segment.get("batch_status") == "succeeded"
    )
    return (
        1 if successful_llm else 0,
        _SOURCE_PRIORITY.get(segment.get("source"), 0),
        1 if segment.get("is_finalized") else 0,
        str(segment.get("batch_completed_at") or segment.get("updated_at") or ""),
        int(segment.get("assessment_batch_id") or 0),
        float(segment.get("confidence") or 0.0),
        int(segment.get("id") or 0),
    )


def _message_segment_policy(row):
    """Return whether a persisted message-range segment is canonical."""
    source = str(row.get("source") or "legacy").strip().lower()
    status = str(row.get("assessment_status") or "").strip().lower()
    batch_id = row.get("assessment_batch_id")

    if source == "llm":
        if (
            batch_id is not None
            and status == "confirmed"
            and row.get("batch_status") == "succeeded"
        ):
            return True, None
        if (
            batch_id is not None
            and status == UNCLASSIFIED_STATE
            and row.get("fallback_reason") == "batch_unclassified"
            and row.get("batch_terminal_status") in ("degraded", "quarantined")
        ):
            return True, None
        return False, "llm_batch_not_successfully_confirmed"
    if batch_id is not None:
        if (
            source == "rule"
            and status == "confirmed"
            and row.get("fallback_reason") == "batch_retry_exhausted"
            and row.get("batch_terminal_status") in ("degraded", "quarantined")
        ):
            return True, None
        return False, "batch_bound_segment_not_confirmed_fallback"

    if status not in ("", "confirmed"):
        return False, "persisted_segment_not_confirmed"
    if source not in _SOURCE_PRIORITY:
        return False, "unsupported_segment_source"
    return True, None


def _build_segments(
    group_id, session_id, ws, we, scope, *, include_legacy_scope=False
):
    warnings = []
    excluded_reasons = {}
    student_index = _student_messages_by_sequence(
        group_id,
        session_id,
        scope,
        ws,
        we,
        include_legacy_scope=include_legacy_scope,
    )
    display_context = _assessment_display_context(group_id, session_id, student_index)
    state_segments = []
    silence_segments = []

    def exclude(row, reason):
        excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
        warnings.append({
            "type": "excluded_state_segment",
            "segment_id": row.get("id"),
            "reason": reason,
        })

    for row in _read_segment_rows(
        group_id,
        session_id,
        scope,
        include_legacy_scope=include_legacy_scope,
    ):
        coarse_normalized = _normalize_coarse_state(
            row.get("coarse_state_code") or row.get("state_code")
        )
        raw_canonical_sub_state = str(
            row.get("canonical_sub_state_code") or ""
        ).strip()
        process_sub_state_code = (
            "unknown_sub_state"
            if raw_canonical_sub_state == "unknown_sub_state"
            else None
        )
        canonical_sub_state = raw_canonical_sub_state
        final_contract = FinalMessageStateResolver.resolve(
            canonical_sub_state_code=canonical_sub_state,
            raw_sub_state_code=row.get("raw_sub_state_code"),
            secondary_tags_json=row.get("secondary_tags_json"),
            secondary_sub_state_tags_json=row.get("secondary_sub_state_tags_json"),
            coarse_state_code=row.get("coarse_state_code"),
            state_code=row.get("state_code"),
            assessment_status=row.get("assessment_status") or "confirmed",
            confidence=row.get("confidence"),
            segment_id=row.get("id"),
            assessment_batch_id=row.get("assessment_batch_id"),
            strategy_pipeline_run_id=row.get("strategy_pipeline_run_id"),
            selected_strategy_id=row.get("selected_strategy_id"),
        )
        if canonical_sub_state not in FINAL_SUB_STATE_CODES:
            canonical_sub_state = None
        assessment_status = str(
            row.get("assessment_status") or "confirmed"
        ).strip().lower()
        display_code = (
            canonical_sub_state
            if canonical_sub_state
            else UNCLASSIFIED_STATE
        )
        if assessment_status == UNCLASSIFIED_STATE:
            display_code = UNCLASSIFIED_STATE
        normalized = _normalize_display_state(display_code, assessment_status)
        code = normalized["state_code"]
        kind = row.get("segment_kind")
        if kind == "message_range":
            accepted, exclusion_reason = _message_segment_policy(row)
            if not accepted:
                exclude(row, exclusion_reason)
                continue
            if code not in MESSAGE_RANGE_STATES:
                exclude(row, "non_display_message_state")
                continue
            try:
                start_seq = int(
                    row.get("start_sequence")
                    if row.get("start_sequence") is not None
                    else row.get("start_message_id")
                )
                end_seq = int(
                    row.get("end_sequence")
                    if row.get("end_sequence") is not None
                    else row.get("end_message_id")
                )
            except (TypeError, ValueError):
                exclude(row, "invalid_message_range")
                continue
            if end_seq < start_seq:
                exclude(row, "invalid_message_range")
                continue
            covered = [
                message
                for sequence, message in sorted(student_index.items())
                if start_seq <= sequence <= end_seq
            ]
            if not covered:
                continue
            start_at = covered[0].get("created_at")
            end_at = covered[-1].get("created_at") or start_at
            if not _overlaps_range(start_at, end_at, ws, we):
                continue
            item = {
                "id": row.get("id"),
                "state_code": code,
                "state_label": normalized["state_label"],
                "final_sub_state_code": final_contract["final_sub_state_code"],
                "final_sub_state_label": final_contract["final_sub_state_label"],
                "canonical_sub_state_code": canonical_sub_state,
                "process_sub_state_code": process_sub_state_code,
                "raw_sub_state_code": row.get("raw_sub_state_code"),
                "coarse_state_code": (
                    final_contract.get("coarse_state_code")
                    or coarse_normalized["state_code"]
                ),
                "legacy_state_code": (
                    final_contract.get("legacy_state_code")
                    or coarse_normalized["state_code"]
                ),
                "state_overlays": final_contract["state_overlays"],
                "assignment_source": (
                    "batch_unclassified"
                    if row.get("fallback_reason") == "batch_unclassified"
                    else "legacy_monitor_only"
                    if not canonical_sub_state
                    and str(row.get("source") or "").strip().lower()
                    in {"state_monitor", "legacy"}
                    else final_contract["assignment_source"]
                ),
                "inferred": final_contract["inferred"],
                "normalization_reason": normalized["normalization_reason"],
                "segment_kind": "message_range",
                "discussion_id": row.get("discussion_id"),
                "start_sequence": start_seq,
                "end_sequence": end_seq,
                # Compatibility aliases.  Both values are messages.sequence,
                # never messages.id primary keys.
                "start_message_id": start_seq,
                "end_message_id": end_seq,
                "start_at": start_at,
                "end_at": end_at,
                "evidence_sequences": _safe_evidence_ids(
                    row.get("evidence_sequences")
                    or row.get("evidence_message_ids_json")
                ),
                "evidence_message_ids": _safe_evidence_ids(
                    row.get("evidence_sequences")
                    or row.get("evidence_message_ids_json")
                ),
                "confidence": row.get("confidence"),
                "source": row.get("source") or "legacy",
                "segment_source": (
                    "degraded_rule"
                    if row.get("fallback_reason") == "batch_retry_exhausted"
                    else "batch_unclassified"
                    if row.get("fallback_reason") == "batch_unclassified"
                    else row.get("source") or "legacy"
                ),
                "raw_source": row.get("source"),
                "fallback_reason": row.get("fallback_reason"),
                "assessment_status": (
                    assessment_status
                    if canonical_sub_state
                    or assessment_status == UNCLASSIFIED_STATE
                    else UNCLASSIFIED_STATE
                ),
                "assessment_batch_id": row.get("assessment_batch_id"),
                "strategy_pipeline_run_id": row.get(
                    "strategy_pipeline_run_id"
                ),
                "should_intervene": row.get("should_intervene"),
                "selected_strategy_id": row.get("selected_strategy_id"),
                "state_assessment_id": row.get("assessment_id"),
                "batch_status": row.get("batch_status"),
                "batch_terminal_status": row.get("batch_terminal_status"),
                "batch_completed_at": row.get("batch_completed_at"),
                "is_active_at_batch_end": bool(row.get("is_active_at_batch_end")),
                "source_run_id": row.get("source_run_id"),
                "assessment_id": row.get("assessment_id"),
                "prompt_version": row.get("prompt_version"),
                "is_finalized": bool(row.get("is_finalized")),
                "message_count": len(covered),
                "duration_seconds": _duration_seconds(start_at, end_at),
                "analysis_anchor_message_id": row.get("analysis_anchor_message_id"),
                "analysis_window_start_message_id": row.get("analysis_window_start_message_id"),
                "analysis_window_end_message_id": row.get("analysis_window_end_message_id"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "canonical_segment_id": row.get("id"),
            }
            item["_assignment_key"] = _segment_assignment_key(item)
            state_segments.append(item)
            continue

        if (
            kind == "time_range"
            and coarse_normalized["state_code"] == SILENCE_STATE
        ):
            start_at = row.get("start_at")
            end_at = row.get("end_at")
            observed_end_at = end_at or row.get("last_observed_at")
            if not _overlaps_range(start_at, observed_end_at, ws, we):
                continue
            duration = int(row.get("gap_seconds") or 0) or _duration_seconds(
                start_at,
                observed_end_at,
            )
            silence_segments.append({
                "id": row.get("id"),
                "state_code": SILENCE_STATE,
                "state_label": STATE_LABEL_MAP[SILENCE_STATE],
                "segment_kind": "time_range",
                "discussion_id": row.get("discussion_id"),
                "start_at": start_at,
                "end_at": end_at,
                "trigger_sequence": row.get("trigger_sequence"),
                "raw_silence_started_at": row.get("raw_silence_started_at"),
                "threshold_reached_at": row.get("threshold_reached_at"),
                "detected_at": row.get("detected_at"),
                "last_observed_at": row.get("last_observed_at"),
                "silent_seconds_at_detection": row.get(
                    "silent_seconds_at_detection"
                ),
                "previous_student_message_id": row.get("previous_student_message_id"),
                "next_student_message_id": row.get("next_student_message_id"),
                "end_sequence": row.get("end_sequence"),
                "gap_seconds": duration,
                "duration_seconds": duration,
                "source": row.get("source"),
                "segment_source": row.get("source"),
                "source_run_id": row.get("source_run_id"),
                "state_assessment_id": row.get("assessment_id"),
                "is_active": bool(row.get("is_active")),
                "is_finalized": bool(row.get("is_finalized")),
                "resolution_reason": row.get("resolution_reason"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "canonical_segment_id": row.get("id"),
            })

    state_segments.sort(key=lambda item: (item["start_message_id"], item["end_message_id"], item["id"] or 0))
    silence_segments.sort(key=lambda item: (item.get("start_at") or "", item.get("id") or 0))
    for item in state_segments:
        item.pop("_assignment_key", None)
    display_context["excluded_segment_count"] = sum(excluded_reasons.values())
    display_context["excluded_segment_reasons"] = excluded_reasons
    warnings.extend(_overlap_warnings(state_segments))
    return state_segments, silence_segments, warnings, display_context, student_index


def _overlap_warnings(state_segments):
    warnings = []
    ordered = sorted(state_segments, key=lambda s: (s["start_message_id"], s["end_message_id"], s["id"] or 0))
    previous = None
    for current in ordered:
        if previous and current["start_message_id"] <= previous["end_message_id"]:
            warnings.append({
                "type": "overlapping_state_segments",
                "message": "重叠状态片段已按新 schema、成功批次时间、confidence、id 的固定优先级解析。",
                "previous_segment_id": previous.get("id"),
                "current_segment_id": current.get("id"),
                "previous_range": [previous["start_message_id"], previous["end_message_id"]],
                "current_range": [current["start_message_id"], current["end_message_id"]],
            })
        if previous is None or current["end_message_id"] > previous["end_message_id"]:
            previous = current
    return warnings


def _winning_segment_for_sequence(state_segments, sequence):
    return winning_primary_segment_for_sequence(state_segments or [], sequence)


def _message_processing_status(sequence, state_segments, display_context):
    return message_processing_status(
        sequence,
        state_segments or [],
        display_context or {},
    )


def _current_group_state(
    state_segments,
    silence_segments,
    student_index,
    display_context,
    assignments_by_sequence=None,
):
    if (
        display_context.get("observation_status") == "observing"
        and display_context.get("observation_started_sequence") is None
    ):
        return {
            "semantic_state": None,
            "final_sub_state_code": None,
            "assessment_status": "observing",
            "state_label": STATE_LABEL_MAP[OBSERVING_STATE],
            "confidence": None,
            "updated_at": display_context.get("observation_updated_at"),
            "source": "post_intervention_observation",
            "current_state_source": "post_intervention_observation",
            "current_state_reason": "waiting_for_first_post_intervention_message",
            "segment_id": None,
        }

    if not student_index:
        return {
            "semantic_state": None,
            "final_sub_state_code": None,
            "assessment_status": "observing",
            "state_label": STATE_LABEL_MAP[OBSERVING_STATE],
            "confidence": None,
            "updated_at": None,
            "source": "no_student_message",
            "current_state_source": "no_student_message",
            "current_state_reason": "awaiting_first_student_message",
            "segment_id": None,
        }
    latest_sequence = max(student_index)
    assignment = (
        assignments_by_sequence.get(latest_sequence)
        if assignments_by_sequence
        else None
    )
    if assignment:
        segment = assignment.get("segment")
        assessment_status = assignment.get("assessment_status")
        final_code = assignment.get("final_sub_state_code")
        return {
            "semantic_state": final_code,
            "final_sub_state_code": final_code,
            "final_sub_state_label": assignment.get("final_sub_state_label"),
            "display_state_code": assignment.get("display_state_code"),
            "display_state_label": assignment.get("display_state_label"),
            "state_code": assignment.get("display_state_code"),
            "assessment_status": assessment_status,
            "state_label": assignment.get("display_state_label"),
            "confidence": assignment.get("confidence"),
            "coarse_state_code": assignment.get("coarse_state_code"),
            "legacy_state_code": assignment.get("legacy_state_code"),
            "updated_at": (
                segment.get("batch_completed_at")
                or segment.get("updated_at")
                or segment.get("end_at")
                if segment
                else display_context.get("observation_updated_at")
            ),
            "source": assignment.get("assignment_source"),
            "assignment_source": assignment.get("assignment_source"),
            "inferred": bool(assignment.get("inferred")),
            "current_state_source": assignment.get("assignment_source"),
            "current_state_reason": assignment.get("state_assignment_reason"),
            "segment_id": assignment.get("source_segment_id"),
            "source_batch_id": assignment.get("source_batch_id"),
            "error_code": assignment.get("error_code"),
            "sequence": latest_sequence,
        }

    status = _message_processing_status(latest_sequence, state_segments, display_context)
    segment = status.get("segment")
    assessment_status = status["assessment_status"]
    return {
        "semantic_state": status.get("semantic_state"),
        "final_sub_state_code": status.get("semantic_state"),
        "final_sub_state_label": (
            segment.get("final_sub_state_label") if segment else None
        ),
        "state_code": status.get("semantic_state") or assessment_status,
        "assessment_status": assessment_status,
        "state_label": (
            segment.get("state_label")
            if segment
            else STATE_LABEL_MAP[
                OBSERVING_STATE
                if assessment_status == "observing"
                else UNCLASSIFIED_STATE
            ]
        ),
        "confidence": segment.get("confidence") if segment else None,
        "coarse_state_code": (
            segment.get("coarse_state_code") if segment else None
        ),
        "legacy_state_code": (
            segment.get("legacy_state_code") if segment else None
        ),
        "updated_at": (
            segment.get("batch_completed_at")
            or segment.get("updated_at")
            or segment.get("end_at")
            if segment
            else display_context.get("observation_updated_at")
        ),
        "source": segment.get("source") if segment else assessment_status,
        "current_state_source": (
            segment.get("source") if segment else assessment_status
        ),
        "current_state_reason": (
            "latest_student_message_covered_by_canonical_segment"
            if segment
            else status.get("state_assignment_reason")
        ),
        "segment_id": segment.get("id") if segment else None,
        "sequence": latest_sequence,
    }


def _active_silence_context(silence_segments):
    active = [
        segment for segment in silence_segments or [] if bool(segment.get("is_active"))
    ]
    if not active:
        return {
            "active": False,
            "segment_id": None,
            "started_at": None,
            "last_observed_at": None,
            "duration_seconds": 0,
        }
    latest = max(
        active,
        key=lambda segment: (
            segment.get("last_observed_at") or segment.get("start_at") or "",
            int(segment.get("id") or 0),
        ),
    )
    return {
        "active": True,
        "segment_id": latest.get("id"),
        "started_at": latest.get("start_at"),
        "last_observed_at": latest.get("last_observed_at"),
        "duration_seconds": int(
            latest.get("duration_seconds") or latest.get("gap_seconds") or 0
        ),
        "source": latest.get("source") or "silence_rule",
        "display_label": STATE_LABEL_MAP[SILENCE_STATE],
    }


def _snapshots_from_segments(state_segments, silence_segments):
    snapshots = []
    for segment in state_segments:
        snapshots.append({
            "id": segment.get("id"),
            "segment_id": segment.get("id"),
            "segment_kind": "message_range",
            "created_at": segment.get("created_at"),
            "window_start": segment.get("start_at"),
            "window_end": segment.get("end_at"),
            "collaboration_state": segment.get("state_code"),
            "state_label": segment.get("state_label"),
            "final_sub_state_code": segment.get("final_sub_state_code"),
            "final_sub_state_label": segment.get("final_sub_state_label"),
            "coarse_state_code": segment.get("coarse_state_code"),
            "legacy_state_code": segment.get("legacy_state_code"),
            "state_overlays": segment.get("state_overlays") or [],
            "assignment_source": segment.get("assignment_source"),
            "inferred": bool(segment.get("inferred")),
            "normalization_reason": segment.get("normalization_reason"),
            "confidence": segment.get("confidence"),
            "message_count": segment.get("message_count") or 0,
            "active_member_count": 0,
            "source": segment.get("source"),
            "segment_source": segment.get("segment_source"),
            "raw_source": segment.get("raw_source"),
            "assessment_status": segment.get("assessment_status") or "confirmed",
            "assessment_batch_id": segment.get("assessment_batch_id"),
            "state_assessment_id": segment.get("state_assessment_id"),
            "canonical_segment_id": segment.get("canonical_segment_id"),
            "duplicate_warning": False,
            "first_sequence": segment.get("start_sequence"),
            "last_sequence": segment.get("end_sequence"),
            "evidence_sequences": segment.get("evidence_sequences") or [],
            "evidence_message_ids": segment.get("evidence_sequences") or [],
            "is_finalized": bool(segment.get("is_finalized")),
            "duration_seconds": segment.get("duration_seconds") or 0,
        })
    for segment in silence_segments:
        snapshots.append({
            "id": segment.get("id"),
            "segment_id": segment.get("id"),
            "segment_kind": "time_range",
            "created_at": segment.get("created_at"),
            "window_start": segment.get("start_at"),
            "window_end": (
                segment.get("end_at") or segment.get("last_observed_at")
            ),
            "collaboration_state": "negative_silence",
            "state_label": STATE_LABEL_MAP["negative_silence"],
            "final_sub_state_code": None,
            "final_sub_state_label": None,
            "coarse_state_code": "negative_silence",
            "legacy_state_code": None,
            "state_overlays": [],
            "assignment_source": "silence_time_range",
            "inferred": False,
            "normalization_reason": None,
            "confidence": None,
            "message_count": 0,
            "active_member_count": 0,
            "source": segment.get("source"),
            "segment_source": segment.get("segment_source"),
            "state_assessment_id": segment.get("state_assessment_id"),
            "canonical_segment_id": segment.get("canonical_segment_id"),
            "duplicate_warning": False,
            "first_sequence": segment.get("previous_student_message_id"),
            "last_sequence": segment.get("next_student_message_id"),
            "evidence_message_ids": [],
            "is_active": bool(segment.get("is_active")),
            "is_finalized": bool(segment.get("is_finalized")),
            "duration_seconds": segment.get("duration_seconds") or 0,
        })
    snapshots.sort(key=lambda item: (item.get("window_start") or "", item.get("segment_id") or 0))
    return snapshots


def _compute_distribution_from_segments(
    state_segments,
    silence_segments,
    student_index=None,
):
    dist = {
        code: {
            "state_code": code,
            "state_label": STATE_LABEL_MAP[code],
            "segment_count": 0,
            "message_count": 0,
            "duration_seconds": 0,
        }
        for code in _DISTRIBUTION_KEYS
    }
    for segment in state_segments:
        code = _map_state_code(segment.get("state_code"))
        if code not in dist:
            continue
        dist[code]["segment_count"] += 1
        dist[code]["duration_seconds"] += int(segment.get("duration_seconds") or 0)
    if student_index is None:
        for segment in state_segments:
            code = _map_state_code(segment.get("state_code"))
            if code not in dist:
                continue
            dist[code]["message_count"] += int(segment.get("message_count") or 0)
    else:
        for sequence in student_index:
            winner = _winning_segment_for_sequence(state_segments, sequence)
            if not winner:
                continue
            code = _map_state_code(winner.get("state_code"))
            if code in dist:
                dist[code]["message_count"] += 1
    return dist


def _compute_distribution_from_assignments(assignments, silence_segments):
    dist = {
        code: {
            "state_code": code,
            "state_label": STATE_LABEL_MAP[code],
            "segment_count": 0,
            "message_count": 0,
            "duration_seconds": 0,
        }
        for code in _DISTRIBUTION_KEYS
    }
    counted_segments = set()
    for assignment in assignments or []:
        if assignment.get("role") != "student":
            continue
        assessment_status = assignment.get("assessment_status")
        final_code = (
            assignment.get("final_sub_state_code")
            if assessment_status == "confirmed"
            else None
        )
        code = (
            final_code
            if is_primary_sub_state(final_code)
            else (
                OBSERVING_STATE
                if assessment_status == OBSERVING_STATE
                else UNCLASSIFIED_STATE
            )
        )
        if code not in dist:
            continue
        dist[code]["message_count"] += 1
        segment_id = assignment.get("source_segment_id")
        if segment_id is not None and segment_id not in counted_segments:
            dist[code]["segment_count"] += 1
            counted_segments.add(segment_id)
    return dist


def _compute_coarse_distribution_from_assignments(assignments):
    """Return an explicitly debug-only Stage 1 projection."""

    dist = {
        code: {
            "state_code": code,
            "state_label": LEGACY_STATE_LABELS[code],
            "segment_count": 0,
            "message_count": 0,
            "duration_seconds": 0,
            "debug_only": True,
        }
        for code in LEGACY_COARSE_STATE_ORDER
    }
    counted_segments = set()
    for assignment in assignments or []:
        if assignment.get("role") != "student":
            continue
        coarse = assignment.get("coarse_state_code")
        if coarse not in dist:
            final_code = assignment.get("final_sub_state_code")
            coarse = (
                legacy_state_for_sub_state(final_code)
                if is_primary_sub_state(final_code)
                else "unknown"
            )
        dist[coarse]["message_count"] += 1
        segment_id = assignment.get("source_segment_id")
        segment_key = (coarse, segment_id)
        if segment_id is not None and segment_key not in counted_segments:
            dist[coarse]["segment_count"] += 1
            counted_segments.add(segment_key)
    return dist


def _compute_detailed_distribution_from_segments(
    state_segments,
    silence_segments,
    student_index=None,
):
    dist = {
        code: {
            "state_code": code,
            "state_label": STATE_LABEL_MAP[code],
            "segment_count": 0,
            "message_count": 0,
            "duration_seconds": 0,
        }
        for code in DETAILED_STATE_ORDER
    }
    for segment in state_segments:
        code = _map_state_code(segment.get("state_code"))
        if code not in dist or code == "negative_silence":
            continue
        dist[code]["segment_count"] += 1
        dist[code]["duration_seconds"] += int(segment.get("duration_seconds") or 0)
    if student_index is None:
        for segment in state_segments:
            code = _map_state_code(segment.get("state_code"))
            if code in dist and code != "negative_silence":
                dist[code]["message_count"] += int(segment.get("message_count") or 0)
    else:
        for sequence in student_index:
            winner = _winning_segment_for_sequence(state_segments, sequence)
            if not winner:
                continue
            code = _map_state_code(winner.get("state_code"))
            if code in dist and code != "negative_silence":
                dist[code]["message_count"] += 1
    return dist


def _compute_detailed_distribution_from_assignments(assignments, silence_segments):
    return _compute_distribution_from_assignments(assignments, silence_segments)


def _compute_distribution(snapshots):
    """Compatibility helper: summarize formal states from segment-like snapshots."""
    state_segments = [
        {
            "state_code": snap.get("collaboration_state"),
            "message_count": snap.get("message_count") or 0,
            "duration_seconds": snap.get("duration_seconds") or 0,
        }
        for snap in snapshots or []
        if snap.get("segment_kind") == "message_range"
    ]
    silence_segments = [
        {
            "duration_seconds": snap.get("duration_seconds") or 0,
        }
        for snap in snapshots or []
        if snap.get("segment_kind") == "time_range"
        and _map_state_code(snap.get("collaboration_state")) == "negative_silence"
    ]
    return _compute_distribution_from_segments(state_segments, silence_segments)


def _compute_transitions(snapshots):
    transitions = []
    previous = None
    for snap in sorted(snapshots or [], key=lambda s: s.get("window_start") or ""):
        current = _map_state_code(snap.get("collaboration_state"))
        if current == OBSERVING_STATE:
            continue
        if previous and previous["state"] != current:
            transitions.append({
                "at": snap.get("window_start"),
                "from_state": previous["state"],
                "from_label": _map_state_label(previous["state"]),
                "to_state": current,
                "to_label": _map_state_label(current),
                "confidence": snap.get("confidence"),
            })
        previous = {"state": current, "snapshot": snap}
    return transitions


def _latest_explicit_state(state_segments, silence_segments):
    candidates = []
    for segment in state_segments:
        candidates.append((segment.get("end_at") or segment.get("start_at"), segment))
    for segment in silence_segments:
        candidates.append(
            (
                segment.get("end_at")
                or segment.get("last_observed_at")
                or segment.get("start_at"),
                segment,
            )
        )
    candidates = [(when, segment) for when, segment in candidates if _parse_dt(when)]
    if not candidates:
        return {
            "state_code": OBSERVING_STATE,
            "state_label": STATE_LABEL_MAP[OBSERVING_STATE],
            "confidence": None,
            "at": None,
            "source": "observing_default",
        }
    when, latest = max(candidates, key=lambda item: _parse_dt(item[0]))
    return {
        "state_code": latest.get("state_code"),
        "state_label": latest.get("state_label"),
        "confidence": latest.get("confidence"),
        "at": when,
        "source": "latest_state_segment",
        "segment_id": latest.get("id"),
    }


def _state_system(*, include_unclassified=True):
    states = [
        {
            "code": code,
            "label": STATE_LABEL_MAP[code],
            "is_formal": True,
            "is_primary": True,
            "is_process": False,
            "is_legacy": False,
        }
        for code in FORMAL_STATE_ORDER
    ] + [
        {
            "code": OBSERVING_STATE,
            "label": STATE_LABEL_MAP[OBSERVING_STATE],
            "is_formal": False,
            "is_primary": False,
            "is_process": True,
            "is_legacy": False,
        },
    ]
    if include_unclassified:
        states.append({
            "code": UNCLASSIFIED_STATE,
            "label": STATE_LABEL_MAP[UNCLASSIFIED_STATE],
            "is_formal": False,
            "is_primary": False,
            "is_process": True,
            "is_legacy": False,
        })
    return states


def _detailed_state_system(*, include_unclassified=True):
    states = _state_system(include_unclassified=include_unclassified)
    for item in states:
        item["is_detailed"] = bool(item.get("is_primary"))
    return states


def _coarse_state_system():
    return [
        {
            "code": code,
            "label": LEGACY_STATE_LABELS[code],
            "is_formal": False,
            "is_primary": False,
            "is_process": False,
            "is_legacy": True,
            "debug_only": True,
        }
        for code in LEGACY_COARSE_STATE_ORDER
    ]


def get_emotion_trend(
    group_id,
    session_id=None,
    start_time=None,
    end_time=None,
    window_minutes=3,
    include_legacy_scope=False,
):
    """Return normalized collaboration state segment trend data for a group."""
    group = query_one("SELECT id, name, group_code FROM groups WHERE id=?", (group_id,))
    if not group:
        return {"error": "Group %d not found" % group_id}

    scope_resolution = _resolve_session_scope(
        group_id,
        session_id,
        start_time=start_time,
        end_time=end_time,
    )
    resolved_session_id = scope_resolution.get("resolved_session_id")
    ws, we, legacy_warning = _resolve_time_range(
        resolved_session_id,
        start_time,
        end_time,
        group_id=group_id,
    )
    scope = _session_scope(resolved_session_id)
    (
        state_segments,
        silence_segments,
        warnings,
        display_context,
        student_index,
    ) = _build_segments(
        group_id,
        resolved_session_id,
        ws,
        we,
        scope,
        include_legacy_scope=include_legacy_scope,
    )
    assignment_payload = assign_message_states(
        messages=list(student_index.values()),
        state_segments=state_segments,
        display_context=display_context,
        silence_segments=silence_segments,
        group_id=group_id,
        session_id=resolved_session_id,
        discussion_id=display_context.get("discussion_id"),
    )
    display_context["message_assignment_policy"] = MESSAGE_STATE_ASSIGNMENT_POLICY
    display_context["message_assignment_summary"] = assignment_payload["summary"]
    snapshots = _snapshots_from_segments(state_segments, silence_segments)
    distribution = _compute_distribution_from_assignments(
        assignment_payload["assignments"],
        silence_segments,
    )
    detailed_distribution = _compute_detailed_distribution_from_assignments(
        assignment_payload["assignments"],
        silence_segments,
    )
    coarse_distribution = _compute_coarse_distribution_from_assignments(
        assignment_payload["assignments"],
    )
    current_state = _current_group_state(
        state_segments,
        silence_segments,
        student_index,
        display_context,
        assignment_payload["by_sequence"],
    )
    active_silence = _active_silence_context(silence_segments)

    return {
        "group_id": group_id,
        "group_name": group["name"],
        "group_code": group["group_code"],
        "session_id": session_id,
        "requested_session_id": session_id,
        "resolved_session_id": resolved_session_id,
        "scope_mode": scope_resolution.get("scope_mode"),
        "scope_resolved_from": scope_resolution.get("resolved_from"),
        "fallback_reason": (
            scope_resolution.get("fallback_reason")
            or (
                "explicit_legacy_scope_enabled"
                if include_legacy_scope
                else None
            )
        ),
        "legacy_scope_fallback": bool(include_legacy_scope),
        "session": scope,
        "window_minutes": window_minutes,
        "generated_at": now_str(),
        "time_range": {
            "start": ws,
            "end": we,
            "legacy_data_warning": legacy_warning,
        },
        "state_system": _state_system(include_unclassified=True),
        "detailed_state_system": _detailed_state_system(
            include_unclassified=True
        ),
        "coarse_state_system": _coarse_state_system(),
        "coarse_state_debug_only": True,
        "state_source_mode": "canonical",
        "state_source_policy": CANONICAL_STATE_SOURCE_POLICY,
        "message_state_context": display_context,
        "message_assignment_policy": MESSAGE_STATE_ASSIGNMENT_POLICY,
        "message_assignment_summary": assignment_payload["summary"],
        "current_state": current_state,
        "active_silence": active_silence,
        "current_state_source": current_state.get("current_state_source"),
        "current_state_reason": current_state.get("current_state_reason"),
        "state_segments": state_segments,
        "silence_segments": silence_segments,
        "state_snapshots": snapshots,
        "snapshots": snapshots,
        "latest_snapshot": snapshots[-1] if snapshots else None,
        "distribution": distribution,
        "detailed_distribution": detailed_distribution,
        "coarse_distribution": coarse_distribution,
        "transitions": _compute_transitions(snapshots),
        "summary": {
            "state_segment_count": len(state_segments),
            "silence_segment_count": len(silence_segments),
            "active_silence": bool(active_silence.get("active")),
            "message_assignment_policy": MESSAGE_STATE_ASSIGNMENT_POLICY,
            "message_assignment_summary": assignment_payload["summary"],
            "student_message_count": assignment_payload["summary"][
                "student_message_count"
            ],
            "display_assigned_student_message_count": (
                assignment_payload["summary"][
                    "display_assigned_student_message_count"
                ]
            ),
            "student_display_assignment_rate": assignment_payload["summary"][
                "student_display_assignment_rate"
            ],
            "precise_sub_state_message_count": assignment_payload["summary"][
                "precise_sub_state_message_count"
            ],
            "precise_sub_state_coverage_rate": assignment_payload["summary"][
                "precise_sub_state_coverage_rate"
            ],
            "legacy_monitor_only_message_count": assignment_payload["summary"][
                "legacy_monitor_only_message_count"
            ],
            "snapshot_count": len(snapshots),
            "latest_state_code": (
                current_state.get("semantic_state")
                or current_state.get("assessment_status")
            ),
            "latest_state_label": current_state.get("state_label"),
            "latest_assessment_status": current_state.get("assessment_status"),
            "latest_state_confidence": current_state.get("confidence"),
            "latest_state_at": current_state.get("updated_at"),
            "latest_state_source": current_state.get("source"),
            "latest_state_reason": current_state.get("current_state_reason"),
            "latest_state_segment_id": current_state.get("segment_id"),
            "distribution": distribution,
            "detailed_distribution": detailed_distribution,
            "coarse_distribution": coarse_distribution,
            "duration_note": "消息范围片段按范围内首尾学生消息时间计算；单消息片段持续时间为 0 秒，仅以前端最小宽度展示。消极沉默按记录的 start_at/end_at 或 gap_seconds 计算。",
        },
        "legacy_data_warning": legacy_warning,
        "excluded_segment_count": display_context.get(
            "excluded_segment_count",
            0,
        ),
        "excluded_segment_reasons": display_context.get(
            "excluded_segment_reasons",
            {},
        ),
        "quality_warnings": warnings,
    }


def get_current_canonical_state(group_id, session_id=None):
    """Return the teacher-facing current state without coarse snapshot fallback."""

    trend = get_emotion_trend(
        group_id=group_id,
        session_id=session_id,
        window_minutes=0,
        include_legacy_scope=False,
    )
    if "error" in trend:
        return trend
    current = dict(trend.get("current_state") or {})
    assessment_status = (
        str(current.get("assessment_status") or UNCLASSIFIED_STATE)
        .strip()
        .lower()
    )
    final_code = (
        current.get("final_sub_state_code")
        if is_primary_sub_state(current.get("final_sub_state_code"))
        else None
    )
    display_code = current.get("display_state_code") or final_code or (
        OBSERVING_STATE
        if assessment_status == OBSERVING_STATE
        else UNCLASSIFIED_STATE
    )
    display_label = (
        current.get("display_state_label")
        or current.get("state_label")
        or STATE_LABEL_MAP.get(display_code)
        or STATE_LABEL_MAP[UNCLASSIFIED_STATE]
    )
    return {
        "group_id": group_id,
        "requested_session_id": session_id,
        "resolved_session_id": trend.get("resolved_session_id"),
        "discussion_id": (
            trend.get("message_state_context") or {}
        ).get("discussion_id"),
        "final_sub_state_code": final_code,
        "final_sub_state_label": (
            STATE_LABEL_MAP.get(final_code) if final_code else None
        ),
        "display_state_code": display_code,
        "display_state_label": display_label,
        "state_code": display_code,
        "state_label": display_label,
        "assessment_status": assessment_status,
        "assignment_source": (
            current.get("assignment_source") or current.get("source")
        ),
        "inferred": bool(current.get("inferred")),
        "confidence": current.get("confidence"),
        "segment_id": current.get("segment_id"),
        "source_batch_id": current.get("source_batch_id"),
        "error_code": current.get("error_code"),
        "coarse_state_code": current.get("coarse_state_code"),
        "legacy_state_code": current.get("legacy_state_code"),
        "updated_at": current.get("updated_at"),
        "active_silence": trend.get("active_silence") or {"active": False},
        "source": "canonical_state_read_model",
        "read_only": True,
    }


__all__ = [
    "FORMAL_STATE_ORDER",
    "DETAILED_STATE_ORDER",
    "CANONICAL_STATE_SOURCE_POLICY",
    "MESSAGE_STATE_ASSIGNMENT_POLICY",
    "MESSAGE_RANGE_STATES",
    "OBSERVING_STATE",
    "SILENCE_STATE",
    "UNCLASSIFIED_STATE",
    "LEGACY_COARSE_STATE_ORDER",
    "PROCESS_DISPLAY_STATE_ORDER",
    "STATE_LABEL_MAP",
    "_DISTRIBUTION_KEYS",
    "_map_state_code",
    "_map_state_label",
    "_message_processing_status",
    "_parse_dt",
    "_resolve_time_range",
    "_resolve_session_scope",
    "_session_scope",
    "get_current_canonical_state",
    "get_emotion_trend",
]
