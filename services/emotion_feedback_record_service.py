# -*- coding: utf-8 -*-
"""Read-only teacher and research views for emotion feedback records.

Emotion feedback and canonical collaboration states intentionally remain two
separate result sets.  The optional nearest-state association is calculated in
memory for research export only and is never persisted or fed back into either
Emotion Stage E1 or E2.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from db import query_all, query_one


EMOTION_FEEDBACK_STATES = frozenset(
    {
        "GROUP_EXCELLENT",
        "GROUP_IMPROVING",
        "GROUP_DECLINING",
        "GROUP_LOW_PARTICIPATION",
        "GROUP_SUSTAINED_EXCELLENT",
    }
)


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalize_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 200
    return max(1, min(value, 500))


def _ensure_session(session_id: int) -> None:
    if not query_one("SELECT id FROM experiment_sessions WHERE id=?", (int(session_id),)):
        raise ValueError("session not found")


def list_emotion_feedback_records(
    *,
    session_id: Optional[int] = None,
    group_id: Optional[int] = None,
    limit: Optional[int] = 200,
    include_nearest_canonical: bool = False,
) -> list[dict]:
    """Return fixed-slot emotion records without projecting strategy fields."""
    clauses = []
    params: list[Any] = []
    if session_id is not None:
        clauses.append("ers.session_id=?")
        params.append(int(session_id))
    if group_id is not None:
        clauses.append("ers.group_id=?")
        params.append(int(group_id))
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(_normalize_limit(limit))
    rows = query_all(
        """
        SELECT ers.id AS slot_id,
               ers.session_id,
               ers.group_id,
               ers.discussion_id,
               ers.slot_index,
               ers.previous_window_start,
               ers.previous_window_end,
               ers.current_window_start,
               ers.current_window_end,
               ers.previous_metrics_json,
               ers.current_metrics_json,
               ers.status AS slot_status,
               ers.skip_reason AS slot_failure_reason,
               ers.last_error AS slot_last_error,
               ers.created_at,
               efa.emotion_feedback_state AS assessment_feedback_state,
               efa.confidence,
               efa.comparison_summary,
               efa.current_window_summary,
               efa.previous_window_summary,
               efa.evidence_message_ids_json,
               efa.failure_reason AS assessment_failure_reason,
               efg.emotion_feedback_state AS generation_feedback_state,
               efg.final_text,
               efg.fallback_used,
               efg.failure_reason AS generation_failure_reason,
               efg.published_at,
               ir.emotion_feedback_type_code AS legacy_feedback_state,
               ir.final_visible_message AS legacy_final_text,
               ir.fallback_used AS legacy_fallback_used,
               ir.actual_published_at AS legacy_published_at
          FROM emotion_reflection_slots AS ers
          LEFT JOIN emotion_feedback_assessments AS efa
            ON efa.slot_id=ers.id
          LEFT JOIN emotion_feedback_generations AS efg
            ON efg.id=(
                SELECT candidate.id
                  FROM emotion_feedback_generations AS candidate
                 WHERE candidate.slot_id=ers.id
                 ORDER BY candidate.attempt_no DESC, candidate.id DESC
                 LIMIT 1
            )
          LEFT JOIN intervention_runs AS ir
            ON ir.id=ers.intervention_run_id
        """
        + where_sql
        + " ORDER BY ers.session_id DESC, ers.group_id ASC, ers.slot_index DESC"
        + limit_sql,
        tuple(params),
    )

    records = []
    for raw in rows:
        row = dict(raw)
        raw_state = (
            row.get("assessment_feedback_state")
            or row.get("generation_feedback_state")
            or row.get("legacy_feedback_state")
        )
        feedback_state = raw_state if raw_state in EMOTION_FEEDBACK_STATES else None
        failure_reason = (
            row.get("assessment_failure_reason")
            or row.get("generation_failure_reason")
            or row.get("slot_last_error")
            or row.get("slot_failure_reason")
        )
        if raw_state and not feedback_state and not failure_reason:
            failure_reason = "legacy_feedback_state_not_supported"
        records.append(
            {
                "slot_id": row.get("slot_id"),
                "session_id": row.get("session_id"),
                "group_id": row.get("group_id"),
                "discussion_id": row.get("discussion_id"),
                "slot_index": row.get("slot_index"),
                "previous_window_start": row.get("previous_window_start"),
                "previous_window_end": row.get("previous_window_end"),
                "current_window_start": row.get("current_window_start"),
                "current_window_end": row.get("current_window_end"),
                "emotion_feedback_state": feedback_state,
                "feedback_state": feedback_state,
                "confidence": row.get("confidence"),
                "comparison_summary": row.get("comparison_summary"),
                "current_window_summary": row.get("current_window_summary"),
                "previous_window_summary": row.get("previous_window_summary"),
                "evidence_message_ids": _json_value(
                    row.get("evidence_message_ids_json"), []
                ),
                "current_metrics": _json_value(row.get("current_metrics_json"), {}),
                "previous_metrics": _json_value(row.get("previous_metrics_json"), {}),
                "final_text": row.get("final_text") or row.get("legacy_final_text"),
                "fallback_used": bool(
                    row.get("fallback_used")
                    if row.get("fallback_used") is not None
                    else row.get("legacy_fallback_used")
                ),
                "slot_status": row.get("slot_status"),
                "failure_reason": failure_reason,
                "created_at": row.get("created_at"),
                "published_at": row.get("published_at")
                or row.get("legacy_published_at"),
            }
        )
    if include_nearest_canonical:
        _attach_nearest_canonical_states(records)
    return records


def list_canonical_state_records(
    *, session_id: int, group_id: Optional[int] = None, limit: int = 200
) -> list[dict]:
    """Return the canonical timeline as a result set separate from emotion."""
    _ensure_session(session_id)
    clauses = ["session_id=?", "canonical_sub_state_code IS NOT NULL"]
    params: list[Any] = [int(session_id)]
    if group_id is not None:
        clauses.append("group_id=?")
        params.append(int(group_id))
    params.append(_normalize_limit(limit))
    rows = query_all(
        """
        SELECT id AS segment_id, session_id, group_id, discussion_id,
               canonical_sub_state_code,
               secondary_tags_json AS overlay,
               COALESCE(sub_state_confidence, confidence) AS confidence,
               segment_kind, start_sequence, end_sequence,
               start_at, end_at, detected_at, created_at,
               assessment_batch_id, evidence_message_ids_json
          FROM collaboration_state_segments
         WHERE """
        + " AND ".join(clauses)
        + " ORDER BY COALESCE(detected_at, end_at, start_at, created_at) DESC, id DESC LIMIT ?",
        tuple(params),
    )
    return [
        {
            **dict(row),
            "evidence_message_ids": _json_value(
                dict(row).get("evidence_message_ids_json"), []
            ),
        }
        for row in rows
    ]


def get_session_agent_records(
    session_id: int, *, group_id: Optional[int] = None, limit: int = 200
) -> dict:
    """Build the teacher view with explicitly separated record collections."""
    _ensure_session(session_id)
    return {
        "session_id": int(session_id),
        "group_id": int(group_id) if group_id is not None else None,
        "emotion_feedbacks": list_emotion_feedback_records(
            session_id=session_id, group_id=group_id, limit=limit
        ),
        "canonical_states": list_canonical_state_records(
            session_id=session_id, group_id=group_id, limit=limit
        ),
        "data_separation": {
            "emotion_feedback_field": "emotion_feedback_state",
            "canonical_state_field": "canonical_sub_state_code",
            "shared_runtime_input": False,
        },
    }


def _attach_nearest_canonical_states(records: list[dict]) -> None:
    scopes = {
        (int(row["session_id"]), int(row["group_id"]), row.get("discussion_id"))
        for row in records
        if row.get("session_id") is not None and row.get("group_id") is not None
    }
    timeline_by_scope: dict[tuple, list[tuple[datetime, str]]] = {}
    for session_id, group_id, discussion_id in scopes:
        clauses = [
            "session_id=?",
            "group_id=?",
            "canonical_sub_state_code IS NOT NULL",
        ]
        params: list[Any] = [session_id, group_id]
        if discussion_id is not None:
            clauses.append("discussion_id=?")
            params.append(int(discussion_id))
        state_rows = query_all(
            """
            SELECT canonical_sub_state_code,
                   COALESCE(detected_at, end_at, start_at, created_at) AS anchor_at
              FROM collaboration_state_segments
             WHERE """
            + " AND ".join(clauses)
            + " ORDER BY anchor_at ASC, id ASC",
            tuple(params),
        )
        timeline = []
        for state_row in state_rows:
            anchor = _parse_time(state_row["anchor_at"])
            if anchor:
                timeline.append((anchor, state_row["canonical_sub_state_code"]))
        timeline_by_scope[(session_id, group_id, discussion_id)] = timeline

    for record in records:
        record["nearest_previous_canonical_state"] = None
        record["nearest_next_canonical_state"] = None
        scope = (
            int(record["session_id"]),
            int(record["group_id"]),
            record.get("discussion_id"),
        )
        timeline = timeline_by_scope.get(scope, [])
        previous_boundary = _parse_time(
            record.get("current_window_start") or record.get("current_window_end")
        )
        next_boundary = _parse_time(
            record.get("current_window_end") or record.get("current_window_start")
        )
        if previous_boundary:
            previous = [item for item in timeline if item[0] <= previous_boundary]
            if previous:
                record["nearest_previous_canonical_state"] = previous[-1][1]
        if next_boundary:
            upcoming = [item for item in timeline if item[0] >= next_boundary]
            if upcoming:
                record["nearest_next_canonical_state"] = upcoming[0][1]


__all__ = [
    "EMOTION_FEEDBACK_STATES",
    "get_session_agent_records",
    "list_canonical_state_records",
    "list_emotion_feedback_records",
]
