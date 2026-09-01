# -*- coding: utf-8 -*-
"""Discussion-scoped persistence for incremental state assessment batches.

This module only owns the batch/cursor transaction boundary. Scheduling and
LLM parsing are deliberately left to later plan batches.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from config import OBSERVATION_MAX_ASSESSMENT_ROUNDS
from db import db, now_str, query_one
from services.three_stage_schema import (
    CANONICAL_SUB_STATE_CODES,
    dumps_json,
    legacy_state_for_sub_state,
    normalize_canonical_sub_state,
    route_for_sub_state,
)
from services.three_stage_latency import latency_timestamp, record_latency_event


logger = logging.getLogger(__name__)

BATCH_STATUSES = {"pending", "running", "succeeded", "failed", "superseded"}
ACTIVE_BATCH_STATUSES = {"pending", "running"}
SEGMENT_SOURCES = {"rule", "llm", "legacy"}
SEGMENT_ASSESSMENT_STATUSES = {"candidate", "confirmed", "superseded"}
OBSERVATION_STATUSES = {"inactive", "observing"}
SEGMENT_STATE_CODES = {
    "positive_collaboration",
    "conflict_tension",
    "negative_silence",
    "frustration_stuck",
    # Retained for existing rule/legacy callers.  New LLM output uses
    # frustration_stuck and the compatibility projection maps it as needed.
    "blocked_frustration",
    "task_detached",
    "off_task",
    "unknown",
}
TERMINAL_BATCH_STATUSES = {"degraded", "quarantined"}
FALLBACK_REASON_RETRY_EXHAUSTED = "batch_retry_exhausted"
FALLBACK_REASON_BATCH_UNCLASSIFIED = "batch_unclassified"


class StateAssessmentBatchError(ValueError):
    """Raised when an incremental assessment operation is invalid."""


def _row_dict(row) -> Optional[dict]:
    return dict(row) if row else None


def _as_int(value: Any, field: str, *, optional: bool = False) -> Optional[int]:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise StateAssessmentBatchError(f"invalid_{field}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StateAssessmentBatchError(f"invalid_{field}") from exc


def _json_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _explicit_evidence_values(payload: dict, *keys: str) -> list[int]:
    values = []
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in keys and isinstance(value, list):
                    for item in value:
                        if isinstance(item, bool):
                            continue
                        try:
                            parsed = int(item)
                        except (TypeError, ValueError):
                            continue
                        if parsed not in values:
                            values.append(parsed)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(item for item in current if isinstance(item, (dict, list)))
    return values


def _scope_values(group_id, session_id, discussion_id) -> tuple[int, int, int]:
    return (
        _as_int(group_id, "group_id"),
        _as_int(session_id, "session_id"),
        _as_int(discussion_id, "discussion_id"),
    )


def _scope_row(conn, group_id: int, session_id: int, discussion_id: int):
    row = conn.execute(
        """
        SELECT gsd.id, gsd.group_id, gsd.session_id,
               es.session_no, es.task_id
        FROM group_session_discussions AS gsd
        JOIN experiment_sessions AS es ON es.id=gsd.session_id
        WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
        """,
        (discussion_id, group_id, session_id),
    ).fetchone()
    if not row:
        raise StateAssessmentBatchError("discussion_scope_mismatch")
    return row


def _ensure_cursor_with_conn(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    timestamp: str,
):
    scope = _scope_row(conn, group_id, session_id, discussion_id)
    conn.execute(
        """
        INSERT INTO discussion_assessment_cursors(
            group_id, session_id, session_no, task_id, discussion_id, updated_at
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(group_id, session_id, discussion_id) DO NOTHING
        """,
        (
            group_id,
            session_id,
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            timestamp,
        ),
    )
    return conn.execute(
        """
        SELECT * FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (group_id, session_id, discussion_id),
    ).fetchone()


def _window_key(
    group_id: int,
    session_id: int,
    discussion_id: int,
    candidate_start_sequence: int,
    candidate_end_sequence: int,
) -> str:
    raw = (
        f"g={group_id}|s={session_id}|d={discussion_id}|"
        f"candidate={candidate_start_sequence}-{candidate_end_sequence}"
    )
    return "state-window:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _batch_with_scope(conn, batch_id: int):
    row = conn.execute(
        "SELECT * FROM state_assessment_batches WHERE id=?",
        (_as_int(batch_id, "batch_id"),),
    ).fetchone()
    if not row:
        raise StateAssessmentBatchError("batch_not_found")
    _scope_row(
        conn,
        int(row["group_id"]),
        int(row["session_id"]),
        int(row["discussion_id"]),
    )
    return row


def _normalize_segments(
    batch: dict,
    segments: list[dict],
    *,
    candidate_student_sequences: Optional[set[int]] = None,
) -> list[dict]:
    if not isinstance(segments, list):
        raise StateAssessmentBatchError("segments_must_be_list")
    normalized = []
    seen_orders = set()
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            raise StateAssessmentBatchError("invalid_segment")
        canonical_payload = item.get("canonical_sub_state_code") or item.get("canonical_sub_state")
        canonical_sub_state = None
        if canonical_payload is not None:
            raw_canonical = str(canonical_payload or "").strip()
            if raw_canonical not in CANONICAL_SUB_STATE_CODES:
                raise StateAssessmentBatchError("invalid_canonical_sub_state")
            canonical_sub_state = normalize_canonical_sub_state(canonical_payload)
        state_code = str(item.get("state_code") or item.get("state") or "").strip()
        if canonical_sub_state and (not state_code or state_code not in SEGMENT_STATE_CODES):
            state_code = legacy_state_for_sub_state(canonical_sub_state)
        if state_code not in SEGMENT_STATE_CODES:
            raise StateAssessmentBatchError("invalid_segment_state")
        start_sequence = _as_int(
            item.get("start_sequence", item.get("start_message_id")),
            "start_sequence",
        )
        end_sequence = _as_int(
            item.get("end_sequence", item.get("end_message_id")),
            "end_sequence",
        )
        if start_sequence > end_sequence:
            raise StateAssessmentBatchError("invalid_segment_range")
        if (
            start_sequence < int(batch["candidate_start_sequence"])
            or end_sequence > int(batch["candidate_end_sequence"])
        ):
            raise StateAssessmentBatchError("segment_outside_candidate_window")
        if candidate_student_sequences is not None and (
            start_sequence not in candidate_student_sequences
            or end_sequence not in candidate_student_sequences
        ):
            raise StateAssessmentBatchError("segment_boundary_not_candidate_student_message")

        segment_order = _as_int(item.get("segment_order", index), "segment_order")
        if segment_order < 0 or segment_order in seen_orders:
            raise StateAssessmentBatchError("duplicate_or_invalid_segment_order")
        seen_orders.add(segment_order)

        evidence = item.get("evidence_sequences", item.get("evidence_message_ids", []))
        if evidence is None:
            evidence = []
        if not isinstance(evidence, list):
            raise StateAssessmentBatchError("invalid_evidence_sequences")
        evidence_sequences = []
        for value in evidence:
            sequence = _as_int(value, "evidence_sequence")
            if sequence < start_sequence or sequence > end_sequence:
                raise StateAssessmentBatchError("evidence_outside_segment")
            if (
                candidate_student_sequences is not None
                and sequence not in candidate_student_sequences
            ):
                raise StateAssessmentBatchError("evidence_not_candidate_student_message")
            if sequence not in evidence_sequences:
                evidence_sequences.append(sequence)
        if len(evidence_sequences) > 3:
            raise StateAssessmentBatchError("too_many_evidence_sequences")

        confidence = item.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool):
                raise StateAssessmentBatchError("invalid_confidence")
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise StateAssessmentBatchError("invalid_confidence") from exc
            if confidence < 0.0 or confidence > 1.0:
                raise StateAssessmentBatchError("invalid_confidence")

        source = str(item.get("source") or "llm").strip()
        if source not in SEGMENT_SOURCES:
            raise StateAssessmentBatchError("invalid_segment_source")
        assessment_status = str(item.get("assessment_status") or "confirmed").strip()
        if assessment_status not in SEGMENT_ASSESSMENT_STATUSES:
            raise StateAssessmentBatchError("invalid_segment_assessment_status")
        active_value = item.get("is_active_at_batch_end", False)
        if not isinstance(active_value, (bool, int)) or int(active_value) not in (0, 1):
            raise StateAssessmentBatchError("invalid_active_segment_flag")

        normalized.append(
            {
                "state_code": state_code,
                "raw_sub_state_code": item.get("raw_sub_state_code") or item.get("raw_sub_state"),
                "canonical_sub_state_code": canonical_sub_state,
                "secondary_tags_json": _json_text(item.get("secondary_tags") or []),
                "sub_state_confidence": confidence if canonical_sub_state else None,
                "should_intervene": (
                    1 if route_for_sub_state(canonical_sub_state)["should_intervene"] else 0
                )
                if canonical_sub_state
                else None,
                "selected_strategy_id": None,
                "source_stage": "stage2" if canonical_sub_state else None,
                "start_sequence": start_sequence,
                "end_sequence": end_sequence,
                "segment_order": segment_order,
                "evidence_sequences": evidence_sequences,
                "confidence": confidence,
                "source": source,
                "assessment_status": assessment_status,
                "is_active_at_batch_end": int(active_value),
                "trigger_type": str(item.get("trigger_type") or batch["trigger_type"]),
            }
        )
    normalized = sorted(normalized, key=lambda item: item["segment_order"])
    for previous, current in zip(normalized, normalized[1:]):
        if current["start_sequence"] <= previous["end_sequence"]:
            raise StateAssessmentBatchError("overlapping_segments")
    return normalized


def _candidate_student_rows(conn, batch: dict) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.id, m.sequence, m.session_no, m.task_id, m.discussion_id
        FROM messages AS m
        JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
          AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
          AND m.sequence BETWEEN ? AND ?
        ORDER BY m.sequence, m.id
        """,
        (
            batch["group_id"],
            batch["session_id"],
            batch["discussion_id"],
            batch["candidate_start_sequence"],
            batch["candidate_end_sequence"],
        ),
    ).fetchall()
    return [dict(row) for row in rows if row["sequence"] is not None]


def _confirmed_rule_fallbacks(conn, batch: dict, student_rows: list[dict]) -> list[dict]:
    """Return only rule assessments with explicit evidence in this batch window."""
    id_to_sequence = {int(row["id"]): int(row["sequence"]) for row in student_rows}
    candidate_sequences = {int(row["sequence"]) for row in student_rows}
    scope_by_sequence = {int(row["sequence"]): row for row in student_rows}
    rows = conn.execute(
        """
        SELECT id, rule_state_code, assessment_status, confidence, state_score,
               rule_assessment_json, context_json
        FROM state_assessments
        WHERE group_id=? AND session_id=? AND discussion_id=?
          AND assessment_status='confirmed'
          AND rule_state_code IS NOT NULL
          AND rule_state_code<>'unknown'
        ORDER BY id
        """,
        (
            batch["group_id"],
            batch["session_id"],
            batch["discussion_id"],
        ),
    ).fetchall()
    fallbacks = []
    for row in rows:
        state_code = str(row["rule_state_code"] or "").strip()
        if state_code not in SEGMENT_STATE_CODES:
            continue
        rule_payload = _json_object(row["rule_assessment_json"])
        context_payload = _json_object(row["context_json"])
        explicit_sequences = _explicit_evidence_values(
            rule_payload,
            "evidence_sequences",
            "matched_sequences",
        )
        explicit_sequences.extend(
            value
            for value in _explicit_evidence_values(
                context_payload,
                "evidence_sequences",
                "matched_sequences",
            )
            if value not in explicit_sequences
        )
        evidence_ids = _explicit_evidence_values(
            rule_payload,
            "evidence_message_ids",
            "matched_message_ids",
        )
        evidence_ids.extend(
            value
            for value in _explicit_evidence_values(
                context_payload,
                "evidence_message_ids",
                "matched_message_ids",
            )
            if value not in evidence_ids
        )
        evidence_sequences = sorted(
            {
                sequence
                for sequence in explicit_sequences
                if sequence in candidate_sequences
            }
            | {
                id_to_sequence[message_id]
                for message_id in evidence_ids
                if message_id in id_to_sequence
            }
        )
        if not evidence_sequences:
            continue
        first_scope = scope_by_sequence[evidence_sequences[0]]
        confidence = row["confidence"]
        if confidence is None:
            confidence = row["state_score"]
        try:
            confidence = min(1.0, max(0.0, float(confidence or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        fallbacks.append(
            {
                "assessment_id": int(row["id"]),
                "state_code": state_code,
                "start_sequence": evidence_sequences[0],
                "end_sequence": evidence_sequences[-1],
                "evidence_sequences": evidence_sequences[:3],
                "confidence": confidence,
                "session_no": first_scope.get("session_no"),
                "task_id": first_scope.get("task_id"),
            }
        )
    return fallbacks


def _insert_rule_fallback_segments(
    conn,
    batch: dict,
    fallbacks: list[dict],
    *,
    timestamp: str,
) -> list[int]:
    segment_ids = []
    for order, fallback in enumerate(fallbacks):
        evidence_json = _json_text(fallback["evidence_sequences"])
        dedupe_key = (
            f"assessment_batch:{batch['id']}:degraded_rule:"
            f"{fallback['assessment_id']}"
        )
        conn.execute(
            """
            INSERT INTO collaboration_state_segments(
                group_id, session_id, session_no, task_id, discussion_id,
                state_code, segment_kind,
                start_message_id, end_message_id,
                assessment_batch_id, start_sequence, end_sequence,
                evidence_message_ids_json, evidence_sequences,
                confidence, fallback_reason, source, assessment_status,
                segment_order, is_active_at_batch_end, trigger_type,
                assessment_id, analysis_anchor_message_id,
                analysis_window_start_message_id,
                analysis_window_end_message_id,
                is_finalized, dedupe_key, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,'message_range',?,?,?,?,?,?,?,?,?,'rule',
                     'confirmed',?,?,?,?,?,?,?,1,?,?,?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                batch["group_id"],
                batch["session_id"],
                fallback["session_no"],
                fallback["task_id"],
                batch["discussion_id"],
                fallback["state_code"],
                fallback["start_sequence"],
                fallback["end_sequence"],
                batch["id"],
                fallback["start_sequence"],
                fallback["end_sequence"],
                evidence_json,
                evidence_json,
                fallback["confidence"],
                FALLBACK_REASON_RETRY_EXHAUSTED,
                order,
                1
                if fallback["end_sequence"] == int(batch["candidate_end_sequence"])
                else 0,
                batch["trigger_type"],
                fallback["assessment_id"],
                batch["candidate_start_sequence"],
                batch["context_start_sequence"]
                if batch["context_start_sequence"] is not None
                else batch["candidate_start_sequence"],
                batch["candidate_end_sequence"],
                dedupe_key,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE collaboration_state_segments
            SET should_intervene=0, selected_strategy_id=NULL, updated_at=?
            WHERE dedupe_key=?
            """,
            (timestamp, dedupe_key),
        )
        row = conn.execute(
            "SELECT id FROM collaboration_state_segments WHERE dedupe_key=?",
            (dedupe_key,),
        ).fetchone()
        if row:
            segment_ids.append(int(row["id"]))
    return segment_ids


def _segment_covered_sequences(student_sequences: list[int], rows: list[dict]) -> set[int]:
    covered = set()
    candidate_set = set(int(sequence) for sequence in student_sequences)
    for row in rows or []:
        try:
            start = int(row["start_sequence"])
            end = int(row["end_sequence"])
        except (TypeError, ValueError, KeyError):
            continue
        covered.update(
            sequence
            for sequence in candidate_set
            if start <= int(sequence) <= end
        )
    return covered


def _contiguous_ranges(sequences: list[int]) -> list[tuple[int, int]]:
    ordered = sorted({int(sequence) for sequence in sequences})
    if not ordered:
        return []
    ranges = []
    start = previous = ordered[0]
    for sequence in ordered[1:]:
        if sequence == previous + 1:
            previous = sequence
            continue
        ranges.append((start, previous))
        start = previous = sequence
    ranges.append((start, previous))
    return ranges


def _batch_confirmed_segment_rows(conn, batch: dict) -> list[dict]:
    rows = conn.execute(
        """
        SELECT start_sequence, end_sequence
        FROM collaboration_state_segments
        WHERE assessment_batch_id=?
          AND segment_kind='message_range'
          AND assessment_status='confirmed'
          AND start_sequence IS NOT NULL
          AND end_sequence IS NOT NULL
        """,
        (batch["id"],),
    ).fetchall()
    return [dict(row) for row in rows]


def _insert_unclassified_fallback_segments(
    conn,
    batch: dict,
    student_rows: list[dict],
    *,
    timestamp: str,
    start_order: int,
    error_code: str,
    error_detail: str = None,
) -> list[int]:
    student_sequences = [int(row["sequence"]) for row in student_rows]
    covered = _segment_covered_sequences(
        student_sequences,
        _batch_confirmed_segment_rows(conn, batch),
    )
    uncovered_ranges = _contiguous_ranges(
        [sequence for sequence in student_sequences if sequence not in covered]
    )
    by_sequence = {int(row["sequence"]): row for row in student_rows}
    segment_ids = []
    for offset, (start_sequence, end_sequence) in enumerate(uncovered_ranges):
        first_scope = by_sequence[start_sequence]
        order = start_order + offset
        dedupe_key = (
            f"assessment_batch:{batch['id']}:batch_unclassified:"
            f"{start_sequence}-{end_sequence}"
        )
        evidence_json = _json_text([])
        conn.execute(
            """
            INSERT INTO collaboration_state_segments(
                group_id, session_id, session_no, task_id, discussion_id,
                state_code, segment_kind,
                start_message_id, end_message_id,
                assessment_batch_id, start_sequence, end_sequence,
                evidence_message_ids_json, evidence_sequences,
                confidence, fallback_reason, source, assessment_status,
                segment_order, is_active_at_batch_end, trigger_type,
                analysis_anchor_message_id,
                analysis_window_start_message_id,
                analysis_window_end_message_id,
                prompt_version, is_finalized, dedupe_key, created_at, updated_at
            ) VALUES(?,?,?,?,?,'unknown','message_range',?,?,?,?,?,?,?,?,?,
                     'llm','unclassified',?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                batch["group_id"],
                batch["session_id"],
                first_scope.get("session_no"),
                first_scope.get("task_id"),
                batch["discussion_id"],
                start_sequence,
                end_sequence,
                batch["id"],
                start_sequence,
                end_sequence,
                evidence_json,
                evidence_json,
                None,
                FALLBACK_REASON_BATCH_UNCLASSIFIED,
                order,
                1 if end_sequence == int(batch["candidate_end_sequence"]) else 0,
                batch["trigger_type"],
                batch["candidate_start_sequence"],
                batch["context_start_sequence"]
                if batch["context_start_sequence"] is not None
                else batch["candidate_start_sequence"],
                batch["candidate_end_sequence"],
                batch.get("prompt_version"),
                1,
                dedupe_key,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE collaboration_state_segments
            SET should_intervene=0, selected_strategy_id=NULL, updated_at=?
            WHERE dedupe_key=?
            """,
            (timestamp, dedupe_key),
        )
        row = conn.execute(
            "SELECT id FROM collaboration_state_segments WHERE dedupe_key=?",
            (dedupe_key,),
        ).fetchone()
        if row:
            segment_ids.append(int(row["id"]))
    return segment_ids


class StateAssessmentBatchService:
    """Transactional data service used by future assessment schedulers."""

    @staticmethod
    def get_cursor(*, group_id: int, session_id: int, discussion_id: int) -> Optional[dict]:
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        row = query_one(
            """
            SELECT * FROM discussion_assessment_cursors
            WHERE group_id=? AND session_id=? AND discussion_id=?
            """,
            (group_id, session_id, discussion_id),
        )
        return _row_dict(row)

    @staticmethod
    def get_or_create_cursor(
        *, group_id: int, session_id: int, discussion_id: int
    ) -> dict:
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = _scope_row(conn, group_id, session_id, discussion_id)
            row = _ensure_cursor_with_conn(
                conn,
                group_id=group_id,
                session_id=session_id,
                discussion_id=discussion_id,
                timestamp=now_str(),
            )
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_last_finalized_student_sequence(
        *, group_id: int, session_id: int, discussion_id: int
    ) -> int:
        cursor = StateAssessmentBatchService.get_or_create_cursor(
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        )
        return int(cursor["last_finalized_student_sequence"] or 0)

    @staticmethod
    def get_last_scheduled_student_sequence(
        *, group_id: int, session_id: int, discussion_id: int
    ) -> int:
        cursor = StateAssessmentBatchService.get_or_create_cursor(
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        )
        return max(
            int(cursor.get("last_finalized_student_sequence") or 0),
            int(cursor.get("last_scheduled_student_sequence") or 0),
        )

    @staticmethod
    def get_active_batch(
        *, group_id: int, session_id: int, discussion_id: int
    ) -> Optional[dict]:
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        row = query_one(
            """
            SELECT * FROM state_assessment_batches
            WHERE group_id=? AND session_id=? AND discussion_id=?
              AND status IN ('pending','running')
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (group_id, session_id, discussion_id),
        )
        return _row_dict(row)

    @staticmethod
    def get_batch(batch_id: int) -> Optional[dict]:
        row = query_one(
            "SELECT * FROM state_assessment_batches WHERE id=?",
            (_as_int(batch_id, "batch_id"),),
        )
        return _row_dict(row)

    @staticmethod
    def create_batch(
        *,
        group_id: int,
        session_id: int,
        discussion_id: int,
        candidate_start_sequence: int,
        candidate_end_sequence: int,
        trigger_type: str,
        context_start_sequence: int = None,
        context_end_sequence: int = None,
        trigger_sequence: int = None,
        window_key: str = None,
        model: str = None,
        prompt_version: str = None,
        request_priority: int = 0,
        max_attempts: int = 2,
        rerun_requested: bool = False,
    ) -> dict:
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        candidate_start = _as_int(candidate_start_sequence, "candidate_start_sequence")
        candidate_end = _as_int(candidate_end_sequence, "candidate_end_sequence")
        if candidate_start < 0 or candidate_start > candidate_end:
            raise StateAssessmentBatchError("invalid_candidate_window")
        context_start = _as_int(
            context_start_sequence, "context_start_sequence", optional=True
        )
        context_end = _as_int(context_end_sequence, "context_end_sequence", optional=True)
        if (context_start is None) != (context_end is None):
            raise StateAssessmentBatchError("incomplete_context_window")
        if context_start is not None and context_start > context_end:
            raise StateAssessmentBatchError("invalid_context_window")
        trigger = str(trigger_type or "").strip()
        if not trigger:
            raise StateAssessmentBatchError("missing_trigger_type")
        trigger_sequence = _as_int(
            trigger_sequence, "trigger_sequence", optional=True
        )
        request_priority = _as_int(request_priority, "request_priority")
        max_attempts = _as_int(max_attempts, "max_attempts")
        if request_priority < 0 or max_attempts < 1:
            raise StateAssessmentBatchError("invalid_batch_retry_policy")
        stable_key = str(window_key or "").strip() or _window_key(
            group_id,
            session_id,
            discussion_id,
            candidate_start,
            candidate_end,
        )
        timestamp = now_str()

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = _scope_row(conn, group_id, session_id, discussion_id)
            _ensure_cursor_with_conn(
                conn,
                group_id=group_id,
                session_id=session_id,
                discussion_id=discussion_id,
                timestamp=timestamp,
            )
            candidate_student_sequences = [
                int(row["sequence"])
                for row in _candidate_student_rows(
                    conn,
                    {
                        "group_id": group_id,
                        "session_id": session_id,
                        "discussion_id": discussion_id,
                        "candidate_start_sequence": candidate_start,
                        "candidate_end_sequence": candidate_end,
                    },
                )
            ]
            cur = conn.execute(
                """
                INSERT INTO state_assessment_batches(
                    group_id, session_id, session_no, task_id, discussion_id,
                    candidate_start_sequence, candidate_end_sequence,
                    context_start_sequence, context_end_sequence,
                    trigger_type, trigger_sequence, window_key, status,
                    rerun_requested, request_priority, last_trigger_sequence,
                    attempt_count, max_attempts,
                    model, prompt_version, student_sequences_json,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,0,?,?,?,?,?,?)
                ON CONFLICT(
                    group_id, session_id, discussion_id,
                    candidate_start_sequence, candidate_end_sequence
                ) DO NOTHING
                """,
                (
                    group_id,
                    session_id,
                    scope["session_no"],
                    scope["task_id"],
                    discussion_id,
                    candidate_start,
                    candidate_end,
                    context_start,
                    context_end,
                    trigger,
                    trigger_sequence,
                    stable_key,
                    1 if rerun_requested else 0,
                    request_priority,
                    trigger_sequence,
                    max_attempts,
                    model,
                    prompt_version,
                    _json_text(candidate_student_sequences),
                    timestamp,
                    timestamp,
                ),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                """
                SELECT * FROM state_assessment_batches
                WHERE group_id=? AND session_id=? AND discussion_id=?
                  AND candidate_start_sequence=? AND candidate_end_sequence=?
                """,
                (
                    group_id,
                    session_id,
                    discussion_id,
                    candidate_start,
                    candidate_end,
                ),
            ).fetchone()
            conn.execute(
                """
                UPDATE discussion_assessment_cursors
                SET last_assessment_requested_at=?, updated_at=?
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (timestamp, timestamp, group_id, session_id, discussion_id),
            )
            conn.commit()
            return {"created": created, "batch": dict(row)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def claim_batch(batch_id: int) -> dict:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = _batch_with_scope(conn, batch_id)
            timestamp = latency_timestamp()
            cur = conn.execute(
                """
                UPDATE state_assessment_batches
                SET status='running', started_at=COALESCE(started_at, ?),
                    attempt_count=attempt_count+1, next_retry_at=NULL,
                    updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                  AND status='pending'
                  AND (next_retry_at IS NULL OR next_retry_at<=?)
                """,
                (
                    timestamp,
                    timestamp,
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            if cur.rowcount == 1:
                record_latency_event(
                    stage="queue",
                    event="task_started",
                    assessment_batch_id=batch["id"],
                    occurred_at=timestamp,
                    conn=conn,
                )
            conn.commit()
            return {"claimed": cur.rowcount == 1, "batch": dict(row)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def set_rerun_requested(batch_id: int, requested: bool = True) -> dict:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = _batch_with_scope(conn, batch_id)
            conn.execute(
                """
                UPDATE state_assessment_batches
                SET rerun_requested=?, updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                """,
                (
                    1 if requested else 0,
                    now_str(),
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def mark_batch_failed(
        batch_id: int, *, error_code: str, error_detail: str = None,
        next_retry_at: str = None,
    ) -> dict:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = _batch_with_scope(conn, batch_id)
            timestamp = now_str()
            conn.execute(
                """
                UPDATE state_assessment_batches
                SET status='failed', error_code=?, error_detail=?, completed_at=?,
                    next_retry_at=?, updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                  AND status IN ('pending','running')
                """,
                (
                    str(error_code or "assessment_failed"),
                    error_detail,
                    timestamp,
                    next_retry_at,
                    timestamp,
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def terminalize_exhausted_batch(
        batch_id: int,
        *,
        error_code: str = None,
        error_detail: str = None,
        force: bool = False,
    ) -> dict:
        """Quarantine one immutable window and advance only the scheduling cursor.

        The transaction owns fallback creation, terminal metadata, and cursor
        advancement.  Re-entry is idempotent and never changes the finalized
        cursor because no LLM result was produced.
        """
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_row = _batch_with_scope(conn, batch_id)
            batch = dict(batch_row)
            if batch.get("terminal_status") in TERMINAL_BATCH_STATUSES:
                rows = conn.execute(
                    """
                    SELECT id FROM collaboration_state_segments
                    WHERE assessment_batch_id=? AND fallback_reason IN (?,?)
                    ORDER BY id
                    """,
                    (
                        batch["id"],
                        FALLBACK_REASON_RETRY_EXHAUSTED,
                        FALLBACK_REASON_BATCH_UNCLASSIFIED,
                    ),
                ).fetchall()
                conn.commit()
                return {
                    "terminalized": False,
                    "reason": "already_terminal",
                    "batch": batch,
                    "segment_ids": [int(row["id"]) for row in rows],
                }
            if batch["status"] == "succeeded":
                conn.commit()
                return {
                    "terminalized": False,
                    "reason": "batch_already_succeeded",
                    "batch": batch,
                    "segment_ids": [],
                }
            exhausted = int(batch.get("attempt_count") or 0) >= int(
                batch.get("max_attempts") or 1
            )
            if not force and not exhausted:
                conn.commit()
                return {
                    "terminalized": False,
                    "reason": "retry_limit_not_reached",
                    "batch": batch,
                    "segment_ids": [],
                }
            if batch["status"] not in {"pending", "running", "failed"}:
                conn.commit()
                return {
                    "terminalized": False,
                    "reason": "batch_not_terminalizable",
                    "batch": batch,
                    "segment_ids": [],
                }

            timestamp = now_str()
            student_rows = _candidate_student_rows(conn, batch)
            student_sequences = [int(row["sequence"]) for row in student_rows]
            fallbacks = _confirmed_rule_fallbacks(conn, batch, student_rows)
            rule_segment_ids = _insert_rule_fallback_segments(
                conn,
                batch,
                fallbacks,
                timestamp=timestamp,
            )
            resolved_error_code = str(
                error_code or batch.get("error_code") or "application_error"
            )
            resolved_error_detail = (
                error_detail if error_detail is not None else batch.get("error_detail")
            )
            unclassified_segment_ids = _insert_unclassified_fallback_segments(
                conn,
                batch,
                student_rows,
                timestamp=timestamp,
                start_order=len(fallbacks),
                error_code=resolved_error_code,
                error_detail=resolved_error_detail,
            )
            segment_ids = rule_segment_ids + unclassified_segment_ids
            terminal_status = "degraded" if rule_segment_ids else "quarantined"
            fallback_action = (
                "degraded_rule_segments" if rule_segment_ids else "unclassified"
            )
            cur = conn.execute(
                """
                UPDATE state_assessment_batches
                SET status='failed', terminal_status=?, terminal_at=?,
                    fallback_action=?, fallback_segment_count=?,
                    student_sequences_json=COALESCE(student_sequences_json, ?),
                    error_code=?, error_detail=?, next_retry_at=NULL,
                    completed_at=COALESCE(completed_at, ?), rerun_requested=0,
                    updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                  AND terminal_status IS NULL
                  AND status IN ('pending','running','failed')
                """,
                (
                    terminal_status,
                    timestamp,
                    fallback_action,
                    len(segment_ids),
                    _json_text(student_sequences),
                    resolved_error_code,
                    resolved_error_detail,
                    timestamp,
                    timestamp,
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            if cur.rowcount != 1:
                row = conn.execute(
                    "SELECT * FROM state_assessment_batches WHERE id=?",
                    (batch["id"],),
                ).fetchone()
                conn.commit()
                return {
                    "terminalized": False,
                    "reason": "terminal_compare_and_set_lost",
                    "batch": dict(row),
                    "segment_ids": segment_ids,
                }
            cursor_row = _ensure_cursor_with_conn(
                conn,
                group_id=batch["group_id"],
                session_id=batch["session_id"],
                discussion_id=batch["discussion_id"],
                timestamp=timestamp,
            )
            cursor_before = max(
                int(cursor_row["last_finalized_student_sequence"] or 0),
                int(cursor_row["last_scheduled_student_sequence"] or 0),
            )
            conn.execute(
                """
                UPDATE discussion_assessment_cursors
                SET last_scheduled_student_sequence=CASE
                        WHEN last_scheduled_student_sequence<? THEN ?
                        ELSE last_scheduled_student_sequence
                    END,
                    last_assessment_completed_at=?,
                    last_scheduling_completed_at=?,
                    updated_at=?
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (
                    batch["candidate_end_sequence"],
                    batch["candidate_end_sequence"],
                    timestamp,
                    timestamp,
                    timestamp,
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            completed = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            conn.commit()
            result = {
                "terminalized": True,
                "reason": "retry_exhausted",
                "batch": dict(completed),
                "segment_ids": segment_ids,
                "cursor_before": cursor_before,
                "cursor_after": int(batch["candidate_end_sequence"]),
            }
            logger.warning(
                "[state_assessment_batch] terminalized %s",
                json.dumps(
                    {
                        "event": "state_assessment_batch_terminalized",
                        "group_id": batch["group_id"],
                        "session_id": batch["session_id"],
                        "discussion_id": batch["discussion_id"],
                        "batch_id": batch["id"],
                        "window_start": batch["candidate_start_sequence"],
                        "window_end": batch["candidate_end_sequence"],
                        "student_sequences": student_sequences,
                        "attempt_count": batch.get("attempt_count"),
                        "error_code": resolved_error_code,
                        "terminal_status": terminal_status,
                        "fallback_action": fallback_action,
                        "fallback_segment_count": len(segment_ids),
                        "rule_fallback_segment_count": len(rule_segment_ids),
                        "unclassified_fallback_segment_count": len(
                            unclassified_segment_ids
                        ),
                        "cursor_after": batch["candidate_end_sequence"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def recover_exhausted_batches(
        *, group_id: int, session_id: int, discussion_id: int
    ) -> list[dict]:
        """Finish terminalization only for batches created by the new schema."""
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        conn = db()
        try:
            rows = conn.execute(
                """
                SELECT id
                FROM state_assessment_batches
                WHERE group_id=? AND session_id=? AND discussion_id=?
                  AND status='failed'
                  AND terminal_status IS NULL
                  AND student_sequences_json IS NOT NULL
                  AND attempt_count>=max_attempts
                ORDER BY candidate_end_sequence, id
                """,
                (group_id, session_id, discussion_id),
            ).fetchall()
        finally:
            conn.close()
        return [
            StateAssessmentBatchService.terminalize_exhausted_batch(int(row["id"]))
            for row in rows
        ]

    @staticmethod
    def prepare_scope_reprocessing(
        *,
        group_id: int,
        session_id: int,
        discussion_id: int,
        apply: bool = False,
        error_codes: tuple[str, ...] = (
            "read_timeout",
            "schema_validation_error",
        ),
    ) -> dict:
        """Prepare terminal Stage 2 failures for ordered, cursor-driven replay.

        Rows deliberately remain ``failed``. Rewinding the scheduling cursor
        lets ``request_state_assessment`` reopen and enqueue exactly one
        immutable window at a time, preserving the one-active-batch invariant.
        """
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        normalized_codes = tuple(
            dict.fromkeys(str(code or "").strip() for code in error_codes if code)
        )
        if not normalized_codes:
            raise StateAssessmentBatchError("reprocess_error_codes_required")
        placeholders = ",".join("?" for _ in normalized_codes)
        conn = db()
        try:
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """
                SELECT id, status
                FROM state_assessment_batches
                WHERE group_id=? AND session_id=? AND discussion_id=?
                  AND status IN ('pending','running')
                ORDER BY id
                LIMIT 1
                """,
                (group_id, session_id, discussion_id),
            ).fetchone()
            if active:
                if apply:
                    conn.rollback()
                return {
                    "prepared": False,
                    "reason": "active_batch_exists",
                    "active_batch": dict(active),
                    "batches": [],
                }

            rows = conn.execute(
                f"""
                SELECT *
                FROM state_assessment_batches
                WHERE group_id=? AND session_id=? AND discussion_id=?
                  AND status='failed'
                  AND terminal_status IN ('degraded','quarantined')
                  AND error_code IN ({placeholders})
                  AND student_sequences_json IS NOT NULL
                ORDER BY candidate_start_sequence, id
                """,
                (
                    group_id,
                    session_id,
                    discussion_id,
                    *normalized_codes,
                ),
            ).fetchall()
            batches = [dict(row) for row in rows]
            if not batches:
                if apply:
                    conn.rollback()
                return {
                    "prepared": False,
                    "reason": "no_matching_terminal_batches",
                    "batches": [],
                }

            replay_from = min(
                int(row["candidate_start_sequence"]) for row in batches
            ) - 1
            cursor = conn.execute(
                """
                SELECT *
                FROM discussion_assessment_cursors
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (group_id, session_id, discussion_id),
            ).fetchone()
            finalized_through = (
                int(cursor["last_finalized_student_sequence"] or 0)
                if cursor
                else 0
            )
            if finalized_through > replay_from:
                if apply:
                    conn.rollback()
                return {
                    "prepared": False,
                    "reason": "finalized_cursor_overlaps_replay",
                    "last_finalized_student_sequence": finalized_through,
                    "replay_from_sequence": replay_from + 1,
                    "batches": batches,
                }

            batch_ids = [int(row["id"]) for row in batches]
            batch_placeholders = ",".join("?" for _ in batch_ids)
            fallback_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM collaboration_state_segments
                WHERE assessment_batch_id IN ({batch_placeholders})
                  AND fallback_reason IN (?,?)
                """,
                (
                    *batch_ids,
                    FALLBACK_REASON_RETRY_EXHAUSTED,
                    FALLBACK_REASON_BATCH_UNCLASSIFIED,
                ),
            ).fetchone()["count"]
            result = {
                "prepared": bool(apply),
                "reason": "scope_reprocessing_prepared" if apply else "dry_run",
                "group_id": group_id,
                "session_id": session_id,
                "discussion_id": discussion_id,
                "replay_from_sequence": replay_from + 1,
                "batch_count": len(batches),
                "batch_ids": batch_ids,
                "fallback_segment_count": int(fallback_count or 0),
                "state_only_replay": True,
                "batches": batches,
            }
            if not apply:
                return result

            timestamp = now_str()
            conn.execute(
                f"""
                DELETE FROM collaboration_state_segments
                WHERE assessment_batch_id IN ({batch_placeholders})
                  AND fallback_reason IN (?,?)
                """,
                (
                    *batch_ids,
                    FALLBACK_REASON_RETRY_EXHAUSTED,
                    FALLBACK_REASON_BATCH_UNCLASSIFIED,
                ),
            )
            conn.execute(
                f"""
                UPDATE state_assessment_batches
                SET attempt_count=0,
                    next_retry_at=NULL,
                    enqueued_at=NULL,
                    terminal_status=NULL,
                    terminal_at=NULL,
                    fallback_action='state_only_replay',
                    fallback_segment_count=0,
                    started_at=NULL,
                    completed_at=NULL,
                    error_code=NULL,
                    error_detail=NULL,
                    rerun_requested=1,
                    updated_at=?
                WHERE id IN ({batch_placeholders})
                  AND status='failed'
                """,
                (timestamp, *batch_ids),
            )
            conn.execute(
                f"""
                UPDATE strategy_pipeline_runs
                SET stage2_status='PENDING',
                    stage2_started_at=NULL,
                    stage2_completed_at=NULL,
                    stage3_status=NULL,
                    publish_status='NOT_READY',
                    final_status='PENDING_STAGE2',
                    skip_reason=NULL,
                    failure_code=NULL,
                    failure_detail=NULL,
                    updated_at=?
                WHERE group_id=? AND COALESCE(session_id, 0)=?
                  AND COALESCE(discussion_id, 0)=?
                  AND stage2_status='FAILED'
                  AND input_cutoff_student_sequence IN (
                      SELECT candidate_end_sequence
                      FROM state_assessment_batches
                      WHERE id IN ({batch_placeholders})
                  )
                """,
                (
                    timestamp,
                    group_id,
                    session_id,
                    discussion_id,
                    *batch_ids,
                ),
            )
            conn.execute(
                """
                UPDATE discussion_assessment_cursors
                SET last_scheduled_student_sequence=?,
                    last_assessment_completed_at=NULL,
                    last_scheduling_completed_at=NULL,
                    updated_at=?
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (
                    max(finalized_through, replay_from),
                    timestamp,
                    group_id,
                    session_id,
                    discussion_id,
                ),
            )
            conn.commit()
            return result
        except Exception:
            if apply:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def prepare_retry(batch_id: int, *, next_retry_at: str) -> dict:
        """Move one failed batch back to pending without resetting attempts."""
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = _batch_with_scope(conn, batch_id)
            timestamp = now_str()
            cur = conn.execute(
                """
                UPDATE state_assessment_batches
                SET status='pending', next_retry_at=?, completed_at=NULL,
                    error_code=NULL, error_detail=NULL, updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                  AND status='failed' AND attempt_count<max_attempts
                """,
                (
                    next_retry_at,
                    timestamp,
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            conn.commit()
            return {"prepared": cur.rowcount == 1, "batch": dict(row)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def mark_enqueued(batch_id: int) -> dict:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = _batch_with_scope(conn, batch_id)
            timestamp = latency_timestamp()
            conn.execute(
                """
                UPDATE state_assessment_batches
                SET enqueued_at=?, updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                """,
                (
                    timestamp,
                    timestamp,
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            record_latency_event(
                stage="queue",
                event="task_enqueued",
                assessment_batch_id=batch["id"],
                occurred_at=timestamp,
                conn=conn,
            )
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def save_successful_segments(
        batch_id: int,
        segments: list[dict],
        *,
        raw_response: str = None,
        parsed_response: Any = None,
        model: str = None,
        prompt_version: str = None,
    ) -> dict:
        """Save all segments and advance the cursor in one transaction."""
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_row = _batch_with_scope(conn, batch_id)
            batch = dict(batch_row)
            if batch["status"] == "succeeded":
                rows = conn.execute(
                    """
                    SELECT * FROM collaboration_state_segments
                    WHERE assessment_batch_id=? ORDER BY segment_order, id
                    """,
                    (batch["id"],),
                ).fetchall()
                conn.commit()
                return {
                    "saved": False,
                    "batch": batch,
                    "segments": [dict(row) for row in rows],
                }
            if batch["status"] != "running":
                raise StateAssessmentBatchError("batch_not_running")

            candidate_rows = conn.execute(
                """
                SELECT DISTINCT m.sequence
                FROM messages AS m
                JOIN users AS u ON u.id=m.user_id
                WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
                  AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
                  AND m.sequence BETWEEN ? AND ?
                """,
                (
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                    batch["candidate_start_sequence"],
                    batch["candidate_end_sequence"],
                ),
            ).fetchall()
            candidate_student_sequences = {
                int(row["sequence"])
                for row in candidate_rows
                if row["sequence"] is not None
            }
            normalized = _normalize_segments(
                batch,
                segments,
                candidate_student_sequences=candidate_student_sequences,
            )
            timestamp = now_str()
            segment_ids = []
            segment_id_by_order = {}
            for segment in normalized:
                evidence_json = _json_text(segment["evidence_sequences"])
                # Compatibility fields below intentionally mirror sequence values;
                # they are not messages.id primary keys.
                dedupe_key = (
                    f"assessment_batch:{batch['id']}:segment:{segment['segment_order']}"
                )
                cur = conn.execute(
                    """
                    INSERT INTO collaboration_state_segments(
                        group_id, session_id, session_no, task_id, discussion_id,
                        state_code, raw_sub_state_code, canonical_sub_state_code,
                        secondary_tags_json, sub_state_confidence,
                        strategy_pipeline_run_id, should_intervene,
                        selected_strategy_id, strategy_library_version,
                        source_stage, segment_kind,
                        start_message_id, end_message_id,
                        assessment_batch_id, start_sequence, end_sequence,
                        start_at, end_at,
                        evidence_message_ids_json, evidence_sequences,
                        confidence, source, assessment_status, segment_order,
                        is_active_at_batch_end, trigger_type,
                        analysis_anchor_message_id,
                        analysis_window_start_message_id,
                        analysis_window_end_message_id,
                        prompt_version, is_finalized, dedupe_key,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch["group_id"],
                        batch["session_id"],
                        batch["session_no"],
                        batch["task_id"],
                        batch["discussion_id"],
                        segment["state_code"],
                        segment.get("raw_sub_state_code"),
                        segment.get("canonical_sub_state_code"),
                        segment.get("secondary_tags_json"),
                        segment.get("sub_state_confidence"),
                        None,
                        segment.get("should_intervene"),
                        segment.get("selected_strategy_id"),
                        None,
                        segment.get("source_stage"),
                        "message_range",
                        segment["start_sequence"],
                        segment["end_sequence"],
                        batch["id"],
                        segment["start_sequence"],
                        segment["end_sequence"],
                        None,
                        None,
                        evidence_json,
                        evidence_json,
                        segment["confidence"],
                        segment["source"],
                        segment["assessment_status"],
                        segment["segment_order"],
                        segment["is_active_at_batch_end"],
                        segment["trigger_type"],
                        batch["candidate_start_sequence"],
                        batch["context_start_sequence"]
                        if batch["context_start_sequence"] is not None
                        else batch["candidate_start_sequence"],
                        batch["candidate_end_sequence"],
                        prompt_version or batch["prompt_version"],
                        1 if segment["assessment_status"] == "confirmed" else 0,
                        dedupe_key,
                        timestamp,
                        timestamp,
                    ),
                )
                segment_id = int(cur.lastrowid)
                segment_ids.append(segment_id)
                segment_id_by_order[segment["segment_order"]] = segment_id

            stored_parsed_response = parsed_response
            if isinstance(parsed_response, dict):
                stored_parsed_response = dict(parsed_response)
                intervention = stored_parsed_response.get("intervention")
                if isinstance(intervention, dict):
                    intervention = dict(intervention)
                    target_order = intervention.get("target_segment_index")
                    if (
                        not isinstance(target_order, bool)
                        and isinstance(target_order, int)
                        and target_order in segment_id_by_order
                    ):
                        intervention["target_segment_id"] = segment_id_by_order[target_order]
                    else:
                        intervention["target_segment_id"] = None
                    stored_parsed_response["intervention"] = intervention

            conn.execute(
                """
                UPDATE state_assessment_batches
                SET status='succeeded', rerun_requested=0,
                    model=COALESCE(?, model),
                    prompt_version=COALESCE(?, prompt_version),
                    raw_response=?, parsed_response=?,
                    error_code=NULL, error_detail=NULL, next_retry_at=NULL,
                    completed_at=?, updated_at=?
                WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                  AND status='running'
                """,
                (
                    model,
                    prompt_version,
                    raw_response,
                    _json_text(stored_parsed_response),
                    timestamp,
                    timestamp,
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            _ensure_cursor_with_conn(
                conn,
                group_id=batch["group_id"],
                session_id=batch["session_id"],
                discussion_id=batch["discussion_id"],
                timestamp=timestamp,
            )
            conn.execute(
                """
                UPDATE discussion_assessment_cursors
                SET last_finalized_student_sequence=CASE
                        WHEN last_finalized_student_sequence<? THEN ?
                        ELSE last_finalized_student_sequence
                    END,
                    last_scheduled_student_sequence=CASE
                        WHEN last_scheduled_student_sequence<? THEN ?
                        ELSE last_scheduled_student_sequence
                    END,
                    last_assessment_completed_at=?,
                    last_scheduling_completed_at=?,
                    updated_at=?
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (
                    batch["candidate_end_sequence"],
                    batch["candidate_end_sequence"],
                    batch["candidate_end_sequence"],
                    batch["candidate_end_sequence"],
                    timestamp,
                    timestamp,
                    timestamp,
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                ),
            )
            intervention_payload = (
                stored_parsed_response.get("intervention")
                if isinstance(stored_parsed_response, dict)
                else None
            )
            observation_reason = (
                str(intervention_payload.get("reason_code") or "").strip()
                if isinstance(intervention_payload, dict)
                else ""
            )
            if observation_reason and observation_reason not in {
                "continue_observing",
                "insufficient_evidence",
            }:
                conn.execute(
                    """
                    UPDATE discussion_assessment_cursors
                    SET observation_status='inactive', updated_at=?
                    WHERE group_id=? AND session_id=? AND discussion_id=?
                      AND observation_status='observing'
                    """,
                    (
                        timestamp,
                        batch["group_id"],
                        batch["session_id"],
                        batch["discussion_id"],
                    ),
                )
            completed_batch = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            segment_rows = conn.execute(
                """
                SELECT * FROM collaboration_state_segments
                WHERE assessment_batch_id=? ORDER BY segment_order, id
                """,
                (batch["id"],),
            ).fetchall()
            conn.commit()
            return {
                "saved": True,
                "batch": dict(completed_batch),
                "segment_ids": segment_ids,
                "segments": [dict(row) for row in segment_rows],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def set_observation(
        *,
        group_id: int,
        session_id: int,
        discussion_id: int,
        observation_status: str,
        observation_started_sequence: int = None,
        last_intervention_sequence: int = None,
    ) -> dict:
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        status = str(observation_status or "").strip()
        if status not in OBSERVATION_STATUSES:
            raise StateAssessmentBatchError("invalid_observation_status")
        observation_started_sequence = _as_int(
            observation_started_sequence,
            "observation_started_sequence",
            optional=True,
        )
        last_intervention_sequence = _as_int(
            last_intervention_sequence,
            "last_intervention_sequence",
            optional=True,
        )
        timestamp = now_str()
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _scope_row(conn, group_id, session_id, discussion_id)
            _ensure_cursor_with_conn(
                conn,
                group_id=group_id,
                session_id=session_id,
                discussion_id=discussion_id,
                timestamp=timestamp,
            )
            conn.execute(
                """
                UPDATE discussion_assessment_cursors
                SET observation_status=?, observation_started_sequence=?,
                    last_intervention_sequence=COALESCE(?, last_intervention_sequence),
                    updated_at=?
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (
                    status,
                    observation_started_sequence,
                    last_intervention_sequence,
                    timestamp,
                    group_id,
                    session_id,
                    discussion_id,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM discussion_assessment_cursors
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (group_id, session_id, discussion_id),
            ).fetchone()
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_message_classification(
        *, group_id: int, session_id: int, discussion_id: int, sequence: int
    ) -> dict:
        group_id, session_id, discussion_id = _scope_values(
            group_id, session_id, discussion_id
        )
        sequence = _as_int(sequence, "sequence")
        conn = db()
        try:
            _scope_row(conn, group_id, session_id, discussion_id)
            message = conn.execute(
                """
                SELECT id, role, sender_type
                FROM messages
                WHERE group_id=? AND session_id=? AND discussion_id=? AND sequence=?
                """,
                (group_id, session_id, discussion_id, sequence),
            ).fetchone()
            if not message:
                raise StateAssessmentBatchError("student_message_not_found")
            if str(message["role"] or message["sender_type"] or "").lower() != "student":
                raise StateAssessmentBatchError("message_not_student")

            segment = conn.execute(
                """
                SELECT s.*, b.completed_at AS batch_completed_at,
                       b.status AS batch_status,
                       b.terminal_status AS batch_terminal_status
                FROM collaboration_state_segments AS s
                JOIN state_assessment_batches AS b
                  ON b.id=s.assessment_batch_id
                WHERE b.group_id=? AND b.session_id=? AND b.discussion_id=?
                  AND s.assessment_status='confirmed'
                  AND s.start_sequence<=? AND s.end_sequence>=?
                  AND (
                      (b.status='succeeded' AND s.source='llm')
                      OR (
                          b.terminal_status IN ('degraded','quarantined')
                          AND s.source='rule'
                          AND s.fallback_reason=?
                      )
                  )
                ORDER BY
                    CASE WHEN b.status='succeeded' AND s.source='llm'
                         THEN 1 ELSE 0 END DESC,
                    b.completed_at DESC, s.confidence DESC, s.id DESC
                LIMIT 1
                """,
                (
                    group_id,
                    session_id,
                    discussion_id,
                    sequence,
                    sequence,
                    FALLBACK_REASON_RETRY_EXHAUSTED,
                ),
            ).fetchone()
            if segment:
                data = dict(segment)
                try:
                    evidence = json.loads(data.get("evidence_sequences") or "[]")
                except (TypeError, ValueError):
                    evidence = []
                return {
                    "semantic_state": data["state_code"],
                    "assessment_status": "confirmed",
                    "segment_id": data["id"],
                    "assessment_batch_id": data["assessment_batch_id"],
                    "confidence": data["confidence"],
                    "evidence_sequences": evidence,
                    "fallback_reason": data.get("fallback_reason"),
                }

            fallback = conn.execute(
                """
                SELECT s.*, b.error_code AS batch_error_code,
                       b.terminal_status AS batch_terminal_status
                FROM collaboration_state_segments AS s
                JOIN state_assessment_batches AS b
                  ON b.id=s.assessment_batch_id
                WHERE b.group_id=? AND b.session_id=? AND b.discussion_id=?
                  AND s.assessment_status='unclassified'
                  AND s.fallback_reason=?
                  AND b.terminal_status IN ('degraded','quarantined')
                  AND s.start_sequence<=? AND s.end_sequence>=?
                ORDER BY COALESCE(b.terminal_at, b.completed_at, b.updated_at) DESC,
                         s.id DESC
                LIMIT 1
                """,
                (
                    group_id,
                    session_id,
                    discussion_id,
                    FALLBACK_REASON_BATCH_UNCLASSIFIED,
                    sequence,
                    sequence,
                ),
            ).fetchone()
            if fallback:
                data = dict(fallback)
                return {
                    "semantic_state": None,
                    "assessment_status": "unclassified",
                    "segment_id": data["id"],
                    "assessment_batch_id": data["assessment_batch_id"],
                    "confidence": data.get("confidence"),
                    "evidence_sequences": [],
                    "fallback_reason": data.get("fallback_reason"),
                    "error_code": data.get("batch_error_code"),
                }

            cursor = conn.execute(
                """
                SELECT observation_status, observation_started_sequence,
                       last_intervention_sequence
                FROM discussion_assessment_cursors
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (group_id, session_id, discussion_id),
            ).fetchone()
            if cursor and cursor["observation_status"] == "observing":
                observation_start = cursor["observation_started_sequence"]
                if observation_start is None:
                    last_intervention = cursor["last_intervention_sequence"]
                    if last_intervention is not None and sequence > int(last_intervention):
                        observation_start = sequence
                expired_through = None
                if observation_start is not None:
                    completed_rounds = conn.execute(
                        """
                        SELECT candidate_end_sequence
                        FROM state_assessment_batches
                        WHERE group_id=? AND session_id=? AND discussion_id=?
                          AND status='succeeded'
                          AND candidate_end_sequence>=?
                        ORDER BY completed_at ASC, id ASC
                        """,
                        (
                            group_id,
                            session_id,
                            discussion_id,
                            int(observation_start),
                        ),
                    ).fetchall()
                    if len(completed_rounds) >= OBSERVATION_MAX_ASSESSMENT_ROUNDS:
                        expired_through = int(
                            completed_rounds[OBSERVATION_MAX_ASSESSMENT_ROUNDS - 1][
                                "candidate_end_sequence"
                            ]
                        )
                if (
                    observation_start is not None
                    and sequence >= int(observation_start)
                    and (expired_through is None or sequence > expired_through)
                ):
                    return {
                        "semantic_state": None,
                        "assessment_status": "observing",
                        "segment_id": None,
                        "assessment_batch_id": None,
                    }
            return {
                "semantic_state": None,
                "assessment_status": "unclassified",
                "segment_id": None,
                "assessment_batch_id": None,
            }
        finally:
            conn.close()

    @staticmethod
    def link_intervention_to_segment(
        intervention_run_id: int,
        *,
        assessment_batch_id: int,
        target_segment_id: int,
        trigger_type: str = None,
        reason_code: str = None,
    ) -> dict:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = _batch_with_scope(conn, assessment_batch_id)
            segment = conn.execute(
                """
                SELECT * FROM collaboration_state_segments
                WHERE id=? AND assessment_batch_id=?
                  AND group_id=? AND session_id=?
                """,
                (
                    _as_int(target_segment_id, "target_segment_id"),
                    batch["id"],
                    batch["group_id"],
                    batch["session_id"],
                ),
            ).fetchone()
            if not segment:
                raise StateAssessmentBatchError("target_segment_scope_mismatch")
            run = conn.execute(
                """
                SELECT * FROM intervention_runs
                WHERE id=? AND group_id=?
                  AND session_id=?
                """,
                (
                    _as_int(intervention_run_id, "intervention_run_id"),
                    batch["group_id"],
                    batch["session_id"],
                ),
            ).fetchone()
            if not run:
                raise StateAssessmentBatchError("intervention_scope_mismatch")
            conn.execute(
                """
                UPDATE intervention_runs
                SET assessment_batch_id=?, target_segment_id=?,
                    trigger_type=COALESCE(?, trigger_type), reason_code=?
                WHERE id=? AND group_id=?
                  AND session_id=?
                """,
                (
                    batch["id"],
                    segment["id"],
                    trigger_type,
                    reason_code,
                    run["id"],
                    batch["group_id"],
                    batch["session_id"],
                ),
            )
            linked = conn.execute(
                "SELECT * FROM intervention_runs WHERE id=?",
                (run["id"],),
            ).fetchone()
            conn.commit()
            return dict(linked)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


__all__ = [
    "StateAssessmentBatchError",
    "StateAssessmentBatchService",
]
