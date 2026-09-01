# -*- coding: utf-8 -*-
"""Shared issue-aware coverage guard for student help requests."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from db import db, parse_dt

logger = logging.getLogger(__name__)


ACTIVE_HELP_STATUSES = frozenset({"QUEUED", "RUNNING", "PENDING", "PROCESSING"})
HANDLED_HELP_STATUSES = frozenset({"COMPLETED", "COMPLETED_WITH_FALLBACK"})
HELP_REQUEST_GRACE_SECONDS = 8

_STATE_ALIASES = {
    "frustration_stuck": "blocked_frustration",
    "conflict": "conflict_tension",
    "off_task": "task_detached",
    "positive": "positive_collaboration",
    "silence": "negative_silence",
}


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_code(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text or text == "unknown":
        return None
    return _STATE_ALIASES.get(text, text)


def _json_ints(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        parsed = value
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, (list, tuple, set)):
        return []
    return sorted({number for item in parsed if (number := _safe_int(item)) is not None})


def _range(start: Any, end: Any) -> tuple[Optional[int], Optional[int]]:
    left = _safe_int(start)
    right = _safe_int(end)
    if left is None and right is None:
        return None, None
    if left is None:
        left = right
    if right is None:
        right = left
    return (left, right) if left <= right else (right, left)


def _range_json(start: Optional[int], end: Optional[int]) -> Optional[dict]:
    if start is None or end is None:
        return None
    return {"start_sequence": start, "end_sequence": end}


def _substantial_overlap(
    left_start: Optional[int],
    left_end: Optional[int],
    right_start: Optional[int],
    right_end: Optional[int],
) -> bool:
    if None in (left_start, left_end, right_start, right_end):
        return False
    overlap = min(left_end, right_end) - max(left_start, right_start) + 1
    if overlap <= 0:
        return False
    shorter = min(left_end - left_start + 1, right_end - right_start + 1)
    return overlap / max(1, shorter) >= 0.5


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        return parse_dt(str(value)) if value else None
    except (TypeError, ValueError):
        return None


class HelpRequestCoverageService:
    """Evaluate whether a help request already owns the target issue."""

    @staticmethod
    def bypassed(reason_code: str) -> dict:
        """Return an explicit not-evaluated result for a non-guard path."""
        return {
            "blocked": False,
            "reason_code": reason_code,
            "help_request_id": None,
            "help_request_ids": [],
            "help_status": None,
            "handled_state_code": None,
            "handled_segment_id": None,
            "handled_evidence_range": None,
            "target_state_code": None,
            "target_segment_id": None,
            "target_evidence_range": None,
            "same_state": False,
            "same_segment": False,
            "evidence_overlap": False,
            "grace_remaining_seconds": 0,
            "grace_active": False,
            "guard_evaluated": False,
            "guard_blocked": False,
        }

    @classmethod
    def evaluate(
        cls,
        group_id: int,
        session_id: int,
        target_state_code: str,
        target_segment_id: int,
        target_start_sequence: int,
        target_end_sequence: int,
        current_time: Any = None,
        *,
        connection=None,
    ) -> dict:
        """Return a structured, auditable coverage decision."""
        owns_connection = connection is None
        conn = connection or db()
        try:
            target = cls._resolve_target(
                conn,
                group_id=group_id,
                session_id=session_id,
                state_code=target_state_code,
                segment_id=target_segment_id,
                start_sequence=target_start_sequence,
                end_sequence=target_end_sequence,
            )
            rows = conn.execute(
                """
                SELECT hr.*,
                       COALESCE(hr.help_request_message_sequence, source.sequence)
                           AS request_sequence,
                       ir.detected_state AS run_state_code,
                       ir.target_segment_id AS run_segment_id,
                       ir.context_from_sequence AS run_evidence_start_sequence,
                       ir.context_to_sequence AS run_evidence_end_sequence,
                       ir.evidence_sequences_json AS run_evidence_sequences,
                       suggestion.state_code AS suggestion_state_code,
                       handled_segment.state_code AS segment_state_code,
                       COALESCE(
                           handled_segment.start_sequence,
                           handled_segment.start_message_id
                       ) AS segment_start_sequence,
                       COALESCE(
                           handled_segment.end_sequence,
                           handled_segment.end_message_id
                       ) AS segment_end_sequence
                  FROM help_requests AS hr
                  LEFT JOIN messages AS source ON source.id=hr.source_message_id
                  LEFT JOIN intervention_runs AS ir ON ir.id=hr.intervention_run_id
                  LEFT JOIN agent_suggestions AS suggestion
                    ON suggestion.help_request_id=hr.id
                  LEFT JOIN collaboration_state_segments AS handled_segment
                    ON handled_segment.id=COALESCE(
                        hr.handled_segment_id, ir.target_segment_id
                    )
                 WHERE hr.group_id=?
                   AND (? IS NULL OR hr.session_id=?)
                   AND UPPER(COALESCE(hr.status, '')) IN (
                       'QUEUED','RUNNING','PENDING','PROCESSING',
                       'COMPLETED','COMPLETED_WITH_FALLBACK'
                   )
                 ORDER BY hr.id DESC
                 LIMIT 20
                """,
                (group_id, session_id, session_id),
            ).fetchall()
            now = _as_datetime(current_time) or datetime.now()
            evaluated = [
                cls._evaluate_row(dict(row), target=target, current_time=now)
                for row in rows
            ]
            blocking = next((item for item in evaluated if item["blocked"]), None)
            if blocking:
                result = blocking
            elif evaluated:
                result = next(
                    (
                        item
                        for item in evaluated
                        if item["reason_code"] == "different_state_new_issue"
                    ),
                    evaluated[0],
                )
            else:
                result = cls._base_result(target)
                result["reason_code"] = "no_relevant_help_request"
            result["help_request_ids"] = [
                int(row["id"]) for row in rows if _safe_int(row["id"]) is not None
            ]
            cls._log_result(group_id, session_id, result)
            return result
        finally:
            if owns_connection:
                conn.close()

    @classmethod
    def resolve_handled_issue(
        cls,
        connection,
        *,
        group_id: int,
        session_id: int = None,
        source_message_id: int = None,
        monitor_run_id: int = None,
        state_assessment_id: int = None,
        detected_state: str = None,
        target_segment_id: int = None,
        evidence_sequences: Any = None,
        cutoff_sequence: int = None,
    ) -> dict:
        """Resolve the issue snapshot persisted when a help response succeeds."""
        request_sequence = None
        if source_message_id is not None:
            source = connection.execute(
                "SELECT sequence FROM messages WHERE id=? AND group_id=?",
                (source_message_id, group_id),
            ).fetchone()
            request_sequence = _safe_int(source["sequence"]) if source else None

        run = None
        if monitor_run_id is not None:
            run = connection.execute(
                """
                SELECT final_state, context_from_sequence, context_to_sequence,
                       evidence_sequences_json, rule_result_json
                  FROM monitor_runs
                 WHERE id=? AND group_id=?
                """,
                (monitor_run_id, group_id),
            ).fetchone()
            run = dict(run) if run else None

        segment_id = _safe_int(target_segment_id)
        if segment_id is None and run:
            try:
                payload = json.loads(run.get("rule_result_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            segment_id = _safe_int(
                ((payload.get("monitor_audit") or {}).get("segment_id"))
                if isinstance(payload, dict)
                else None
            )

        state = _state_code((run or {}).get("final_state")) or _state_code(detected_state)
        segment = cls._find_handled_segment(
            connection,
            group_id=group_id,
            session_id=session_id,
            segment_id=segment_id,
            request_sequence=request_sequence,
            state_code=state,
            monitor_run_id=monitor_run_id,
        )
        if segment:
            segment_id = _safe_int(segment.get("id"))
            state = _state_code(segment.get("state_code")) or state

        if state is None and state_assessment_id is not None:
            assessment = connection.execute(
                """
                SELECT fused_state_code, state_code, llm_state_code, rule_state_code
                  FROM state_assessments
                 WHERE id=? AND group_id=?
                """,
                (state_assessment_id, group_id),
            ).fetchone()
            if assessment:
                state = next(
                    (
                        normalized
                        for value in assessment
                        if (normalized := _state_code(value)) is not None
                    ),
                    None,
                )

        sequences = _json_ints(evidence_sequences)
        if not sequences and segment:
            sequences = _json_ints(segment.get("evidence_sequences"))
        if not sequences and run:
            sequences = _json_ints(run.get("evidence_sequences_json"))

        start = _safe_int((segment or {}).get("start_sequence"))
        end = _safe_int((segment or {}).get("end_sequence"))
        if start is None and sequences:
            start = min(sequences)
        if end is None and sequences:
            end = max(sequences)
        if start is None:
            start = _safe_int((run or {}).get("context_from_sequence"))
        if end is None:
            end = _safe_int((run or {}).get("context_to_sequence"))
        if start is None:
            start = request_sequence
        if end is None:
            end = _safe_int(cutoff_sequence) or request_sequence
        start, end = _range(start, end)
        return {
            "handled_state_code": state,
            "handled_segment_id": segment_id,
            "handled_evidence_start_sequence": start,
            "handled_evidence_end_sequence": end,
        }

    @staticmethod
    def _find_handled_segment(
        connection,
        *,
        group_id: int,
        session_id: int,
        segment_id: int,
        request_sequence: int,
        state_code: str,
        monitor_run_id: int,
    ) -> Optional[dict]:
        if segment_id is not None:
            row = connection.execute(
                """
                SELECT id, state_code,
                       COALESCE(start_sequence, start_message_id)
                           AS start_sequence,
                       COALESCE(end_sequence, end_message_id)
                           AS end_sequence,
                       COALESCE(evidence_sequences, evidence_message_ids_json)
                           AS evidence_sequences,
                       source_run_id
                  FROM collaboration_state_segments
                 WHERE id=? AND group_id=?
                   AND (? IS NULL OR session_id=?)
                """,
                (segment_id, group_id, session_id, session_id),
            ).fetchone()
            if row:
                return dict(row)
        if request_sequence is None:
            return None
        row = connection.execute(
            """
            SELECT id, state_code,
                   COALESCE(start_sequence, start_message_id)
                       AS start_sequence,
                   COALESCE(end_sequence, end_message_id)
                       AS end_sequence,
                   COALESCE(evidence_sequences, evidence_message_ids_json)
                       AS evidence_sequences,
                   source_run_id
              FROM collaboration_state_segments
             WHERE group_id=?
               AND (? IS NULL OR session_id=?)
               AND segment_kind='message_range'
               AND COALESCE(start_sequence, start_message_id)<=?
               AND COALESCE(end_sequence, end_message_id)>=?
             ORDER BY
               CASE WHEN source_run_id=? THEN 0 ELSE 1 END,
               CASE WHEN state_code=? THEN 0 ELSE 1 END,
               is_active_at_batch_end DESC,
               id DESC
             LIMIT 1
            """,
            (
                group_id,
                session_id,
                session_id,
                request_sequence,
                request_sequence,
                monitor_run_id,
                state_code,
            ),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _resolve_target(
        connection,
        *,
        group_id: int,
        session_id: int,
        state_code: str,
        segment_id: int,
        start_sequence: int,
        end_sequence: int,
    ) -> dict:
        normalized_state = _state_code(state_code)
        resolved_segment_id = _safe_int(segment_id)
        start, end = _range(start_sequence, end_sequence)
        if resolved_segment_id is not None:
            row = connection.execute(
                """
                SELECT id, state_code,
                       COALESCE(start_sequence, start_message_id)
                           AS start_sequence,
                       COALESCE(end_sequence, end_message_id)
                           AS end_sequence
                  FROM collaboration_state_segments
                 WHERE id=? AND group_id=?
                   AND (? IS NULL OR session_id=?)
                """,
                (resolved_segment_id, group_id, session_id, session_id),
            ).fetchone()
            if row:
                normalized_state = normalized_state or _state_code(row["state_code"])
                segment_start, segment_end = _range(
                    row["start_sequence"], row["end_sequence"]
                )
                start = segment_start if segment_start is not None else start
                end = segment_end if segment_end is not None else end
        return {
            "state_code": normalized_state,
            "segment_id": resolved_segment_id,
            "start_sequence": start,
            "end_sequence": end,
        }

    @classmethod
    def _evaluate_row(cls, row: dict, *, target: dict, current_time: datetime) -> dict:
        result = cls._base_result(target)
        status = str(row.get("status") or "").upper()
        handled_state = next(
            (
                normalized
                for value in (
                    row.get("handled_state_code"),
                    row.get("segment_state_code"),
                    row.get("run_state_code"),
                    row.get("suggestion_state_code"),
                )
                if (normalized := _state_code(value)) is not None
            ),
            None,
        )
        handled_segment_id = (
            _safe_int(row.get("handled_segment_id"))
            or _safe_int(row.get("run_segment_id"))
        )
        run_sequences = _json_ints(row.get("run_evidence_sequences"))
        handled_start = (
            _safe_int(row.get("handled_evidence_start_sequence"))
            or _safe_int(row.get("segment_start_sequence"))
            or (min(run_sequences) if run_sequences else None)
            or _safe_int(row.get("run_evidence_start_sequence"))
            or _safe_int(row.get("request_sequence"))
        )
        handled_end = (
            _safe_int(row.get("handled_evidence_end_sequence"))
            or _safe_int(row.get("segment_end_sequence"))
            or (max(run_sequences) if run_sequences else None)
            or _safe_int(row.get("run_evidence_end_sequence"))
            or _safe_int(row.get("covered_until_sequence"))
            or _safe_int(row.get("request_sequence"))
        )
        handled_start, handled_end = _range(handled_start, handled_end)
        same_state = bool(
            target["state_code"]
            and handled_state
            and target["state_code"] == handled_state
        )
        same_segment = bool(
            target["segment_id"] is not None
            and handled_segment_id is not None
            and target["segment_id"] == handled_segment_id
        )
        evidence_overlap = _substantial_overlap(
            handled_start,
            handled_end,
            target["start_sequence"],
            target["end_sequence"],
        )
        request_sequence = _safe_int(row.get("request_sequence"))
        request_in_target = bool(
            request_sequence is not None
            and target["start_sequence"] is not None
            and target["end_sequence"] is not None
            and target["start_sequence"] <= request_sequence <= target["end_sequence"]
        )
        created_at = _as_datetime(row.get("created_at"))
        grace_remaining = 0
        if created_at:
            elapsed = max(0.0, (current_time - created_at).total_seconds())
            grace_remaining = max(
                0, int(round(HELP_REQUEST_GRACE_SECONDS - elapsed))
            )
        grace_active = grace_remaining > 0

        result.update(
            {
                "help_request_id": _safe_int(row.get("id")),
                "help_status": status or None,
                "handled_state_code": handled_state,
                "handled_segment_id": handled_segment_id,
                "handled_evidence_range": _range_json(handled_start, handled_end),
                "same_state": same_state,
                "same_segment": same_segment,
                "evidence_overlap": evidence_overlap,
                "grace_remaining_seconds": grace_remaining,
                "grace_active": grace_active,
            }
        )

        if status in ACTIVE_HELP_STATUSES:
            if same_segment or same_state or evidence_overlap or request_in_target:
                return cls._blocked(result, "same_issue_help_in_progress")
            if grace_active:
                return cls._blocked(result, "help_request_race_grace")
            if (
                target["state_code"]
                and handled_state
                and target["state_code"] != handled_state
            ):
                result["reason_code"] = "different_state_new_issue"
            else:
                result["reason_code"] = "no_matching_help_request"
            return result

        if status in HANDLED_HELP_STATUSES:
            if same_segment or (same_state and evidence_overlap):
                return cls._blocked(result, "same_issue_already_handled")
            if same_state and request_in_target:
                return cls._blocked(result, "same_issue_already_handled")
            if (
                target["state_code"]
                and handled_state
                and target["state_code"] != handled_state
            ):
                result["reason_code"] = "different_state_new_issue"
            elif same_state:
                result["reason_code"] = "same_state_new_issue"
            elif request_in_target and not target["state_code"] and not handled_state:
                return cls._blocked(result, "same_issue_already_handled")
            else:
                result["reason_code"] = "no_matching_help_request"
        return result

    @staticmethod
    def _base_result(target: dict) -> dict:
        return {
            "blocked": False,
            "reason_code": None,
            "help_request_id": None,
            "help_request_ids": [],
            "help_status": None,
            "handled_state_code": None,
            "handled_segment_id": None,
            "handled_evidence_range": None,
            "target_state_code": target.get("state_code"),
            "target_segment_id": target.get("segment_id"),
            "target_evidence_range": _range_json(
                target.get("start_sequence"), target.get("end_sequence")
            ),
            "same_state": False,
            "same_segment": False,
            "evidence_overlap": False,
            "grace_remaining_seconds": 0,
            "grace_active": False,
            "guard_evaluated": True,
            "guard_blocked": False,
        }

    @staticmethod
    def _blocked(result: dict, reason_code: str) -> dict:
        result["blocked"] = True
        result["guard_blocked"] = True
        result["reason_code"] = reason_code
        return result

    @staticmethod
    def _log_result(group_id: int, session_id: int, result: dict) -> None:
        logger.info(
            "[help_request_guard] evaluation %s",
            json.dumps(
                {
                    "group_id": group_id,
                    "session_id": session_id,
                    **result,
                },
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ),
        )
