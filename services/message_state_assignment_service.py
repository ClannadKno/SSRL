# -*- coding: utf-8 -*-
"""Read-model assignment of final state display status to messages.

The assignment model is intentionally dynamic: callers provide already scoped
messages, state segments, batch windows, cursor context, and silence segments.
No table is written and no per-message LLM call is made here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.three_stage_schema import (
    FINAL_SUB_STATE_LABELS,
    is_legacy_state,
    is_primary_sub_state,
    legacy_state_for_sub_state,
)


OBSERVING_STATE = "observing"
UNCLASSIFIED_STATE = "unclassified"
UNKNOWN_SUB_STATE = "unknown_sub_state"
SUCCESS_UNCOVERED_STATE = "assessment_complete_unconfirmed"
FAILED_UNCLASSIFIED_STATE = "assessment_failed_unclassified"
MESSAGE_STATE_ASSIGNMENT_POLICY = "message_state_assignment_v2"

DISPLAY_STATE_LABELS = {
    OBSERVING_STATE: "观察中",
    UNCLASSIFIED_STATE: "未分类",
    UNKNOWN_SUB_STATE: "子状态不确定",
    SUCCESS_UNCOVERED_STATE: "已完成评估但无确认片段",
    FAILED_UNCLASSIFIED_STATE: "评估失败/未分类",
}

CARRY_FORWARD_CONFIG = {
    "max_messages": 2,
    "max_seconds": 90,
}

ACTIVE_BATCH_STATUSES = {"pending", "running"}
TERMINAL_FAILURE_STATUSES = {"failed", "superseded"}
TERMINAL_FAILURE_RESULTS = {"degraded", "quarantined"}

PRIMARY_SEGMENT_SOURCE_PRIORITY = {
    "llm": 60,
    "session_finalizer": 50,
    "strategy_llm": 40,
    "rule": 30,
    "state_monitor": 20,
    "legacy": 0,
}


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _role(message: dict[str, Any]) -> str:
    raw = str(
        message.get("role")
        or message.get("sender_type")
        or message.get("user_role")
        or "student"
    ).strip().lower()
    if raw in {"agent", "teacher"}:
        return raw
    return "student"


def _sequence(message: dict[str, Any]) -> int | None:
    return _to_int(message.get("sequence") or message.get("id"))


def _segment_range(segment: dict[str, Any]) -> tuple[int | None, int | None]:
    start = _to_int(
        segment.get("start_sequence")
        if segment.get("start_sequence") is not None
        else segment.get("start_message_id")
    )
    end = _to_int(
        segment.get("end_sequence")
        if segment.get("end_sequence") is not None
        else segment.get("end_message_id")
    )
    return start, end


def _state_code(value: Any) -> str:
    return str(value or "").strip()


def _source(segment: dict[str, Any]) -> str:
    return str(segment.get("source") or segment.get("segment_source") or "").strip().lower()


def _discussion_id(item: dict[str, Any], default_discussion_id: Any = None) -> int | None:
    return _to_int(item.get("discussion_id")) or _to_int(default_discussion_id)


def _same_scope(
    message: dict[str, Any],
    segment: dict[str, Any],
    *,
    group_id: Any = None,
    session_id: Any = None,
    discussion_id: Any = None,
) -> bool:
    message_group = _to_int(message.get("group_id")) or _to_int(group_id)
    segment_group = _to_int(segment.get("group_id")) or _to_int(group_id)
    if message_group is not None and segment_group is not None and message_group != segment_group:
        return False

    message_session = _to_int(message.get("session_id")) or _to_int(session_id)
    segment_session = _to_int(segment.get("session_id")) or _to_int(session_id)
    if (
        message_session is not None
        and segment_session is not None
        and message_session != segment_session
    ):
        return False

    message_discussion = _discussion_id(message, discussion_id)
    segment_discussion = _discussion_id(segment, discussion_id)
    if (
        message_discussion is not None
        and segment_discussion is not None
        and message_discussion != segment_discussion
    ):
        return False
    return True


def _segment_covers(
    segment: dict[str, Any],
    sequence: int,
    message: dict[str, Any] | None = None,
    *,
    group_id: Any = None,
    session_id: Any = None,
    discussion_id: Any = None,
) -> bool:
    start, end = _segment_range(segment)
    if start is None or end is None:
        return False
    if not start <= int(sequence) <= end:
        return False
    if message is not None:
        return _same_scope(
            message,
            segment,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        )
    return True


def _primary_code(segment: dict[str, Any]) -> str | None:
    explicit = _state_code(segment.get("final_sub_state_code"))
    if is_primary_sub_state(explicit):
        return explicit
    canonical = _state_code(segment.get("canonical_sub_state_code"))
    if is_primary_sub_state(canonical):
        return canonical
    if _source(segment) in {"state_monitor", "legacy"}:
        return None
    state_code = _state_code(segment.get("state_code"))
    return state_code if is_primary_sub_state(state_code) else None


def _is_confirmed_primary_segment(segment: dict[str, Any]) -> bool:
    status = str(segment.get("assessment_status") or "confirmed").strip().lower()
    return status == "confirmed" and _primary_code(segment) is not None


def _primary_segment_key(segment: dict[str, Any]) -> tuple[Any, ...]:
    source = _source(segment)
    successful_llm = (
        source == "llm"
        and segment.get("assessment_batch_id") is not None
        and str(segment.get("batch_status") or "").lower() == "succeeded"
    )
    return (
        1 if successful_llm else 0,
        PRIMARY_SEGMENT_SOURCE_PRIORITY.get(source, 0),
        1 if segment.get("is_finalized") else 0,
        str(segment.get("batch_completed_at") or segment.get("updated_at") or ""),
        _to_int(segment.get("assessment_batch_id")) or 0,
        _to_float(segment.get("confidence")),
        _to_int(segment.get("id")) or 0,
    )


def winning_primary_segment_for_sequence(
    state_segments: list[dict[str, Any]],
    sequence: int,
    *,
    message: dict[str, Any] | None = None,
    group_id: Any = None,
    session_id: Any = None,
    discussion_id: Any = None,
) -> dict[str, Any] | None:
    candidates = [
        segment
        for segment in state_segments or []
        if _is_confirmed_primary_segment(segment)
        and _segment_covers(
            segment,
            sequence,
            message,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        )
    ]
    return max(candidates, key=_primary_segment_key) if candidates else None


def _is_confirmed_unknown_segment(segment: dict[str, Any]) -> bool:
    canonical = _state_code(
        segment.get("final_sub_state_code")
        or segment.get("canonical_sub_state_code")
        or segment.get("process_sub_state_code")
    )
    if canonical != UNKNOWN_SUB_STATE:
        return False
    if str(segment.get("assessment_status") or "confirmed").strip().lower() not in {
        "confirmed",
        UNCLASSIFIED_STATE,
    }:
        return False
    if _source(segment) in {"state_monitor", "legacy"}:
        return False
    if (
        segment.get("assessment_batch_id") is not None
        and str(segment.get("batch_status") or "").strip().lower() != "succeeded"
    ):
        return False
    return True


def _unknown_segment_for_sequence(
    state_segments: list[dict[str, Any]],
    sequence: int,
    *,
    message: dict[str, Any],
    group_id: Any = None,
    session_id: Any = None,
    discussion_id: Any = None,
) -> dict[str, Any] | None:
    candidates = [
        segment
        for segment in state_segments or []
        if _is_confirmed_unknown_segment(segment)
        and _segment_covers(
            segment,
            sequence,
            message,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        )
    ]
    return max(candidates, key=_primary_segment_key) if candidates else None


def _legacy_coarse_code(segment: dict[str, Any], final_code: str | None = None) -> str | None:
    for key in ("coarse_state_code", "legacy_state_code", "state_code"):
        code = _state_code(segment.get(key))
        if is_legacy_state(code):
            return code
    if final_code:
        return legacy_state_for_sub_state(final_code)
    return None


def _legacy_segment_for_sequence(
    state_segments: list[dict[str, Any]],
    sequence: int,
    *,
    message: dict[str, Any],
    group_id: Any = None,
    session_id: Any = None,
    discussion_id: Any = None,
) -> dict[str, Any] | None:
    candidates = []
    for segment in state_segments or []:
        if _primary_code(segment):
            continue
        if not _legacy_coarse_code(segment):
            continue
        if not _segment_covers(
            segment,
            sequence,
            message,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        ):
            continue
        if _source(segment) in {"state_monitor", "legacy"} or _legacy_coarse_code(segment):
            candidates.append(segment)
    return max(candidates, key=_primary_segment_key) if candidates else None


def _normalise_batch_windows(
    display_context: dict[str, Any] | None = None,
    state_assessment_batches: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    windows = list(state_assessment_batches or [])
    if display_context:
        windows.extend(display_context.get("batch_windows") or [])
    result = []
    for item in windows:
        start = _to_int(
            item.get("start_sequence")
            if item.get("start_sequence") is not None
            else item.get("candidate_start_sequence")
        )
        end = _to_int(
            item.get("end_sequence")
            if item.get("end_sequence") is not None
            else item.get("candidate_end_sequence")
        )
        if start is None or end is None:
            continue
        result.append({
            "batch_id": item.get("batch_id") or item.get("id"),
            "status": str(item.get("status") or "").strip().lower(),
            "terminal_status": str(item.get("terminal_status") or "").strip().lower(),
            "error_code": item.get("error_code"),
            "fallback_action": item.get("fallback_action"),
            "fallback_segment_count": item.get("fallback_segment_count"),
            "start_sequence": start,
            "end_sequence": end,
        })
    return result


def _matching_batches(sequence: int, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        window
        for window in windows
        if int(window["start_sequence"]) <= int(sequence) <= int(window["end_sequence"])
    ]


def _batch_sort_key(window: dict[str, Any]) -> tuple[int, int, str]:
    return (
        _to_int(window.get("end_sequence")) or 0,
        _to_int(window.get("batch_id")) or 0,
        str(window.get("status") or ""),
    )


def _assignment(
    message: dict[str, Any],
    *,
    assessment_status: str | None,
    assignment_source: str | None,
    state_assignment_reason: str | None,
    final_sub_state_code: str | None = None,
    inferred: bool = False,
    source_segment: dict[str, Any] | None = None,
    source_batch: dict[str, Any] | None = None,
    error_code: str | None = None,
    context_state_code: str | None = None,
    display_state_code: str | None = None,
) -> dict[str, Any]:
    final_code = final_sub_state_code if is_primary_sub_state(final_sub_state_code) else None
    resolved_display_code = (
        str(display_state_code or "").strip()
        or final_code
        or (
            assessment_status
            if assessment_status in {OBSERVING_STATE, UNCLASSIFIED_STATE}
            else None
        )
    )
    display_state_label = (
        FINAL_SUB_STATE_LABELS.get(resolved_display_code)
        or DISPLAY_STATE_LABELS.get(resolved_display_code)
    )
    coarse = (
        _legacy_coarse_code(source_segment or {}, final_code)
        if source_segment or final_code
        else None
    )
    legacy = coarse if coarse and is_legacy_state(coarse) else None
    return {
        "message_id": message.get("id"),
        "sequence": _sequence(message),
        "role": _role(message),
        "final_sub_state_code": final_code,
        "final_sub_state_label": (
            FINAL_SUB_STATE_LABELS.get(final_code) if final_code else None
        ),
        "display_state_code": resolved_display_code,
        "display_state_label": display_state_label,
        "semantic_state": final_code,
        "coarse_state_code": coarse,
        "legacy_state_code": legacy,
        "assessment_status": assessment_status,
        "assignment_source": assignment_source,
        "state_assignment_reason": state_assignment_reason,
        "inferred": bool(inferred),
        "source_segment_id": (
            source_segment.get("id") if source_segment else None
        ),
        "source_batch_id": (
            (source_batch.get("batch_id") or source_batch.get("id"))
            if source_batch
            else (
                source_segment.get("assessment_batch_id") if source_segment else None
            )
        ),
        "error_code": error_code,
        "confidence": source_segment.get("confidence") if source_segment else None,
        "state_overlays": (
            list(source_segment.get("state_overlays") or [])
            if source_segment
            else []
        ),
        "context_state_code": context_state_code,
        "segment": source_segment,
    }


def _non_student_assignment(
    message: dict[str, Any],
    context_state_code: str | None,
) -> dict[str, Any]:
    return _assignment(
        message,
        assessment_status=None,
        assignment_source="non_student_message",
        state_assignment_reason="non_student_messages_excluded_from_student_state_coverage",
        context_state_code=context_state_code,
    )


def _source_assignment_name(segment: dict[str, Any]) -> str:
    source = _source(segment)
    if source == "llm":
        return "model_segment"
    if source == "session_finalizer":
        return "session_finalizer_segment"
    if source == "strategy_llm":
        return "strategy_model_segment"
    if source == "rule":
        return "rule_canonical_segment"
    return "canonical_segment"


def _terminal_batch_assignment(
    message: dict[str, Any],
    matching_windows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    terminal = [
        window
        for window in matching_windows
        if window.get("status") not in ACTIVE_BATCH_STATUSES
    ]
    if not terminal:
        return None
    window = max(terminal, key=_batch_sort_key)
    terminal_status = window.get("terminal_status")
    failed = (
        window.get("status") in TERMINAL_FAILURE_STATUSES
        or terminal_status in TERMINAL_FAILURE_RESULTS
    )
    succeeded = window.get("status") == "succeeded"
    if not failed and not succeeded:
        return None
    error = (
        window.get("error_code") or terminal_status or window.get("status")
        if failed
        else None
    )
    return _assignment(
        message,
        assessment_status=UNCLASSIFIED_STATE,
        assignment_source=(
            "batch_unclassified" if failed else "successful_batch_uncovered"
        ),
        state_assignment_reason=(
            "explicit_failed_batch_without_confirmed_segment"
            if failed
            else "assessment_completed_without_confirmed_segment"
        ),
        source_batch=window,
        error_code=error,
        display_state_code=(
            FAILED_UNCLASSIFIED_STATE if failed else SUCCESS_UNCOVERED_STATE
        ),
    )


def _unknown_sub_state_assignment(
    message: dict[str, Any],
    segment: dict[str, Any],
) -> dict[str, Any]:
    return _assignment(
        message,
        assessment_status=UNCLASSIFIED_STATE,
        assignment_source="unknown_sub_state_segment",
        state_assignment_reason="confirmed_unknown_sub_state_segment",
        source_segment=segment,
        display_state_code=UNKNOWN_SUB_STATE,
    )


def _post_intervention_observing_assignment(
    message: dict[str, Any],
    display_context: dict[str, Any],
) -> dict[str, Any] | None:
    if display_context.get("observation_status") != "observing":
        return None
    sequence = _sequence(message)
    if sequence is None:
        return None
    start = display_context.get("observation_started_sequence")
    expired_through = display_context.get("observation_expired_through_sequence")
    if (
        start is not None
        and sequence >= int(start)
        and (expired_through is None or sequence > int(expired_through))
    ):
        return _assignment(
            message,
            assessment_status=OBSERVING_STATE,
            assignment_source="post_intervention_observing",
            state_assignment_reason="active_post_intervention_observation",
        )
    return None


def _active_batch_assignment(
    message: dict[str, Any],
    matching_windows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    active = [
        window for window in matching_windows if window.get("status") in ACTIVE_BATCH_STATUSES
    ]
    if not active:
        return None
    window = max(active, key=_batch_sort_key)
    return _assignment(
        message,
        assessment_status=OBSERVING_STATE,
        assignment_source="awaiting_active_batch",
        state_assignment_reason="assessment_batch_in_progress",
        source_batch=window,
    )


def _crosses_intervention(previous_sequence: int, sequence: int, display_context: dict[str, Any]) -> bool:
    intervention_sequence = _to_int(display_context.get("last_intervention_sequence"))
    return (
        intervention_sequence is not None
        and previous_sequence <= intervention_sequence < sequence
    )


def _batch_intersects_failed_gap(
    previous_sequence: int,
    sequence: int,
    windows: list[dict[str, Any]],
) -> bool:
    for window in windows:
        if window.get("status") in ACTIVE_BATCH_STATUSES:
            continue
        failed = (
            window.get("status") in TERMINAL_FAILURE_STATUSES
            or window.get("terminal_status") in TERMINAL_FAILURE_RESULTS
            or bool(window.get("error_code"))
        )
        if not failed:
            continue
        if window["start_sequence"] <= sequence and window["end_sequence"] > previous_sequence:
            return True
    return False


def _silence_between(
    previous_message: dict[str, Any],
    message: dict[str, Any],
    silence_segments: list[dict[str, Any]],
) -> bool:
    previous_sequence = _sequence(previous_message)
    sequence = _sequence(message)
    previous_time = _parse_dt(previous_message.get("created_at"))
    current_time = _parse_dt(message.get("created_at"))
    for segment in silence_segments or []:
        prev = _to_int(segment.get("previous_student_message_id"))
        nxt = _to_int(segment.get("next_student_message_id"))
        if (
            previous_sequence is not None
            and sequence is not None
            and prev is not None
            and previous_sequence <= prev < sequence
        ):
            return True
        if (
            previous_sequence is not None
            and sequence is not None
            and nxt is not None
            and previous_sequence < nxt <= sequence
        ):
            return True
        silence_start = _parse_dt(segment.get("start_at"))
        silence_end = _parse_dt(segment.get("end_at") or segment.get("last_observed_at"))
        if previous_time and current_time and silence_start:
            end = silence_end or current_time
            if silence_start <= current_time and end >= previous_time:
                return True
    return False


def _new_segment_evidence_between(
    previous_sequence: int,
    sequence: int,
    state_segments: list[dict[str, Any]],
    source_segment_id: Any,
) -> bool:
    source_id = _to_int(source_segment_id)
    for segment in state_segments or []:
        if _to_int(segment.get("id")) == source_id:
            continue
        start, _end = _segment_range(segment)
        if start is not None and previous_sequence < start <= sequence:
            return True
    return False


def _same_discussion_for_carry(
    previous_message: dict[str, Any],
    message: dict[str, Any],
    default_discussion_id: Any,
) -> bool:
    previous_discussion = _discussion_id(previous_message, default_discussion_id)
    current_discussion = _discussion_id(message, default_discussion_id)
    if previous_discussion is None or current_discussion is None:
        return previous_discussion == current_discussion
    return previous_discussion == current_discussion


def _carry_forward_assignment(
    message: dict[str, Any],
    *,
    previous_primary_assignment: dict[str, Any] | None,
    previous_primary_message: dict[str, Any] | None,
    state_segments: list[dict[str, Any]],
    silence_segments: list[dict[str, Any]],
    batch_windows: list[dict[str, Any]],
    display_context: dict[str, Any],
    discussion_id: Any = None,
) -> dict[str, Any] | None:
    if not previous_primary_assignment or not previous_primary_message:
        return None
    final_code = previous_primary_assignment.get("final_sub_state_code")
    if not is_primary_sub_state(final_code):
        return None

    sequence = _sequence(message)
    previous_sequence = _sequence(previous_primary_message)
    if sequence is None or previous_sequence is None or sequence <= previous_sequence:
        return None
    if sequence - previous_sequence > int(CARRY_FORWARD_CONFIG["max_messages"]):
        return None

    current_at = _parse_dt(message.get("created_at"))
    previous_at = _parse_dt(previous_primary_message.get("created_at"))
    if current_at and previous_at:
        seconds = (current_at - previous_at).total_seconds()
        if seconds < 0 or seconds > int(CARRY_FORWARD_CONFIG["max_seconds"]):
            return None

    if not _same_discussion_for_carry(previous_primary_message, message, discussion_id):
        return None
    if _crosses_intervention(previous_sequence, sequence, display_context):
        return None
    if _batch_intersects_failed_gap(previous_sequence, sequence, batch_windows):
        return None
    if _silence_between(previous_primary_message, message, silence_segments):
        return None
    if _new_segment_evidence_between(
        previous_sequence,
        sequence,
        state_segments,
        previous_primary_assignment.get("source_segment_id"),
    ):
        return None

    segment = previous_primary_assignment.get("segment")
    return _assignment(
        message,
        assessment_status="confirmed",
        assignment_source="carry_forward",
        state_assignment_reason="short_gap_carry_forward",
        final_sub_state_code=final_code,
        inferred=True,
        source_segment=segment,
    )


def _cursor_fallback_assignment(
    message: dict[str, Any],
    display_context: dict[str, Any],
) -> dict[str, Any]:
    sequence = _sequence(message)
    if display_context.get("has_assessment_pipeline"):
        finalized_through = _to_int(
            display_context.get("last_finalized_student_sequence")
        )
        if (
            finalized_through is not None
            and sequence is not None
            and sequence <= finalized_through
        ):
            return _assignment(
                message,
                assessment_status=UNCLASSIFIED_STATE,
                assignment_source="cursor_unclassified",
                state_assignment_reason="finalized_cursor_without_confirmed_segment",
            )
        intervention_through = _to_int(display_context.get("last_intervention_sequence"))
        if (
            intervention_through is not None
            and sequence is not None
            and sequence <= intervention_through
        ):
            return _assignment(
                message,
                assessment_status=UNCLASSIFIED_STATE,
                assignment_source="cursor_unclassified",
                state_assignment_reason="intervention_boundary_without_confirmed_segment",
            )
        return _assignment(
            message,
            assessment_status=OBSERVING_STATE,
            assignment_source="awaiting_assessment_batch",
            state_assignment_reason="awaiting_assessment_batch",
        )
    return _assignment(
        message,
        assessment_status=OBSERVING_STATE,
        assignment_source="awaiting_detection_conditions",
        state_assignment_reason="awaiting_detection_conditions",
    )


def _legacy_assignment(
    message: dict[str, Any],
    segment: dict[str, Any],
) -> dict[str, Any]:
    source = (
        "legacy_monitor_only"
        if _source(segment) == "state_monitor"
        else "legacy_coarse_only"
    )
    return _assignment(
        message,
        assessment_status=UNCLASSIFIED_STATE,
        assignment_source=source,
        state_assignment_reason=f"{source}_not_final_sub_state",
        source_segment=segment,
        error_code="legacy_coarse_not_final_sub_state",
    )


def _normalise_message_list(messages: Any) -> list[dict[str, Any]]:
    if isinstance(messages, dict):
        iterable = messages.values()
    else:
        iterable = messages or []
    return [dict(message) for message in iterable]


def assign_message_states(
    *,
    messages: Any,
    state_segments: list[dict[str, Any]] | None = None,
    state_assessment_batches: list[dict[str, Any]] | None = None,
    display_context: dict[str, Any] | None = None,
    silence_segments: list[dict[str, Any]] | None = None,
    group_id: Any = None,
    session_id: Any = None,
    discussion_id: Any = None,
) -> dict[str, Any]:
    """Assign each student message to a final sub-state or process state."""

    display_context = dict(display_context or {})
    batch_windows = _normalise_batch_windows(
        display_context,
        state_assessment_batches,
    )
    default_discussion_id = (
        discussion_id
        if discussion_id is not None
        else display_context.get("discussion_id")
    )
    ordered_messages = sorted(
        _normalise_message_list(messages),
        key=lambda message: (
            _sequence(message) if _sequence(message) is not None else 10**12,
            _to_int(message.get("id")) or 0,
        ),
    )

    assignments = []
    by_sequence = {}
    by_message_id = {}
    previous_primary_assignment = None
    previous_primary_message = None
    latest_context_state_code = None

    for message in ordered_messages:
        sequence = _sequence(message)
        if _role(message) != "student":
            assignment = _non_student_assignment(message, latest_context_state_code)
        elif sequence is None:
            assignment = _assignment(
                message,
                assessment_status=UNCLASSIFIED_STATE,
                assignment_source="invalid_message_sequence",
                state_assignment_reason="student_message_missing_sequence",
                error_code="missing_sequence",
            )
        else:
            primary_segment = winning_primary_segment_for_sequence(
                state_segments or [],
                sequence,
                message=message,
                group_id=group_id,
                session_id=session_id,
                discussion_id=default_discussion_id,
            )
            if primary_segment:
                final_code = _primary_code(primary_segment)
                assignment = _assignment(
                    message,
                    assessment_status="confirmed",
                    assignment_source=_source_assignment_name(primary_segment),
                    state_assignment_reason="confirmed_primary_segment_coverage",
                    final_sub_state_code=final_code,
                    source_segment=primary_segment,
                )
            else:
                matching_windows = _matching_batches(sequence, batch_windows)
                unknown_segment = _unknown_segment_for_sequence(
                    state_segments or [],
                    sequence,
                    message=message,
                    group_id=group_id,
                    session_id=session_id,
                    discussion_id=default_discussion_id,
                )
                assignment = (
                    _unknown_sub_state_assignment(message, unknown_segment)
                    if unknown_segment
                    else _terminal_batch_assignment(message, matching_windows)
                    or _post_intervention_observing_assignment(
                        message,
                        display_context,
                    )
                    or _active_batch_assignment(message, matching_windows)
                )
                if assignment is None:
                    legacy_segment = _legacy_segment_for_sequence(
                        state_segments or [],
                        sequence,
                        message=message,
                        group_id=group_id,
                        session_id=session_id,
                        discussion_id=default_discussion_id,
                    )
                    if legacy_segment:
                        assignment = _legacy_assignment(message, legacy_segment)
                if assignment is None:
                    assignment = _carry_forward_assignment(
                        message,
                        previous_primary_assignment=previous_primary_assignment,
                        previous_primary_message=previous_primary_message,
                        state_segments=state_segments or [],
                        silence_segments=silence_segments or [],
                        batch_windows=batch_windows,
                        display_context=display_context,
                        discussion_id=default_discussion_id,
                    )
                if assignment is None:
                    assignment = _cursor_fallback_assignment(message, display_context)

        assignments.append(assignment)
        if assignment.get("sequence") is not None:
            by_sequence[assignment["sequence"]] = assignment
        if assignment.get("message_id") is not None:
            by_message_id[assignment["message_id"]] = assignment

        if (
            assignment.get("role") == "student"
            and assignment.get("assessment_status") == "confirmed"
            and is_primary_sub_state(assignment.get("final_sub_state_code"))
            and not assignment.get("inferred")
        ):
            previous_primary_assignment = assignment
            previous_primary_message = message
            latest_context_state_code = assignment.get("final_sub_state_code")
        elif (
            assignment.get("role") == "student"
            and assignment.get("assessment_status") == "confirmed"
            and is_primary_sub_state(assignment.get("final_sub_state_code"))
        ):
            latest_context_state_code = assignment.get("final_sub_state_code")

    return {
        "policy": MESSAGE_STATE_ASSIGNMENT_POLICY,
        "carry_forward_config": dict(CARRY_FORWARD_CONFIG),
        "assignments": assignments,
        "by_sequence": by_sequence,
        "by_message_id": by_message_id,
        "summary": summarize_assignments(assignments),
    }


def summarize_assignments(assignments: list[dict[str, Any]]) -> dict[str, Any]:
    student_assignments = [
        item for item in assignments or [] if item.get("role") == "student"
    ]
    non_student = [
        item for item in assignments or [] if item.get("role") != "student"
    ]
    display_assigned = [
        item
        for item in student_assignments
        if item.get("assessment_status")
        in {"confirmed", OBSERVING_STATE, UNCLASSIFIED_STATE}
    ]
    confirmed_primary = [
        item
        for item in student_assignments
        if item.get("assessment_status") == "confirmed"
        and is_primary_sub_state(item.get("final_sub_state_code"))
    ]
    precise_primary = [item for item in confirmed_primary if not item.get("inferred")]
    observing = [
        item
        for item in student_assignments
        if item.get("assessment_status") == OBSERVING_STATE
    ]
    unclassified = [
        item
        for item in student_assignments
        if item.get("assessment_status") == UNCLASSIFIED_STATE
    ]
    inferred = [item for item in student_assignments if item.get("inferred")]
    success_uncovered = [
        item
        for item in student_assignments
        if item.get("display_state_code") == SUCCESS_UNCOVERED_STATE
    ]
    unknown_sub_state = [
        item
        for item in student_assignments
        if item.get("display_state_code") == UNKNOWN_SUB_STATE
    ]
    failed_unclassified = [
        item
        for item in student_assignments
        if item.get("display_state_code") == FAILED_UNCLASSIFIED_STATE
    ]
    other_unclassified = [
        item
        for item in unclassified
        if item.get("display_state_code")
        not in {
            SUCCESS_UNCOVERED_STATE,
            UNKNOWN_SUB_STATE,
            FAILED_UNCLASSIFIED_STATE,
        }
    ]
    legacy_only = [
        item
        for item in student_assignments
        if item.get("assignment_source") == "legacy_monitor_only"
    ]
    total = len(student_assignments)
    return {
        "policy": MESSAGE_STATE_ASSIGNMENT_POLICY,
        "student_message_count": total,
        "non_student_message_count": len(non_student),
        "display_assigned_student_message_count": len(display_assigned),
        "student_display_assignment_rate": (
            round(len(display_assigned) / total, 4) if total else 1.0
        ),
        "confirmed_primary_sub_state_message_count": len(confirmed_primary),
        "precise_sub_state_message_count": len(precise_primary),
        "precise_sub_state_coverage_rate": (
            round(len(precise_primary) / total, 4) if total else 1.0
        ),
        "observing_student_message_count": len(observing),
        "unclassified_student_message_count": len(unclassified),
        "success_uncovered_student_message_count": len(success_uncovered),
        "unknown_sub_state_student_message_count": len(unknown_sub_state),
        "failed_unclassified_student_message_count": len(failed_unclassified),
        "other_unclassified_student_message_count": len(other_unclassified),
        "unclassified_category_count_invariant": (
            len(success_uncovered)
            + len(unknown_sub_state)
            + len(failed_unclassified)
            + len(other_unclassified)
            == len(unclassified)
        ),
        "inferred_assignment_count": len(inferred),
        "legacy_monitor_only_message_count": len(legacy_only),
        "student_state_count_invariant": (
            len(confirmed_primary) + len(observing) + len(unclassified) == total
        ),
    }


def message_processing_status(
    sequence: int,
    state_segments: list[dict[str, Any]],
    display_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility helper for older callers that need one sequence status."""

    payload = assign_message_states(
        messages=[{"sequence": sequence, "role": "student"}],
        state_segments=state_segments,
        display_context=display_context or {},
    )
    assignment = payload["assignments"][0]
    return {
        "semantic_state": assignment.get("final_sub_state_code"),
        "assessment_status": assignment.get("assessment_status"),
        "segment": assignment.get("segment"),
        "state_assignment_reason": assignment.get("state_assignment_reason"),
        "assignment_source": assignment.get("assignment_source"),
        "display_state_code": assignment.get("display_state_code"),
        "display_state_label": assignment.get("display_state_label"),
        "inferred": assignment.get("inferred"),
        "source_batch_id": assignment.get("source_batch_id"),
        "error_code": assignment.get("error_code"),
    }


__all__ = [
    "ACTIVE_BATCH_STATUSES",
    "CARRY_FORWARD_CONFIG",
    "DISPLAY_STATE_LABELS",
    "FAILED_UNCLASSIFIED_STATE",
    "MESSAGE_STATE_ASSIGNMENT_POLICY",
    "OBSERVING_STATE",
    "SUCCESS_UNCOVERED_STATE",
    "UNCLASSIFIED_STATE",
    "UNKNOWN_SUB_STATE",
    "assign_message_states",
    "message_processing_status",
    "summarize_assignments",
    "winning_primary_segment_for_sequence",
]
