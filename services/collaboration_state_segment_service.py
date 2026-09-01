# -*- coding: utf-8 -*-
"""Normalized collaboration state segment persistence.

The table is a teacher-facing derived store. Raw detector and strategy audit
records remain in monitor_runs, state_assessments, group_states, and agent
audit tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from config import ONLINE_SILENCE_NO_MSG_SECONDS
from db import db, now_str, parse_dt, query_all, query_one

logger = logging.getLogger(__name__)


MESSAGE_STATE_ALIASES = {
    "positive_collaboration": "positive_collaboration",
    "conflict_tension": "conflict_tension",
    "frustration_stuck": "frustration_stuck",
    "blocked_frustration": "frustration_stuck",
    "off_task": "off_task",
    "task_detached": "off_task",
}
MESSAGE_SEGMENT_STATES = {
    "positive_collaboration",
    "conflict_tension",
    "frustration_stuck",
    "off_task",
}
STATE_MONITOR_MESSAGE_ALIASES = {
    "positive_collaboration": "positive_collaboration",
    "conflict_tension": "conflict_tension",
    "blocked_frustration": "blocked_frustration",
    "frustration_stuck": "blocked_frustration",
    "task_detached": "task_detached",
    "off_task": "task_detached",
}
STATE_MONITOR_MESSAGE_STATES = {
    "positive_collaboration",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
}
STATE_MONITOR_SOURCE = "state_monitor"
SILENCE_STATE = "negative_silence"


def _resolve_write_scope(
    conn,
    *,
    group_id,
    session_id=None,
    session_no=None,
    task_id=None,
    discussion_id=None,
    sequence=None,
):
    from services.discussion_scope import resolve_discussion_scope

    return resolve_discussion_scope(
        conn,
        group_id=group_id,
        sequence=sequence,
        session_id=session_id,
        session_no=session_no,
        task_id=task_id,
        discussion_id=discussion_id,
        allow_legacy_fallback=False,
    )


class SegmentValidationError(ValueError):
    """Raised when a normalized state segment fails persistence validation."""


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_dict(row) -> Optional[dict]:
    return dict(row) if row else None


def _as_int(value, field: str, *, allow_none: bool = False) -> Optional[int]:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise SegmentValidationError(f"invalid_{field}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SegmentValidationError(f"invalid_{field}")


def _as_confidence(value) -> float:
    if isinstance(value, bool):
        raise SegmentValidationError("invalid_confidence")
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        raise SegmentValidationError("invalid_confidence")
    if confidence < 0.0 or confidence > 1.0:
        raise SegmentValidationError("invalid_confidence")
    return confidence


def _normalize_message_state(state: Any) -> str:
    if not isinstance(state, str):
        raise SegmentValidationError("invalid_state")
    normalized = MESSAGE_STATE_ALIASES.get(state.strip())
    if normalized not in MESSAGE_SEGMENT_STATES:
        raise SegmentValidationError("invalid_state")
    return normalized


def _normalize_monitor_message_state(state: Any) -> str:
    if not isinstance(state, str):
        raise SegmentValidationError("invalid_state")
    normalized = STATE_MONITOR_MESSAGE_ALIASES.get(state.strip())
    if normalized not in STATE_MONITOR_MESSAGE_STATES:
        raise SegmentValidationError("invalid_state")
    return normalized


def _normalize_evidence_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise SegmentValidationError("missing_evidence")
    ids = []
    for item in value:
        ids.append(_as_int(item, "evidence_message_id"))
    if len(set(ids)) != len(ids):
        raise SegmentValidationError("duplicate_evidence")
    return ids


def _session_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"(({prefix}session_id IS NULL AND ? IS NULL) OR {prefix}session_id=?) "
        f"AND (({prefix}session_no IS NULL AND ? IS NULL) OR {prefix}session_no=?)"
    )


def _session_params(session_id, session_no) -> tuple:
    return (session_id, session_id, session_no, session_no)


def _is_student_message(row: dict) -> bool:
    role = str(row.get("role") or "").strip().lower()
    sender_type = str(row.get("sender_type") or "").strip().lower()
    return role == "student" or (not role and sender_type == "student")


def _message_matches_session(row: dict, session_id, session_no) -> bool:
    actual_session_id = row.get("session_id")
    actual_session_no = row.get("session_no")
    if session_id is not None:
        return actual_session_id is not None and str(actual_session_id) == str(session_id)
    if session_no is not None:
        return actual_session_no is not None and str(actual_session_no) == str(session_no)
    return True


def _load_messages_by_sequence(conn, group_id: int, sequences: set[int]) -> dict[int, dict]:
    if not sequences:
        return {}
    placeholders = ",".join("?" for _ in sequences)
    rows = conn.execute(
        f"""
        SELECT id, group_id, sequence, role, sender_type, session_id,
               session_no, task_id, created_at
        FROM messages
        WHERE group_id=? AND sequence IN ({placeholders})
        """,
        (group_id, *sorted(sequences)),
    ).fetchall()
    result = {}
    for row in rows:
        data = dict(row)
        if data.get("sequence") is not None:
            result[int(data["sequence"])] = data
    return result


def _strategy_dedupe_key(
    *,
    group_id: int,
    session_id,
    session_no,
    anchor,
    source_run_id,
    state_code: str,
    start_message_id: int,
    end_message_id: int,
    evidence_ids: list[int],
    prompt_version,
) -> str:
    raw = "|".join(
        [
            "strategy_llm",
            f"g={group_id}",
            f"sid={session_id or ''}",
            f"sno={session_no or ''}",
            f"a={anchor or ''}",
            f"run={source_run_id or ''}",
            f"state={state_code}",
            f"range={start_message_id}-{end_message_id}",
            "evidence=" + ",".join(str(i) for i in evidence_ids),
            f"prompt={prompt_version or ''}",
        ]
    )
    return "strategy_llm:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _session_finalizer_dedupe_key(
    *,
    group_id: int,
    session_id,
    session_no,
    source_run_id,
    state_code: str,
    start_message_id: int,
    end_message_id: int,
    evidence_ids: list[int],
    prompt_version,
) -> str:
    raw = "|".join(
        [
            "session_finalizer",
            f"g={group_id}",
            f"sid={session_id or ''}",
            f"sno={session_no or ''}",
            f"run={source_run_id or ''}",
            f"state={state_code}",
            f"range={start_message_id}-{end_message_id}",
            "evidence=" + ",".join(str(i) for i in evidence_ids),
            f"prompt={prompt_version or ''}",
        ]
    )
    return "session_finalizer:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _state_monitor_dedupe_key(
    *,
    group_id: int,
    session_id,
    session_no,
    source_run_id,
    assessment_id,
    state_code: str,
    start_message_id: int,
    end_message_id: int,
    evidence_ids: list[int],
) -> str:
    if assessment_id is not None:
        return f"state_monitor_assessment:{assessment_id}"
    raw = "|".join(
        [
            STATE_MONITOR_SOURCE,
            f"g={group_id}",
            f"sid={session_id or ''}",
            f"sno={session_no or ''}",
            f"run={source_run_id or ''}",
            f"state={state_code}",
            f"range={start_message_id}-{end_message_id}",
            "evidence=" + ",".join(str(i) for i in evidence_ids),
        ]
    )
    return "state_monitor:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _silence_dedupe_key(group_id: int, session_id, session_no, previous_student_message_id: int) -> str:
    return (
        "silence_rule:"
        f"g={group_id}:sid={session_id or ''}:sno={session_no or ''}:"
        f"prev={previous_student_message_id}"
    )


def _dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return parse_dt(value)
    return None


def _seconds_between(start, end) -> int:
    start_dt = _dt(start)
    end_dt = _dt(end)
    if not start_dt or not end_dt:
        raise SegmentValidationError("invalid_time_range")
    return max(0, int((end_dt - start_dt).total_seconds()))


def _validate_strategy_segments(
    conn,
    *,
    group_id: int,
    session_id,
    session_no,
    state_segments: list[dict],
) -> list[dict]:
    normalized = []
    needed_sequences = set()
    for item in state_segments or []:
        if not isinstance(item, dict):
            raise SegmentValidationError("invalid_segment")
        state_code = _normalize_message_state(item.get("state") or item.get("state_code"))
        start = _as_int(item.get("start_message_id"), "start_message_id")
        end = _as_int(item.get("end_message_id"), "end_message_id")
        if start > end:
            raise SegmentValidationError("invalid_message_range")
        evidence_ids = _normalize_evidence_ids(item.get("evidence_message_ids"))
        for evidence_id in evidence_ids:
            if evidence_id < start or evidence_id > end:
                raise SegmentValidationError("evidence_out_of_range")
        confidence = _as_confidence(item.get("confidence"))
        needed_sequences.update([start, end, *evidence_ids])
        normalized.append(
            {
                "state_code": state_code,
                "start_message_id": start,
                "end_message_id": end,
                "evidence_message_ids": evidence_ids,
                "confidence": confidence,
                "segment_order": item.get("segment_order"),
                "boundary_normalization": item.get("boundary_normalization"),
                "agent_message_sequences_inside_range": item.get(
                    "agent_message_sequences_inside_range"
                )
                or [],
            }
        )

    normalized.sort(key=lambda s: (s["start_message_id"], s["end_message_id"]))
    previous_end = None
    for segment in normalized:
        if previous_end is not None and segment["start_message_id"] <= previous_end:
            raise SegmentValidationError("overlapping_segments")
        previous_end = segment["end_message_id"]

    message_index = _load_messages_by_sequence(conn, group_id, needed_sequences)
    for sequence in needed_sequences:
        row = message_index.get(sequence)
        if not row:
            raise SegmentValidationError("message_reference_not_found")
        if not _message_matches_session(row, session_id, session_no):
            raise SegmentValidationError("message_cross_session")
        if not _is_student_message(row):
            raise SegmentValidationError("message_not_student")

    return normalized


def _parse_evidence_json(value) -> list[int]:
    try:
        parsed = json.loads(value or "[]")
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


def _merge_sorted_ints(*collections) -> list[int]:
    values = set()
    for collection in collections:
        for item in collection or []:
            values.add(_as_int(item, "evidence_message_id"))
    return sorted(values)


def _confidence_max(left, right):
    candidates = []
    for value in (left, right):
        if value is None:
            continue
        candidates.append(_as_confidence(value))
    if not candidates:
        return None
    return max(candidates)


def _has_student_messages_between(conn, *, group_id, session_id, session_no, start_exclusive, end_exclusive):
    if end_exclusive <= start_exclusive + 1:
        return False
    where = [
        "m.group_id=?",
        "m.sequence>?",
        "m.sequence<?",
        "COALESCE(NULLIF(TRIM(m.role), ''), NULLIF(TRIM(m.sender_type), ''), u.role)='student'",
    ]
    params = [group_id, start_exclusive, end_exclusive]
    if session_id is not None:
        where.append("m.session_id=?")
        params.append(session_id)
    elif session_no is not None:
        where.append("m.session_no=?")
        params.append(session_no)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM messages m
        LEFT JOIN users u ON u.id=m.user_id
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    ).fetchone()
    return bool(row and int(row["c"] or 0) > 0)


def _min_optional_int(*values):
    ints = [_as_int(value, "optional_int", allow_none=True) for value in values]
    ints = [value for value in ints if value is not None]
    return min(ints) if ints else None


def _max_optional_int(*values):
    ints = [_as_int(value, "optional_int", allow_none=True) for value in values]
    ints = [value for value in ints if value is not None]
    return max(ints) if ints else None


def _validate_monitor_message_segment(
    conn,
    *,
    group_id: int,
    session_id,
    session_no,
    state_code,
    start_message_id,
    end_message_id,
    evidence_message_ids,
    confidence,
) -> dict:
    normalized_state = _normalize_monitor_message_state(state_code)
    start = _as_int(start_message_id, "start_message_id")
    end = _as_int(end_message_id, "end_message_id")
    if start > end:
        raise SegmentValidationError("invalid_message_range")
    evidence_ids = _merge_sorted_ints(evidence_message_ids)
    if not evidence_ids:
        raise SegmentValidationError("missing_evidence")
    for evidence_id in evidence_ids:
        if evidence_id < start or evidence_id > end:
            raise SegmentValidationError("evidence_out_of_range")
    confidence_value = _as_confidence(confidence)

    needed_sequences = {start, end, *evidence_ids}
    message_index = _load_messages_by_sequence(conn, group_id, needed_sequences)
    for sequence in needed_sequences:
        row = message_index.get(sequence)
        if not row:
            raise SegmentValidationError("message_reference_not_found")
        if not _message_matches_session(row, session_id, session_no):
            raise SegmentValidationError("message_cross_session")
        if not _is_student_message(row):
            raise SegmentValidationError("message_not_student")
    return {
        "state_code": normalized_state,
        "start_message_id": start,
        "end_message_id": end,
        "evidence_message_ids": evidence_ids,
        "confidence": confidence_value,
    }


def _state_monitor_assessment_row(conn, *, group_id, session_id, session_no, assessment_id):
    if assessment_id is None:
        return None
    return conn.execute(
        f"""
        SELECT *
        FROM collaboration_state_segments
        WHERE group_id=?
          AND {_session_where()}
          AND source=?
          AND assessment_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            group_id,
            *_session_params(session_id, session_no),
            STATE_MONITOR_SOURCE,
            assessment_id,
        ),
    ).fetchone()


def _covering_state_monitor_row(conn, *, group_id, session_id, session_no, state_code, start, end):
    return conn.execute(
        f"""
        SELECT *
        FROM collaboration_state_segments
        WHERE group_id=?
          AND {_session_where()}
          AND source=?
          AND segment_kind='message_range'
          AND state_code=?
          AND start_message_id<=?
          AND end_message_id>=?
        ORDER BY end_message_id DESC, id DESC
        LIMIT 1
        """,
        (
            group_id,
            *_session_params(session_id, session_no),
            STATE_MONITOR_SOURCE,
            state_code,
            start,
            end,
        ),
    ).fetchone()


def _latest_state_monitor_message_row(conn, *, group_id, session_id, session_no):
    return conn.execute(
        f"""
        SELECT *
        FROM collaboration_state_segments
        WHERE group_id=?
          AND {_session_where()}
          AND source=?
          AND segment_kind='message_range'
          AND end_message_id IS NOT NULL
        ORDER BY end_message_id DESC, id DESC
        LIMIT 1
        """,
        (
            group_id,
            *_session_params(session_id, session_no),
            STATE_MONITOR_SOURCE,
        ),
    ).fetchone()


def _segment_from_row(row) -> dict:
    data = dict(row)
    try:
        data["evidence_message_ids"] = json.loads(data.get("evidence_message_ids_json") or "[]")
    except (TypeError, ValueError):
        data["evidence_message_ids"] = []
    data["is_finalized"] = bool(data.get("is_finalized"))
    data["is_active"] = bool(data.get("is_active"))
    return data


class CollaborationStateSegmentService:
    """Repository/service for normalized collaboration state segments."""

    @staticmethod
    def upsert_monitor_assessment_segment(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        task_id=None,
        discussion_id=None,
        state_code=None,
        start_message_id=None,
        end_message_id=None,
        evidence_message_ids=None,
        confidence=None,
        source_run_id=None,
        assessment_id=None,
        trigger_sequence=None,
        analysis_window_start_message_id=None,
        analysis_window_end_message_id=None,
    ) -> dict:
        """Persist a finalized teacher timeline segment owned by monitoring.

        This method is intentionally separate from strategy/finalizer writes:
        it keeps final detector state visible even when later intervention
        logic passes, cools down, fails to lock, or is disabled.
        """
        if state_code == "unknown":
            return {"skipped": True, "reason": "final_state_unknown", "saved_count": 0}
        if state_code == SILENCE_STATE:
            return {"skipped": True, "reason": "negative_silence_time_range_only", "saved_count": 0}

        group_id = _as_int(group_id, "group_id")
        session_id = _as_int(session_id, "session_id", allow_none=True)
        session_no = _as_int(session_no, "session_no", allow_none=True)
        task_id = _as_int(task_id, "task_id", allow_none=True)
        source_run_id = _as_int(source_run_id, "source_run_id", allow_none=True)
        assessment_id = _as_int(assessment_id, "assessment_id", allow_none=True)
        trigger_sequence = _as_int(
            trigger_sequence,
            "trigger_sequence",
            allow_none=True,
        )
        window_start = _as_int(
            analysis_window_start_message_id,
            "analysis_window_start_message_id",
            allow_none=True,
        )
        window_end = _as_int(
            analysis_window_end_message_id,
            "analysis_window_end_message_id",
            allow_none=True,
        )

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = _resolve_write_scope(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
                sequence=start_message_id,
            )
            session_id = scope.session_id
            session_no = scope.session_no
            task_id = scope.task_id
            discussion_id = scope.discussion_id
            segment = _validate_monitor_message_segment(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                state_code=state_code,
                start_message_id=start_message_id,
                end_message_id=end_message_id,
                evidence_message_ids=evidence_message_ids or [],
                confidence=confidence,
            )
            existing_assessment = _state_monitor_assessment_row(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                assessment_id=assessment_id,
            )
            if existing_assessment:
                conn.commit()
                return {
                    "skipped": False,
                    "saved": False,
                    "saved_count": 0,
                    "segment_id": int(existing_assessment["id"]),
                    "segment_ids": [int(existing_assessment["id"])],
                    "reason": "assessment_already_persisted",
                    "state_code": existing_assessment["state_code"],
                    "start_message_id": existing_assessment["start_message_id"],
                    "end_message_id": existing_assessment["end_message_id"],
                    "merged": False,
                    "range_type": "message_range",
                }

            covering = _covering_state_monitor_row(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                state_code=segment["state_code"],
                start=segment["start_message_id"],
                end=segment["end_message_id"],
            )
            if covering:
                conn.commit()
                return {
                    "skipped": False,
                    "saved": False,
                    "saved_count": 0,
                    "segment_id": int(covering["id"]),
                    "segment_ids": [int(covering["id"])],
                    "reason": "range_already_covered",
                    "state_code": covering["state_code"],
                    "start_message_id": covering["start_message_id"],
                    "end_message_id": covering["end_message_id"],
                    "merged": False,
                    "range_type": "message_range",
                }

            latest = _latest_state_monitor_message_row(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
            )
            now = now_str()
            if latest and latest["state_code"] == segment["state_code"]:
                latest_end = _as_int(latest["end_message_id"], "end_message_id")
                latest_start = _as_int(latest["start_message_id"], "start_message_id")
                if (
                    segment["start_message_id"] <= latest_end + 1
                    or not _has_student_messages_between(
                        conn,
                        group_id=group_id,
                        session_id=session_id,
                        session_no=session_no,
                        start_exclusive=latest_end,
                        end_exclusive=segment["start_message_id"],
                    )
                ):
                    merged_start = min(latest_start, segment["start_message_id"])
                    merged_end = max(latest_end, segment["end_message_id"])
                    merged_evidence = _merge_sorted_ints(
                        _parse_evidence_json(latest["evidence_message_ids_json"]),
                        segment["evidence_message_ids"],
                    )
                    merged_confidence = _confidence_max(latest["confidence"], segment["confidence"])
                    conn.execute(
                        """
                        UPDATE collaboration_state_segments
                        SET task_id=COALESCE(task_id, ?),
                            discussion_id=COALESCE(discussion_id, ?),
                            start_message_id=?,
                            end_message_id=?,
                            evidence_message_ids_json=?,
                            evidence_sequences=?,
                            confidence=?,
                            source_run_id=COALESCE(?, source_run_id),
                            assessment_id=COALESCE(assessment_id, ?),
                            trigger_sequence=COALESCE(?, trigger_sequence),
                            analysis_window_start_message_id=?,
                            analysis_window_end_message_id=?,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            task_id,
                            discussion_id,
                            merged_start,
                            merged_end,
                            _json_dumps(merged_evidence),
                            _json_dumps(merged_evidence),
                            merged_confidence,
                            source_run_id,
                            assessment_id,
                            trigger_sequence,
                            _min_optional_int(
                                latest["analysis_window_start_message_id"],
                                window_start,
                                merged_start,
                            ),
                            _max_optional_int(
                                latest["analysis_window_end_message_id"],
                                window_end,
                                merged_end,
                            ),
                            now,
                            latest["id"],
                        ),
                    )
                    conn.commit()
                    return {
                        "skipped": False,
                        "saved": True,
                        "saved_count": 1,
                        "segment_id": int(latest["id"]),
                        "segment_ids": [int(latest["id"])],
                        "state_code": segment["state_code"],
                        "start_message_id": merged_start,
                        "end_message_id": merged_end,
                        "merged": True,
                        "range_type": "message_range",
                    }

            dedupe_key = _state_monitor_dedupe_key(
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                source_run_id=source_run_id,
                assessment_id=assessment_id,
                state_code=segment["state_code"],
                start_message_id=segment["start_message_id"],
                end_message_id=segment["end_message_id"],
                evidence_ids=segment["evidence_message_ids"],
            )
            conn.execute(
                """
                INSERT INTO collaboration_state_segments(
                    group_id, session_id, session_no, task_id, discussion_id,
                    state_code, segment_kind, start_message_id, end_message_id,
                    start_at, end_at, evidence_message_ids_json,
                    evidence_sequences, confidence,
                    source, source_run_id, assessment_id,
                    analysis_anchor_message_id,
                    analysis_window_start_message_id,
                    analysis_window_end_message_id,
                    trigger_sequence,
                    previous_student_message_id, next_student_message_id,
                    gap_seconds, prompt_version, is_finalized,
                    dedupe_key, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    evidence_message_ids_json=excluded.evidence_message_ids_json,
                    evidence_sequences=excluded.evidence_sequences,
                    confidence=excluded.confidence,
                    source_run_id=COALESCE(excluded.source_run_id, collaboration_state_segments.source_run_id),
                    assessment_id=COALESCE(collaboration_state_segments.assessment_id, excluded.assessment_id),
                    trigger_sequence=COALESCE(excluded.trigger_sequence, collaboration_state_segments.trigger_sequence),
                    analysis_window_start_message_id=excluded.analysis_window_start_message_id,
                    analysis_window_end_message_id=excluded.analysis_window_end_message_id,
                    updated_at=excluded.updated_at
                """,
                (
                    group_id,
                    session_id,
                    session_no,
                    task_id,
                    discussion_id,
                    segment["state_code"],
                    "message_range",
                    segment["start_message_id"],
                    segment["end_message_id"],
                    None,
                    None,
                    _json_dumps(segment["evidence_message_ids"]),
                    _json_dumps(segment["evidence_message_ids"]),
                    segment["confidence"],
                    STATE_MONITOR_SOURCE,
                    source_run_id,
                    assessment_id,
                    segment["start_message_id"],
                    window_start if window_start is not None else segment["start_message_id"],
                    window_end if window_end is not None else segment["end_message_id"],
                    trigger_sequence,
                    None,
                    None,
                    None,
                    None,
                    1,
                    dedupe_key,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM collaboration_state_segments WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            conn.commit()
            segment_id = int(row["id"]) if row else None
            return {
                "skipped": False,
                "saved": True,
                "saved_count": 1,
                "segment_id": segment_id,
                "segment_ids": [segment_id] if segment_id is not None else [],
                "state_code": segment["state_code"],
                "start_message_id": segment["start_message_id"],
                "end_message_id": segment["end_message_id"],
                "merged": False,
                "range_type": "message_range",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def save_strategy_llm_segments(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        task_id=None,
        discussion_id=None,
        state_segments: list[dict] = None,
        source_run_id=None,
        analysis_anchor_message_id=None,
        analysis_window_start_message_id=None,
        analysis_window_end_message_id=None,
        prompt_version=None,
        assessment_id=None,
    ) -> dict:
        """Replace provisional strategy LLM segments for one analysis anchor."""
        group_id = _as_int(group_id, "group_id")
        session_id = _as_int(session_id, "session_id", allow_none=True)
        session_no = _as_int(session_no, "session_no", allow_none=True)
        task_id = _as_int(task_id, "task_id", allow_none=True)
        source_run_id = _as_int(source_run_id, "source_run_id", allow_none=True)
        assessment_id = _as_int(assessment_id, "assessment_id", allow_none=True)
        anchor = _as_int(
            analysis_anchor_message_id,
            "analysis_anchor_message_id",
            allow_none=True,
        )
        window_start = _as_int(
            analysis_window_start_message_id,
            "analysis_window_start_message_id",
            allow_none=True,
        )
        window_end = _as_int(
            analysis_window_end_message_id,
            "analysis_window_end_message_id",
            allow_none=True,
        )
        if anchor is None and state_segments:
            anchor = min(_as_int(seg.get("start_message_id"), "start_message_id") for seg in state_segments)
        if anchor is None:
            return {"saved_count": 0, "deleted_count": 0, "anchor": None}

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = _resolve_write_scope(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
                sequence=anchor,
            )
            session_id = scope.session_id
            session_no = scope.session_no
            task_id = scope.task_id
            discussion_id = scope.discussion_id
            normalized = _validate_strategy_segments(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                state_segments=state_segments or [],
            )
            delete_sql = f"""
                DELETE FROM collaboration_state_segments
                WHERE group_id=?
                  AND {_session_where()}
                  AND source='strategy_llm'
                  AND analysis_anchor_message_id=?
                  AND is_finalized=0
            """
            cur = conn.execute(
                delete_sql,
                (group_id, *_session_params(session_id, session_no), anchor),
            )
            deleted_count = cur.rowcount
            now = now_str()
            saved_count = 0
            for segment in normalized:
                evidence_ids = segment["evidence_message_ids"]
                dedupe_key = _strategy_dedupe_key(
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    anchor=anchor,
                    source_run_id=source_run_id,
                    state_code=segment["state_code"],
                    start_message_id=segment["start_message_id"],
                    end_message_id=segment["end_message_id"],
                    evidence_ids=evidence_ids,
                    prompt_version=prompt_version,
                )
                cur = conn.execute(
                    """
                    INSERT INTO collaboration_state_segments(
                        group_id, session_id, session_no, task_id, discussion_id,
                        state_code, segment_kind, start_message_id, end_message_id,
                        start_at, end_at, evidence_message_ids_json, confidence,
                        source, source_run_id, assessment_id,
                        analysis_anchor_message_id,
                        analysis_window_start_message_id,
                        analysis_window_end_message_id,
                        previous_student_message_id, next_student_message_id,
                        gap_seconds, prompt_version, is_finalized,
                        dedupe_key, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dedupe_key) DO UPDATE SET
                        evidence_message_ids_json=excluded.evidence_message_ids_json,
                        confidence=excluded.confidence,
                        source_run_id=excluded.source_run_id,
                        assessment_id=excluded.assessment_id,
                        analysis_window_start_message_id=excluded.analysis_window_start_message_id,
                        analysis_window_end_message_id=excluded.analysis_window_end_message_id,
                        prompt_version=excluded.prompt_version,
                        updated_at=excluded.updated_at
                    WHERE collaboration_state_segments.is_finalized=0
                    """,
                    (
                        group_id,
                        session_id,
                        session_no,
                        task_id,
                        discussion_id,
                        segment["state_code"],
                        "message_range",
                        segment["start_message_id"],
                        segment["end_message_id"],
                        None,
                        None,
                        _json_dumps(evidence_ids),
                        segment["confidence"],
                        "strategy_llm",
                        source_run_id,
                        assessment_id,
                        anchor,
                        window_start,
                        window_end,
                        None,
                        None,
                        None,
                        prompt_version,
                        0,
                        dedupe_key,
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    saved_count += 1
            segment_rows = conn.execute(
                f"""
                SELECT id
                FROM collaboration_state_segments
                WHERE group_id=?
                  AND {_session_where()}
                  AND source='strategy_llm'
                  AND analysis_anchor_message_id=?
                ORDER BY id ASC
                """,
                (group_id, *_session_params(session_id, session_no), anchor),
            ).fetchall()
            conn.commit()
            return {
                "saved_count": saved_count,
                "deleted_count": deleted_count,
                "anchor": anchor,
                "segment_ids": [int(row["id"]) for row in segment_rows],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def delete_provisional_for_anchor(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        analysis_anchor_message_id: int,
    ) -> int:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"""
                DELETE FROM collaboration_state_segments
                WHERE group_id=?
                  AND {_session_where()}
                  AND source='strategy_llm'
                  AND analysis_anchor_message_id=?
                  AND is_finalized=0
                """,
                (
                    _as_int(group_id, "group_id"),
                    *_session_params(
                        _as_int(session_id, "session_id", allow_none=True),
                        _as_int(session_no, "session_no", allow_none=True),
                    ),
                    _as_int(analysis_anchor_message_id, "analysis_anchor_message_id"),
                ),
            )
            conn.commit()
            return cur.rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def save_finalization_segments(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        task_id=None,
        discussion_id=None,
        state_segments: list[dict] = None,
        source_run_id=None,
        analysis_anchor_message_id=None,
        analysis_window_start_message_id=None,
        analysis_window_end_message_id=None,
        prompt_version=None,
        assessment_id=None,
    ) -> dict:
        """Persist finalized message_range segments from the session finalizer."""
        group_id = _as_int(group_id, "group_id")
        session_id = _as_int(session_id, "session_id", allow_none=True)
        session_no = _as_int(session_no, "session_no", allow_none=True)
        task_id = _as_int(task_id, "task_id", allow_none=True)
        source_run_id = _as_int(source_run_id, "source_run_id", allow_none=True)
        assessment_id = _as_int(assessment_id, "assessment_id", allow_none=True)
        anchor = _as_int(
            analysis_anchor_message_id,
            "analysis_anchor_message_id",
            allow_none=True,
        )
        window_start = _as_int(
            analysis_window_start_message_id,
            "analysis_window_start_message_id",
            allow_none=True,
        )
        window_end = _as_int(
            analysis_window_end_message_id,
            "analysis_window_end_message_id",
            allow_none=True,
        )
        if anchor is None and state_segments:
            anchor = min(_as_int(seg.get("start_message_id"), "start_message_id") for seg in state_segments)

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = _resolve_write_scope(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
                sequence=anchor,
            )
            session_id = scope.session_id
            session_no = scope.session_no
            task_id = scope.task_id
            discussion_id = scope.discussion_id
            normalized = _validate_strategy_segments(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                state_segments=state_segments or [],
            )
            deleted_count = 0
            if anchor is not None:
                cur = conn.execute(
                    f"""
                    DELETE FROM collaboration_state_segments
                    WHERE group_id=?
                      AND {_session_where()}
                      AND source='strategy_llm'
                      AND analysis_anchor_message_id=?
                      AND is_finalized=0
                    """,
                    (group_id, *_session_params(session_id, session_no), anchor),
                )
                deleted_count = cur.rowcount

            now = now_str()
            saved_count = 0
            saved_segments = []
            for segment in normalized:
                evidence_ids = segment["evidence_message_ids"]
                dedupe_key = _session_finalizer_dedupe_key(
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    source_run_id=source_run_id,
                    state_code=segment["state_code"],
                    start_message_id=segment["start_message_id"],
                    end_message_id=segment["end_message_id"],
                    evidence_ids=evidence_ids,
                    prompt_version=prompt_version,
                )
                cur = conn.execute(
                    """
                    INSERT INTO collaboration_state_segments(
                        group_id, session_id, session_no, task_id, discussion_id,
                        state_code, segment_kind, start_message_id, end_message_id,
                        start_at, end_at, evidence_message_ids_json,
                        evidence_sequences, confidence,
                        source, source_run_id, assessment_id,
                        analysis_anchor_message_id,
                        analysis_window_start_message_id,
                        analysis_window_end_message_id,
                        previous_student_message_id, next_student_message_id,
                        gap_seconds, prompt_version, segment_order, is_finalized,
                        dedupe_key, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dedupe_key) DO UPDATE SET
                        evidence_message_ids_json=excluded.evidence_message_ids_json,
                        evidence_sequences=excluded.evidence_sequences,
                        confidence=excluded.confidence,
                        assessment_id=excluded.assessment_id,
                        analysis_anchor_message_id=excluded.analysis_anchor_message_id,
                        analysis_window_start_message_id=excluded.analysis_window_start_message_id,
                        analysis_window_end_message_id=excluded.analysis_window_end_message_id,
                        prompt_version=excluded.prompt_version,
                        segment_order=excluded.segment_order,
                        is_finalized=1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        group_id,
                        session_id,
                        session_no,
                        task_id,
                        discussion_id,
                        segment["state_code"],
                        "message_range",
                        segment["start_message_id"],
                        segment["end_message_id"],
                        None,
                        None,
                        _json_dumps(evidence_ids),
                        _json_dumps(evidence_ids),
                        segment["confidence"],
                        "session_finalizer",
                        source_run_id,
                        assessment_id,
                        anchor,
                        window_start,
                        window_end,
                        None,
                        None,
                        None,
                        prompt_version,
                        segment.get("segment_order"),
                        1,
                        dedupe_key,
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    saved_count += 1
                saved_row = conn.execute(
                    "SELECT id FROM collaboration_state_segments WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone()
                if saved_row:
                    saved_segments.append(
                        {
                            "segment_id": int(saved_row["id"]),
                            "segment_order": segment.get("segment_order"),
                            "state": segment["state_code"],
                            "start_message_id": segment["start_message_id"],
                            "end_message_id": segment["end_message_id"],
                            "evidence_message_ids": evidence_ids,
                            "boundary_normalization": segment.get(
                                "boundary_normalization"
                            ),
                            "agent_message_sequences_inside_range": segment.get(
                                "agent_message_sequences_inside_range"
                            )
                            or [],
                        }
                    )
            conn.commit()
            return {
                "saved_count": saved_count,
                "saved_segment_ids": [
                    item["segment_id"] for item in saved_segments
                ],
                "saved": saved_segments,
                "deleted_count": deleted_count,
                "anchor": anchor,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def finalize_anchor(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        analysis_anchor_message_id: int,
    ) -> int:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"""
                UPDATE collaboration_state_segments
                SET is_finalized=1, updated_at=?
                WHERE group_id=?
                  AND {_session_where()}
                  AND source='strategy_llm'
                  AND analysis_anchor_message_id=?
                  AND is_finalized=0
                """,
                (
                    now_str(),
                    _as_int(group_id, "group_id"),
                    *_session_params(
                        _as_int(session_id, "session_id", allow_none=True),
                        _as_int(session_no, "session_no", allow_none=True),
                    ),
                    _as_int(analysis_anchor_message_id, "analysis_anchor_message_id"),
                ),
            )
            conn.commit()
            return cur.rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def save_or_update_silence_interval(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        task_id=None,
        discussion_id=None,
        previous_student_message_id: int,
        start_at: str,
        end_at: str = None,
        source_run_id=None,
        assessment_id=None,
        next_student_message_id=None,
        is_finalized: bool = False,
        trigger_sequence=None,
        raw_silence_started_at=None,
        threshold_reached_at=None,
        detected_at=None,
        last_observed_at=None,
        silent_seconds_at_detection=None,
        resolution_reason=None,
    ) -> dict:
        group_id = _as_int(group_id, "group_id")
        session_id = _as_int(session_id, "session_id", allow_none=True)
        session_no = _as_int(session_no, "session_no", allow_none=True)
        task_id = _as_int(task_id, "task_id", allow_none=True)
        previous_student_message_id = _as_int(previous_student_message_id, "previous_student_message_id")
        next_student_message_id = _as_int(
            next_student_message_id,
            "next_student_message_id",
            allow_none=True,
        )
        source_run_id = _as_int(source_run_id, "source_run_id", allow_none=True)
        assessment_id = _as_int(assessment_id, "assessment_id", allow_none=True)
        trigger_sequence = _as_int(
            trigger_sequence
            if trigger_sequence is not None
            else previous_student_message_id,
            "trigger_sequence",
        )
        raw_silence_started_at = raw_silence_started_at or start_at
        threshold_reached_at = threshold_reached_at or start_at
        observation_at = last_observed_at or end_at or detected_at or now_str()
        detected_at = detected_at or observation_at
        if silent_seconds_at_detection is None:
            silent_seconds_at_detection = _seconds_between(
                raw_silence_started_at,
                detected_at,
            )
        else:
            silent_seconds_at_detection = _as_int(
                silent_seconds_at_detection,
                "silent_seconds_at_detection",
            )
        if is_finalized and not end_at:
            raise SegmentValidationError("missing_silence_end")
        stored_end_at = end_at if is_finalized else None
        gap_seconds = _seconds_between(start_at, end_at or observation_at)
        is_active = 0 if is_finalized else 1
        dedupe_key = _silence_dedupe_key(
            group_id,
            session_id,
            session_no,
            previous_student_message_id,
        )
        silence_event_key = dedupe_key
        now = now_str()
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = _resolve_write_scope(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
                sequence=previous_student_message_id,
            )
            session_id = scope.session_id
            session_no = scope.session_no
            task_id = scope.task_id
            discussion_id = scope.discussion_id
            prev = _load_messages_by_sequence(conn, group_id, {previous_student_message_id}).get(previous_student_message_id)
            if not prev or not _is_student_message(prev):
                raise SegmentValidationError("previous_message_not_student")
            if not _message_matches_session(prev, session_id, session_no):
                raise SegmentValidationError("previous_message_cross_session")
            if next_student_message_id is not None:
                nxt = _load_messages_by_sequence(conn, group_id, {next_student_message_id}).get(next_student_message_id)
                if not nxt or not _is_student_message(nxt):
                    raise SegmentValidationError("next_message_not_student")
                if not _message_matches_session(nxt, session_id, session_no):
                    raise SegmentValidationError("next_message_cross_session")
            cur = conn.execute(
                """
                INSERT INTO collaboration_state_segments(
                    group_id, session_id, session_no, task_id, discussion_id,
                    state_code, segment_kind, start_message_id, end_message_id,
                    start_at, end_at, trigger_sequence,
                    raw_silence_started_at, threshold_reached_at,
                    detected_at, last_observed_at, silent_seconds_at_detection,
                    is_active, resolution_reason,
                    silence_event_key,
                    evidence_message_ids_json, confidence,
                    source, source_run_id, assessment_id,
                    analysis_anchor_message_id,
                    analysis_window_start_message_id,
                    analysis_window_end_message_id,
                    previous_student_message_id, next_student_message_id,
                    gap_seconds, prompt_version, is_finalized,
                    dedupe_key, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    end_at=CASE
                        WHEN excluded.is_finalized=1 THEN excluded.end_at
                        ELSE collaboration_state_segments.end_at
                    END,
                    last_observed_at=CASE
                        WHEN collaboration_state_segments.last_observed_at IS NULL
                          OR excluded.last_observed_at>collaboration_state_segments.last_observed_at
                        THEN excluded.last_observed_at
                        ELSE collaboration_state_segments.last_observed_at
                    END,
                    source_run_id=COALESCE(excluded.source_run_id, collaboration_state_segments.source_run_id),
                    assessment_id=COALESCE(collaboration_state_segments.assessment_id, excluded.assessment_id),
                    next_student_message_id=COALESCE(excluded.next_student_message_id, collaboration_state_segments.next_student_message_id),
                    gap_seconds=CASE
                        WHEN excluded.is_finalized=1 THEN excluded.gap_seconds
                        WHEN excluded.gap_seconds>collaboration_state_segments.gap_seconds
                        THEN excluded.gap_seconds
                        ELSE collaboration_state_segments.gap_seconds
                    END,
                    is_finalized=MAX(collaboration_state_segments.is_finalized, excluded.is_finalized),
                    is_active=CASE
                        WHEN excluded.is_finalized=1 THEN 0
                        ELSE collaboration_state_segments.is_active
                    END,
                    resolution_reason=COALESCE(excluded.resolution_reason, collaboration_state_segments.resolution_reason),
                    silence_event_key=COALESCE(
                        collaboration_state_segments.silence_event_key,
                        excluded.silence_event_key
                    ),
                    updated_at=excluded.updated_at
                WHERE collaboration_state_segments.is_finalized=0
                   OR excluded.is_finalized=1
                """,
                (
                    group_id,
                    session_id,
                    session_no,
                    task_id,
                    discussion_id,
                    SILENCE_STATE,
                    "time_range",
                    None,
                    None,
                    start_at,
                    stored_end_at,
                    trigger_sequence,
                    raw_silence_started_at,
                    threshold_reached_at,
                    detected_at,
                    observation_at,
                    silent_seconds_at_detection,
                    is_active,
                    resolution_reason,
                    silence_event_key,
                    "[]",
                    None,
                    "silence_rule",
                    source_run_id,
                    assessment_id,
                    None,
                    None,
                    None,
                    previous_student_message_id,
                    next_student_message_id,
                    gap_seconds,
                    None,
                    1 if is_finalized else 0,
                    dedupe_key,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, start_at, end_at, detected_at, last_observed_at,
                       gap_seconds, is_active, is_finalized, silence_event_key,
                       intervention_scheduled_at, intervention_run_id,
                       intervention_published_at, intervention_disposition
                FROM collaboration_state_segments
                WHERE dedupe_key=?
                """,
                (dedupe_key,),
            ).fetchone()
            conn.commit()
            return {
                "saved": bool(cur.rowcount),
                "segment_id": int(row["id"]) if row else None,
                "dedupe_key": dedupe_key,
                "silence_event_key": (
                    row["silence_event_key"] if row else silence_event_key
                ),
                "start_at": row["start_at"] if row else start_at,
                "end_at": row["end_at"] if row else stored_end_at,
                "detected_at": row["detected_at"] if row else detected_at,
                "last_observed_at": (
                    row["last_observed_at"] if row else observation_at
                ),
                "gap_seconds": int(row["gap_seconds"] or 0) if row else gap_seconds,
                "is_active": bool(row["is_active"]) if row else bool(is_active),
                "is_finalized": (
                    bool(row["is_finalized"]) if row else bool(is_finalized)
                ),
                "intervention_scheduled_at": (
                    row["intervention_scheduled_at"] if row else None
                ),
                "intervention_run_id": (
                    row["intervention_run_id"] if row else None
                ),
                "intervention_published_at": (
                    row["intervention_published_at"] if row else None
                ),
                "intervention_disposition": (
                    row["intervention_disposition"] if row else None
                ),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def latest_student_sequence(group_id: int, *, session_id=None, session_no=None) -> Optional[int]:
        params = [_as_int(group_id, "group_id")]
        where = ["group_id=?", "sequence IS NOT NULL", "COALESCE(role, '')='student'"]
        if session_id is not None:
            where.append("session_id=?")
            params.append(session_id)
        elif session_no is not None:
            where.append("session_no=?")
            params.append(session_no)
        row = query_one(
            f"""
            SELECT sequence FROM messages
            WHERE {' AND '.join(where)}
            ORDER BY sequence DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        return int(row["sequence"]) if row and row["sequence"] is not None else None

    @staticmethod
    def claim_silence_intervention(
        *,
        segment_id: int,
        monitor_run_id: int = None,
        now_value=None,
    ) -> dict:
        """Atomically claim the one strategy enqueue allowed for a silence event."""
        segment_id = _as_int(segment_id, "segment_id")
        monitor_run_id = _as_int(
            monitor_run_id,
            "monitor_run_id",
            allow_none=True,
        )
        claimed_at = (
            now_value.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(now_value, datetime)
            else (now_value or now_str())
        )
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, silence_event_key, dedupe_key, is_active, is_finalized,
                       intervention_scheduled_at, intervention_disposition
                FROM collaboration_state_segments
                WHERE id=? AND state_code=? AND source='silence_rule'
                """,
                (segment_id, SILENCE_STATE),
            ).fetchone()
            if not row:
                conn.rollback()
                return {
                    "claimed": False,
                    "reason": "silence_segment_not_found",
                    "segment_id": segment_id,
                }
            if not bool(row["is_active"]) or bool(row["is_finalized"]):
                conn.rollback()
                return {
                    "claimed": False,
                    "reason": "silence_event_no_longer_active",
                    "segment_id": segment_id,
                    "silence_event_key": row["silence_event_key"] or row["dedupe_key"],
                }
            if row["intervention_scheduled_at"]:
                conn.rollback()
                return {
                    "claimed": False,
                    "reason": "silence_intervention_already_scheduled",
                    "segment_id": segment_id,
                    "silence_event_key": row["silence_event_key"] or row["dedupe_key"],
                    "intervention_scheduled_at": row["intervention_scheduled_at"],
                    "intervention_disposition": row["intervention_disposition"],
                }
            event_key = row["silence_event_key"] or row["dedupe_key"]
            cur = conn.execute(
                """
                UPDATE collaboration_state_segments
                SET silence_event_key=COALESCE(silence_event_key, ?),
                    intervention_scheduled_at=?,
                    intervention_disposition='ENQUEUE_PENDING',
                    source_run_id=COALESCE(source_run_id, ?),
                    updated_at=?
                WHERE id=? AND intervention_scheduled_at IS NULL
                  AND is_active=1 AND is_finalized=0
                """,
                (
                    event_key,
                    claimed_at,
                    monitor_run_id,
                    claimed_at,
                    segment_id,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {
                    "claimed": False,
                    "reason": "silence_intervention_already_scheduled",
                    "segment_id": segment_id,
                    "silence_event_key": event_key,
                }
            conn.commit()
            return {
                "claimed": True,
                "reason": "silence_intervention_claimed",
                "segment_id": segment_id,
                "silence_event_key": event_key,
                "intervention_scheduled_at": claimed_at,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def record_silence_intervention_disposition(
        *,
        segment_id: int,
        disposition: str,
        intervention_run_id: int = None,
        published_at=None,
        clear_schedule_claim: bool = False,
    ) -> dict:
        """Update silence-event scheduling/run audit without changing its lifecycle."""
        segment_id = _as_int(segment_id, "segment_id")
        intervention_run_id = _as_int(
            intervention_run_id,
            "intervention_run_id",
            allow_none=True,
        )
        disposition = str(disposition or "UNKNOWN")[:120]
        published_text = (
            published_at.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(published_at, datetime)
            else published_at
        )
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE collaboration_state_segments
                SET intervention_disposition=CASE
                        WHEN intervention_disposition IN ('PUBLISHED', 'PASS')
                        THEN intervention_disposition
                        ELSE ?
                    END,
                    intervention_run_id=COALESCE(?, intervention_run_id),
                    intervention_published_at=COALESCE(?, intervention_published_at),
                    intervention_scheduled_at=CASE
                        WHEN intervention_disposition IN ('PUBLISHED', 'PASS')
                        THEN intervention_scheduled_at
                        WHEN ?=1 THEN NULL
                        ELSE intervention_scheduled_at
                    END,
                    updated_at=?
                WHERE id=? AND state_code=? AND source='silence_rule'
                """,
                (
                    disposition,
                    intervention_run_id,
                    published_text,
                    1 if clear_schedule_claim else 0,
                    now_str(),
                    segment_id,
                    SILENCE_STATE,
                ),
            )
            row = conn.execute(
                """
                SELECT intervention_scheduled_at, intervention_run_id,
                       intervention_published_at, intervention_disposition
                FROM collaboration_state_segments
                WHERE id=?
                """,
                (segment_id,),
            ).fetchone()
            conn.commit()
            return {
                "updated": cur.rowcount == 1,
                "segment_id": segment_id,
                **(dict(row) if row else {}),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def record_negative_silence_if_applicable(
        *,
        group_id: int,
        expected_sequence: int = None,
        expected_last_student_message_at=None,
        expected_session_id=None,
        expected_task_id=None,
        source_run_id=None,
        assessment_id=None,
        now_value=None,
    ) -> dict:
        """Create or monotonically extend an open negative-silence time range."""
        group_id = _as_int(group_id, "group_id")
        expected_sequence = _as_int(expected_sequence, "expected_sequence", allow_none=True)
        expected_session_id = _as_int(
            expected_session_id,
            "expected_session_id",
            allow_none=True,
        )
        expected_task_id = _as_int(
            expected_task_id,
            "expected_task_id",
            allow_none=True,
        )
        if isinstance(expected_last_student_message_at, datetime):
            expected_last_student_message_at = expected_last_student_message_at.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        now_text = now_value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(now_value, datetime) else (now_value or now_str())

        conn = db()
        try:
            group = conn.execute(
                "SELECT id, state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
                (group_id,),
            ).fetchone()
            if not group:
                return {"skipped": True, "reason": "group_not_found"}
            if group["state"] == "CLOSED":
                return {"skipped": True, "reason": "session_not_active"}

            prev = CollaborationStateSegmentService._latest_student_message_for_silence(
                conn,
                group_id,
            )
            if not prev:
                return {"skipped": True, "reason": "no_previous_student_message"}
            stale_fields = {}
            if expected_sequence is not None and int(prev["sequence"]) != expected_sequence:
                stale_fields["actual_sequence"] = int(prev["sequence"])
            if (
                expected_last_student_message_at is not None
                and str(prev["created_at"]) != str(expected_last_student_message_at)
            ):
                stale_fields["actual_last_student_message_at"] = prev["created_at"]
            if (
                expected_session_id is not None
                and (
                    prev["session_id"] is None
                    or int(prev["session_id"]) != expected_session_id
                )
            ):
                stale_fields["actual_session_id"] = prev["session_id"]
            if (
                expected_task_id is not None
                and (
                    prev["task_id"] is None
                    or int(prev["task_id"]) != expected_task_id
                )
            ):
                stale_fields["actual_task_id"] = prev["task_id"]
            if stale_fields:
                return {
                    "skipped": True,
                    "reason": "stale_silence_task",
                    "expected_last_student_sequence": expected_sequence,
                    "expected_last_student_message_at": expected_last_student_message_at,
                    "expected_session_id": expected_session_id,
                    "expected_task_id": expected_task_id,
                    **stale_fields,
                }

            session_id = prev["session_id"]
            session_no = prev["session_no"]
            task_id = prev["task_id"]
            if not session_id:
                try:
                    from db import get_current_running_session_context
                    current = get_current_running_session_context()
                    if current and (
                        session_no is None
                        or str(current.get("session_no")) == str(session_no)
                    ):
                        session_id = current.get("session_id")
                        session_no = session_no if session_no is not None else current.get("session_no")
                        task_id = task_id if task_id is not None else current.get("task_id")
                except Exception:
                    pass
            gate = CollaborationStateSegmentService._silence_gate(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                now_text=now_text,
            )
            if not gate["allowed"]:
                return {"skipped": True, "reason": gate["reason"]}

            silent_seconds = _seconds_between(prev["created_at"], now_text)
            threshold_seconds = int(ONLINE_SILENCE_NO_MSG_SECONDS or 0)
            if silent_seconds < threshold_seconds:
                return {
                    "skipped": True,
                    "reason": "below_silence_threshold",
                    "silent_seconds": silent_seconds,
                }
            threshold_reached_at = (
                _dt(prev["created_at"]) + timedelta(seconds=threshold_seconds)
            ).strftime("%Y-%m-%d %H:%M:%S")
        finally:
            conn.close()

        saved = CollaborationStateSegmentService.save_or_update_silence_interval(
            group_id=group_id,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            previous_student_message_id=int(prev["sequence"]),
            trigger_sequence=int(prev["sequence"]),
            start_at=threshold_reached_at,
            end_at=None,
            raw_silence_started_at=prev["created_at"],
            threshold_reached_at=threshold_reached_at,
            detected_at=now_text,
            last_observed_at=now_text,
            silent_seconds_at_detection=silent_seconds,
            source_run_id=source_run_id,
            assessment_id=assessment_id,
            is_finalized=False,
        )
        return {
            "skipped": False,
            "state_code": SILENCE_STATE,
            "range_type": "time_range",
            "trigger_sequence": int(prev["sequence"]),
            "silent_seconds": silent_seconds,
            **saved,
        }

    @staticmethod
    def _latest_student_message_for_silence(conn, group_id: int):
        return conn.execute(
            """
            SELECT sequence, created_at, session_id, session_no, task_id
            FROM messages
            WHERE group_id=?
              AND sequence IS NOT NULL
              AND COALESCE(role, '')='student'
            ORDER BY sequence DESC, id DESC
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()

    @staticmethod
    def _silence_gate(
        conn,
        *,
        group_id: int,
        session_id,
        session_no,
        task_id,
        now_text: str,
    ) -> dict:
        if not session_id:
            return {"allowed": False, "reason": "session_missing"}
        session = conn.execute(
            "SELECT id, status FROM experiment_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if not session or session["status"] != "running":
            return {"allowed": False, "reason": "session_not_running"}

        control = conn.execute(
            """
            SELECT agent_paused, session_paused
            FROM group_session_controls
            WHERE group_id=? AND session_id=?
            """,
            (group_id, session_id),
        ).fetchone()
        if control and (control["agent_paused"] or control["session_paused"]):
            return {"allowed": False, "reason": "teacher_pause"}

        runtime = conn.execute(
            """
            SELECT status, started_at, deadline
            FROM group_session_discussions
            WHERE session_id=? AND group_id=?
            """,
            (session_id, group_id),
        ).fetchone()
        if not runtime or runtime["status"] != "running" or not runtime["started_at"]:
            return {"allowed": False, "reason": "discussion_not_running"}
        deadline_dt = _dt(runtime["deadline"])
        now_dt = _dt(now_text)
        if deadline_dt and now_dt and now_dt >= deadline_dt:
            return {"allowed": False, "reason": "discussion_closed"}

        if task_id is not None and session_no is not None:
            doc = conn.execute(
                """
                SELECT status, submitted_at
                FROM collaborative_documents
                WHERE group_id=? AND task_id=? AND session_no=?
                ORDER BY id DESC LIMIT 1
                """,
                (group_id, task_id, session_no),
            ).fetchone()
        else:
            doc = conn.execute(
                """
                SELECT status, submitted_at
                FROM collaborative_documents
                WHERE group_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (group_id,),
            ).fetchone()
        if doc:
            locked_statuses = {"submitted", "locked", "frozen", "closed", "submitting"}
            if doc["submitted_at"] is not None or doc["status"] in locked_statuses:
                return {"allowed": False, "reason": "task_frozen"}
        return {"allowed": True, "reason": "active"}

    @staticmethod
    def close_open_silence_on_student_message(
        *,
        message_id: int = None,
        group_id: int = None,
        sequence: int = None,
    ) -> dict:
        """Finalize the latest provisional silence interval when a student speaks."""
        conn = db()
        try:
            if message_id is not None:
                msg = conn.execute(
                    """
                    SELECT id, group_id, sequence, role, sender_type, session_id,
                           session_no, task_id, created_at
                    FROM messages
                    WHERE id=?
                    """,
                    (_as_int(message_id, "message_id"),),
                ).fetchone()
            else:
                msg = conn.execute(
                    """
                    SELECT id, group_id, sequence, role, sender_type, session_id,
                           session_no, task_id, created_at
                    FROM messages
                    WHERE group_id=? AND sequence=?
                    """,
                    (_as_int(group_id, "group_id"), _as_int(sequence, "sequence")),
                ).fetchone()
            if not msg:
                return {"skipped": True, "reason": "message_not_found"}
            msg = dict(msg)
            if not _is_student_message(msg):
                return {"skipped": True, "reason": "message_not_student"}
            if msg.get("sequence") is None:
                return {"skipped": True, "reason": "message_without_sequence"}
            where = f"""
                group_id=?
                AND {_session_where()}
                AND state_code=?
                AND segment_kind='time_range'
                AND source='silence_rule'
                AND is_active=1
                AND is_finalized=0
                AND previous_student_message_id IS NOT NULL
                AND previous_student_message_id<>?
            """
            params = (
                msg["group_id"],
                *_session_params(msg.get("session_id"), msg.get("session_no")),
                SILENCE_STATE,
                int(msg["sequence"]),
            )
            row = conn.execute(
                f"""
                SELECT id, start_at, previous_student_message_id
                FROM collaboration_state_segments
                WHERE {where}
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                return {"skipped": True, "reason": "no_open_silence"}
            gap_seconds = _seconds_between(row["start_at"], msg["created_at"])
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE collaboration_state_segments
                SET end_at=?, end_sequence=?, next_student_message_id=?,
                    last_observed_at=?, gap_seconds=?, is_active=0,
                    is_finalized=1,
                    resolution_reason='student_message_resumed',
                    updated_at=?
                WHERE id=? AND is_active=1 AND is_finalized=0
                """,
                (
                    msg["created_at"],
                    int(msg["sequence"]),
                    int(msg["sequence"]),
                    msg["created_at"],
                    gap_seconds,
                    now_str(),
                    row["id"],
                ),
            )
            conn.commit()
            return {
                "closed": cur.rowcount > 0,
                "segment_id": row["id"],
                "next_student_message_id": int(msg["sequence"]),
                "gap_seconds": gap_seconds,
                "is_active": False,
                "resolution_reason": "student_message_resumed",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def list_segments(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        include_provisional: bool = True,
    ) -> list[dict]:
        where = ["group_id=?", _session_where()]
        params = [
            _as_int(group_id, "group_id"),
            *_session_params(
                _as_int(session_id, "session_id", allow_none=True),
                _as_int(session_no, "session_no", allow_none=True),
            ),
        ]
        if not include_provisional:
            where.append("is_finalized=1")
        rows = query_all(
            f"""
            SELECT * FROM collaboration_state_segments
            WHERE {' AND '.join(where)}
            ORDER BY
                COALESCE(start_message_id, previous_student_message_id, 0),
                COALESCE(start_at, created_at),
                id
            """,
            tuple(params),
        )
        return [_segment_from_row(row) for row in rows]

    @staticmethod
    def get_message_state(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
        message_id: int,
        include_provisional: bool = True,
    ) -> Optional[dict]:
        where = [
            "group_id=?",
            _session_where(),
            "segment_kind='message_range'",
            "start_message_id<=?",
            "end_message_id>=?",
        ]
        params = [
            _as_int(group_id, "group_id"),
            *_session_params(
                _as_int(session_id, "session_id", allow_none=True),
                _as_int(session_no, "session_no", allow_none=True),
            ),
            _as_int(message_id, "message_id"),
            _as_int(message_id, "message_id"),
        ]
        if not include_provisional:
            where.append("is_finalized=1")
        row = query_one(
            f"""
            SELECT * FROM collaboration_state_segments
            WHERE {' AND '.join(where)}
            ORDER BY is_finalized DESC, updated_at DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        return _segment_from_row(row) if row else None

    @staticmethod
    def get_latest_unfinalized_analysis_range(
        *,
        group_id: int,
        session_id=None,
        session_no=None,
    ) -> Optional[dict]:
        row = query_one(
            f"""
            SELECT analysis_anchor_message_id,
                   MIN(analysis_window_start_message_id) AS analysis_window_start_message_id,
                   MAX(analysis_window_end_message_id) AS analysis_window_end_message_id,
                   MAX(updated_at) AS updated_at,
                   COUNT(*) AS segment_count
            FROM collaboration_state_segments
            WHERE group_id=?
              AND {_session_where()}
              AND source='strategy_llm'
              AND is_finalized=0
              AND analysis_anchor_message_id IS NOT NULL
            GROUP BY analysis_anchor_message_id
            ORDER BY updated_at DESC, analysis_anchor_message_id DESC
            LIMIT 1
            """,
            (
                _as_int(group_id, "group_id"),
                *_session_params(
                    _as_int(session_id, "session_id", allow_none=True),
                    _as_int(session_no, "session_no", allow_none=True),
                ),
            ),
        )
        return _row_dict(row)


__all__ = [
    "CollaborationStateSegmentService",
    "SegmentValidationError",
]
