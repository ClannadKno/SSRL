# -*- coding: utf-8 -*-
"""Unified, discussion-scoped scheduling for incremental state assessment.

Rules and periodic scanners may decide *why* an assessment is useful, but only
this module may claim a candidate window and enqueue the LLM worker.  SQLite's
transaction and partial unique index are the final single-flight boundary.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from config import (
    STATE_LLM_CONTEXT_MESSAGES,
    STATE_LLM_FAILURE_BACKOFF_SECONDS,
    STATE_LLM_FAILURE_MAX_ATTEMPTS,
    STATE_LLM_MAX_CANDIDATE_MESSAGES,
    STATE_LLM_MESSAGE_THRESHOLD,
    STATE_LLM_MIN_INTERVAL_SECONDS,
    STATE_LLM_TIME_THRESHOLD_SECONDS,
)
from db import db, now_str, query_all, query_one
from services.agent_mode_service import (
    agent_config_from_session,
    pipeline_mode_from_session,
)
from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
from services.state_assessment_batch_service import StateAssessmentBatchService
from services.three_stage_observation import record_observation_assessment
from services.three_stage_schema import legacy_state_for_sub_state, normalize_canonical_sub_state
from services.three_stage_publish import (
    InterventionDecisionGate,
    ThreeStageInterventionPublisher,
)
from services.three_stage_stage2 import Stage2PipelineService, is_stage2_result
from services.three_stage_stage3 import Stage3PipelineService, is_stage3_enabled


logger = logging.getLogger(__name__)


class _StrategyLeaseHeartbeatError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = str(reason or "room_lease_heartbeat_failed")
        super().__init__(self.reason)

TRIGGER_PRIORITIES = {
    "help_request": 500,
    "rule_high_risk": 400,
    "silence_check": 350,
    "post_intervention_observation": 300,
    "message_count_periodic": 200,
    "time_periodic": 100,
}
TRIGGER_ALIASES = {
    "student_help": "help_request",
    "student_help_request": "help_request",
    "new_message": "message_count_periodic",
    "student_message": "message_count_periodic",
}
ORDINARY_PERIODIC_TRIGGERS = {"message_count_periodic", "time_periodic"}
KNOWN_LLM_STATES = {
    "positive_collaboration",
    "conflict_tension",
    "negative_silence",
    "blocked_frustration",
    "frustration_stuck",
    "task_detached",
    "unknown",
}
TRANSPORT_FAILURES = {
    "authentication_error",
    "rate_limited",
    "network_error",
    "upstream_5xx",
    "llm_error",
    "unknown_error",
}


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid_{name}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{name}") from exc


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _normalize_trigger(trigger_type: str) -> str:
    trigger = str(trigger_type or "").strip()
    trigger = TRIGGER_ALIASES.get(trigger, trigger)
    if trigger not in TRIGGER_PRIORITIES:
        raise ValueError("unsupported_state_assessment_trigger")
    return trigger


def _request_cutoff_sequence(
    *,
    trigger: str,
    trigger_sequence: Optional[int],
    replacement_cutoff_sequence: Optional[int],
) -> Optional[int]:
    """Return the immutable upper bound for a trigger/replacement request.

    Ordinary periodic requests intentionally keep their existing bounded
    aggregation behavior.  Explicit triggers and replacement requests instead
    freeze the candidate at the message that caused the request.
    """

    if replacement_cutoff_sequence is not None:
        return int(replacement_cutoff_sequence)
    if trigger_sequence is not None and trigger not in ORDINARY_PERIODIC_TRIGGERS:
        return int(trigger_sequence)
    return None


def _continuation_contract(batch: dict) -> dict:
    """Resolve the next request without changing the completed batch window."""

    queued_trigger = str(batch.get("continuation_trigger_type") or "").strip()
    if queued_trigger:
        return {
            "trigger_type": queued_trigger,
            "trigger_sequence": batch.get("continuation_trigger_sequence"),
            "fixed_candidate_start_sequence": batch.get(
                "continuation_candidate_start_sequence"
            ),
            "fixed_candidate_end_sequence": batch.get(
                "continuation_candidate_end_sequence"
            ),
            "replacement_of_pipeline_run_id": batch.get(
                "continuation_replacement_of_pipeline_run_id"
            ),
            "replacement_reason": batch.get("continuation_replacement_reason"),
            "replacement_trigger_message_id": batch.get(
                "continuation_replacement_trigger_message_id"
            ),
            "replacement_cutoff_sequence": batch.get(
                "continuation_replacement_cutoff_sequence"
            ),
        }

    current_cutoff = _request_cutoff_sequence(
        trigger=str(batch.get("trigger_type") or "message_count_periodic"),
        trigger_sequence=batch.get("trigger_sequence"),
        replacement_cutoff_sequence=batch.get("replacement_cutoff_sequence"),
    )
    if (
        current_cutoff is not None
        and current_cutoff > int(batch.get("candidate_end_sequence") or 0)
    ):
        return {
            "trigger_type": batch.get("trigger_type") or "message_count_periodic",
            "trigger_sequence": batch.get("trigger_sequence"),
            "fixed_candidate_start_sequence": None,
            "fixed_candidate_end_sequence": None,
            "replacement_of_pipeline_run_id": batch.get(
                "replacement_of_pipeline_run_id"
            ),
            "replacement_reason": batch.get("replacement_reason"),
            "replacement_trigger_message_id": batch.get(
                "replacement_trigger_message_id"
            ),
            "replacement_cutoff_sequence": batch.get(
                "replacement_cutoff_sequence"
            ),
        }

    return {
        "trigger_type": "message_count_periodic",
        "trigger_sequence": None,
        "fixed_candidate_start_sequence": None,
        "fixed_candidate_end_sequence": None,
        "replacement_of_pipeline_run_id": None,
        "replacement_reason": None,
        "replacement_trigger_message_id": None,
        "replacement_cutoff_sequence": None,
    }


def _normalize_batch_error_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if code in {"read_timeout", "connect_timeout"}:
        return code
    if code in {"empty_response", "no_output"}:
        return "empty_response"
    if code in {
        "json_parse_error",
        "truncated_response",
        "reasoning_budget_exhausted",
    }:
        return code
    if code == "schema_validation_error":
        return code
    if code in TRANSPORT_FAILURES:
        return "llm_transport_error"
    return "application_error"


def _log_request(event: str, payload: dict) -> None:
    fields = {
        key: payload.get(key)
        for key in (
            "group_id",
            "session_id",
            "discussion_id",
            "trigger_type",
            "trigger_sequence",
            "last_finalized_sequence",
            "last_scheduled_sequence",
            "candidate_start_sequence",
            "candidate_end_sequence",
            "pending_or_running",
            "rerun_requested",
            "reason",
            "window_key",
            "assessment_batch_id",
            "error_code",
            "terminal_status",
            "cursor_before",
            "cursor_after",
        )
        if payload.get(key) is not None
    }
    logger.info(
        "[state_assessment_scheduler] %s %s",
        event,
        json.dumps(fields, ensure_ascii=False, separators=(",", ":"), default=str),
    )


def _scope_row(conn, group_id: int, session_id: int, discussion_id: int):
    return conn.execute(
        """
        SELECT gsd.*, es.status AS session_status, es.session_no, es.task_id,
               es.agent_mode, es.strategy_agent_enabled,
               es.emotion_agent_enabled,
               es.research_state_monitoring_enabled,
               g.state AS group_state
        FROM group_session_discussions AS gsd
        JOIN experiment_sessions AS es ON es.id=gsd.session_id
        JOIN groups AS g ON g.id=gsd.group_id
        WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
        """,
        (discussion_id, group_id, session_id),
    ).fetchone()


def _student_rows_after_cursor(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    after_sequence: int,
):
    return conn.execute(
        """
        SELECT m.id, m.sequence, m.created_at
        FROM messages AS m
        JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
          AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
          AND m.sequence IS NOT NULL AND m.sequence>?
        ORDER BY m.sequence ASC, m.id ASC
        """,
        (group_id, session_id, discussion_id, after_sequence),
    ).fetchall()


def _student_rows_in_window(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    start_sequence: int,
    end_sequence: int,
):
    return conn.execute(
        """
        SELECT m.id, m.sequence, m.created_at
        FROM messages AS m
        JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
          AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
          AND m.sequence IS NOT NULL AND m.sequence BETWEEN ? AND ?
        ORDER BY m.sequence ASC, m.id ASC
        """,
        (
            group_id,
            session_id,
            discussion_id,
            int(start_sequence),
            int(end_sequence),
        ),
    ).fetchall()


def _student_message_id_at_or_before(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    cutoff_sequence: Optional[int],
):
    if cutoff_sequence is None:
        return None
    row = conn.execute(
        """
        SELECT m.id
        FROM messages AS m
        JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
          AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
          AND m.sequence IS NOT NULL AND m.sequence<=?
        ORDER BY m.sequence DESC, m.id DESC
        LIMIT 1
        """,
        (group_id, session_id, discussion_id, int(cutoff_sequence)),
    ).fetchone()
    return int(row["id"]) if row else None


def _context_bounds(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    candidate_start: int,
):
    limit = max(0, int(STATE_LLM_CONTEXT_MESSAGES or 0))
    if limit <= 0:
        return None, None
    rows = conn.execute(
        """
        SELECT m.sequence
        FROM messages AS m
        JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
          AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
          AND m.sequence IS NOT NULL AND m.sequence<?
        ORDER BY m.sequence DESC, m.id DESC
        LIMIT ?
        """,
        (group_id, session_id, discussion_id, candidate_start, limit),
    ).fetchall()
    sequences = sorted({int(row["sequence"]) for row in rows})
    if not sequences:
        return None, None
    return sequences[0], sequences[-1]


def _window_key(group_id: int, session_id: int, discussion_id: int, start: int, end: int) -> str:
    from services.state_assessment_batch_service import _window_key as batch_window_key

    return batch_window_key(group_id, session_id, discussion_id, start, end)


def _enqueue_batch(batch_id: int, *, delay: int = 0) -> None:
    from agent.monitoring_tasks import process_state_assessment_batch

    process_state_assessment_batch.schedule(
        args=(int(batch_id),),
        delay=max(0, int(delay or 0)),
        priority=40,
    )


def _is_due(reference: Any, threshold_seconds: int, *, current: datetime) -> bool:
    parsed = _parse_dt(reference)
    if not parsed:
        return True
    return (current - parsed).total_seconds() >= max(0, int(threshold_seconds or 0))


def request_state_assessment(
    group_id: int,
    session_id: int,
    discussion_id: int,
    trigger_type: str,
    trigger_sequence: int = None,
    *,
    continuation: bool = False,
    fixed_candidate_start_sequence: int = None,
    fixed_candidate_end_sequence: int = None,
    replacement_of_pipeline_run_id: int = None,
    replacement_reason: str = None,
    replacement_trigger_message_id: int = None,
    replacement_cutoff_sequence: int = None,
) -> dict:
    """Claim and enqueue one immutable candidate window.

    Duplicate callers either receive the existing window or set its rerun flag;
    no caller waits for the LLM and only the transaction winner enqueues work.
    """
    group_id = _as_int(group_id, "group_id")
    session_id = _as_int(session_id, "session_id")
    discussion_id = _as_int(discussion_id, "discussion_id")
    trigger_sequence = None if trigger_sequence is None else _as_int(trigger_sequence, "trigger_sequence")
    fixed_candidate_start_sequence = (
        None
        if fixed_candidate_start_sequence is None
        else _as_int(
            fixed_candidate_start_sequence,
            "fixed_candidate_start_sequence",
        )
    )
    fixed_candidate_end_sequence = (
        None
        if fixed_candidate_end_sequence is None
        else _as_int(
            fixed_candidate_end_sequence,
            "fixed_candidate_end_sequence",
        )
    )
    if (fixed_candidate_start_sequence is None) != (
        fixed_candidate_end_sequence is None
    ):
        raise ValueError("incomplete_fixed_candidate_window")
    if (
        fixed_candidate_start_sequence is not None
        and fixed_candidate_start_sequence > fixed_candidate_end_sequence
    ):
        raise ValueError("invalid_fixed_candidate_window")
    replacement_of_pipeline_run_id = (
        None
        if replacement_of_pipeline_run_id is None
        else _as_int(replacement_of_pipeline_run_id, "replacement_of_pipeline_run_id")
    )
    replacement_trigger_message_id = (
        None
        if replacement_trigger_message_id is None
        else _as_int(replacement_trigger_message_id, "replacement_trigger_message_id")
    )
    replacement_cutoff_sequence = (
        None
        if replacement_cutoff_sequence is None
        else _as_int(replacement_cutoff_sequence, "replacement_cutoff_sequence")
    )
    replacement_reason = (
        str(replacement_reason or "continuation_request")
        if replacement_of_pipeline_run_id is not None
        else None
    )
    trigger = _normalize_trigger(trigger_type)
    priority = TRIGGER_PRIORITIES[trigger]
    timestamp = now_str()
    current = datetime.now()
    max_candidate = max(1, int(STATE_LLM_MAX_CANDIDATE_MESSAGES or 8))
    message_threshold = max(1, int(STATE_LLM_MESSAGE_THRESHOLD or 4))
    max_attempts = max(1, int(STATE_LLM_FAILURE_MAX_ATTEMPTS or 2))
    created = False
    should_enqueue = False
    result = {
        "group_id": group_id,
        "session_id": session_id,
        "discussion_id": discussion_id,
        "trigger_type": trigger,
        "trigger_sequence": trigger_sequence,
        "fixed_candidate_start_sequence": fixed_candidate_start_sequence,
        "fixed_candidate_end_sequence": fixed_candidate_end_sequence,
        "replacement_of_pipeline_run_id": replacement_of_pipeline_run_id,
        "replacement_reason": replacement_reason,
        "created": False,
        "enqueued": False,
    }

    session_row = query_one(
        "SELECT * FROM experiment_sessions WHERE id=?", (session_id,)
    )
    session_config = agent_config_from_session(dict(session_row or {}))
    pipeline_mode = pipeline_mode_from_session(dict(session_row or {}))
    result["pipeline_mode"] = pipeline_mode
    if session_config.get("configuration_error"):
        result.update({"skipped": True, "reason": "invalid_agent_configuration"})
        _log_request("skipped", result)
        return result
    if pipeline_mode is None:
        result.update({"skipped": True, "reason": "state_monitoring_disabled"})
        _log_request("skipped", result)
        return result

    recovered = StateAssessmentBatchService.recover_exhausted_batches(
        group_id=group_id,
        session_id=session_id,
        discussion_id=discussion_id,
    )
    if recovered:
        result["recovered_terminal_batches"] = [
            item["batch"]["id"] for item in recovered if item.get("batch")
        ]
    pipeline_recovery = Stage2PipelineService.recover_terminal_batch_orphans(
        group_id=group_id,
        session_id=session_id,
        discussion_id=discussion_id,
    )
    recovered_pipeline_ids = [
        pipeline_id
        for item in pipeline_recovery
        for pipeline_id in item.get("pipeline_run_ids", [])
    ]
    if recovered_pipeline_ids:
        result["recovered_terminal_pipeline_run_ids"] = recovered_pipeline_ids

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        scope = _scope_row(conn, group_id, session_id, discussion_id)
        if not scope:
            result.update({"skipped": True, "reason": "discussion_scope_mismatch"})
            conn.commit()
            _log_request("skipped", result)
            return result
        if (
            scope["status"] != "running"
            or scope["session_status"] != "running"
            or str(scope["group_state"] or "").upper() == "CLOSED"
        ):
            result.update({"skipped": True, "reason": "discussion_not_running"})
            conn.commit()
            _log_request("skipped", result)
            return result

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
        cursor = conn.execute(
            """
            SELECT * FROM discussion_assessment_cursors
            WHERE group_id=? AND session_id=? AND discussion_id=?
            """,
            (group_id, session_id, discussion_id),
        ).fetchone()
        last_finalized = int(cursor["last_finalized_student_sequence"] or 0)
        last_scheduled = max(
            last_finalized,
            int(cursor["last_scheduled_student_sequence"] or 0),
        )
        result["last_finalized_sequence"] = last_finalized
        result["last_scheduled_sequence"] = last_scheduled
        unfinalized_rows = _student_rows_after_cursor(
            conn,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
            after_sequence=last_scheduled,
        )
        result["new_student_message_count"] = len(unfinalized_rows)

        if replacement_of_pipeline_run_id is not None:
            if replacement_cutoff_sequence is None:
                replacement_cutoff_sequence = (
                    fixed_candidate_end_sequence
                    if fixed_candidate_end_sequence is not None
                    else trigger_sequence
                    if trigger_sequence is not None
                    else (
                        int(unfinalized_rows[-1]["sequence"])
                        if unfinalized_rows
                        else None
                    )
                )
            if replacement_trigger_message_id is None:
                replacement_trigger_message_id = _student_message_id_at_or_before(
                    conn,
                    group_id=group_id,
                    session_id=session_id,
                    discussion_id=discussion_id,
                    cutoff_sequence=replacement_cutoff_sequence,
                )
            result.update(
                {
                    "replacement_trigger_message_id": replacement_trigger_message_id,
                    "replacement_cutoff_sequence": replacement_cutoff_sequence,
                }
            )
        request_cutoff_sequence = (
            fixed_candidate_end_sequence
            if fixed_candidate_end_sequence is not None
            else _request_cutoff_sequence(
                trigger=trigger,
                trigger_sequence=trigger_sequence,
                replacement_cutoff_sequence=replacement_cutoff_sequence,
            )
        )
        result["request_cutoff_sequence"] = request_cutoff_sequence

        active = conn.execute(
            """
            SELECT * FROM state_assessment_batches
            WHERE group_id=? AND session_id=? AND discussion_id=?
              AND status IN ('pending','running')
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (group_id, session_id, discussion_id),
        ).fetchone()
        if active:
            active_row = dict(active)
            active_start = int(active_row["candidate_start_sequence"])
            active_end = int(active_row["candidate_end_sequence"])
            same_window = (
                request_cutoff_sequence == active_end
                and (
                    fixed_candidate_start_sequence is None
                    or fixed_candidate_start_sequence == active_start
                )
            )

            effective_trigger = active_row["trigger_type"]
            effective_priority = int(active_row["request_priority"] or 0)
            effective_last_trigger = active_row["last_trigger_sequence"]
            current_replacement_of = active_row["replacement_of_pipeline_run_id"]
            current_replacement_reason = active_row["replacement_reason"]
            current_replacement_message = active_row[
                "replacement_trigger_message_id"
            ]
            current_replacement_cutoff = active_row[
                "replacement_cutoff_sequence"
            ]
            if same_window and priority > effective_priority:
                effective_trigger = trigger
                effective_priority = priority
                effective_last_trigger = trigger_sequence
            if (
                same_window
                and replacement_of_pipeline_run_id is not None
                and current_replacement_of is None
            ):
                current_replacement_of = replacement_of_pipeline_run_id
                current_replacement_reason = replacement_reason
                current_replacement_message = replacement_trigger_message_id
                current_replacement_cutoff = replacement_cutoff_sequence

            queued_trigger = active_row["continuation_trigger_type"]
            queued_sequence = active_row["continuation_trigger_sequence"]
            queued_priority = int(
                active_row["continuation_request_priority"] or 0
            )
            queued_start = active_row[
                "continuation_candidate_start_sequence"
            ]
            queued_end = active_row["continuation_candidate_end_sequence"]
            queued_replacement_of = active_row[
                "continuation_replacement_of_pipeline_run_id"
            ]
            queued_replacement_reason = active_row[
                "continuation_replacement_reason"
            ]
            queued_replacement_message = active_row[
                "continuation_replacement_trigger_message_id"
            ]
            queued_replacement_cutoff = active_row[
                "continuation_replacement_cutoff_sequence"
            ]

            should_queue_request = (
                request_cutoff_sequence is not None and not same_window
            )
            incoming_start = fixed_candidate_start_sequence
            incoming_end = fixed_candidate_end_sequence
            if should_queue_request and request_cutoff_sequence < active_end:
                incoming_start = max(last_finalized + 1, active_start)
                incoming_end = request_cutoff_sequence
                if incoming_start > incoming_end:
                    should_queue_request = False

            existing_queued_cutoff = None
            if queued_trigger:
                existing_queued_cutoff = (
                    queued_end
                    if queued_end is not None
                    else _request_cutoff_sequence(
                        trigger=str(queued_trigger),
                        trigger_sequence=queued_sequence,
                        replacement_cutoff_sequence=queued_replacement_cutoff,
                    )
                )
            replace_queued_request = should_queue_request and (
                existing_queued_cutoff is None
                or request_cutoff_sequence < int(existing_queued_cutoff)
                or (
                    request_cutoff_sequence == int(existing_queued_cutoff)
                    and priority > queued_priority
                )
            )
            if replace_queued_request:
                queued_trigger = trigger
                queued_sequence = trigger_sequence
                queued_priority = priority
                queued_start = incoming_start
                queued_end = incoming_end
                queued_replacement_of = replacement_of_pipeline_run_id
                queued_replacement_reason = replacement_reason
                queued_replacement_message = replacement_trigger_message_id
                queued_replacement_cutoff = replacement_cutoff_sequence
            elif (
                should_queue_request
                and existing_queued_cutoff is not None
                and request_cutoff_sequence == int(existing_queued_cutoff)
                and replacement_of_pipeline_run_id is not None
                and queued_replacement_of is None
            ):
                queued_replacement_of = replacement_of_pipeline_run_id
                queued_replacement_reason = replacement_reason
                queued_replacement_message = replacement_trigger_message_id
                queued_replacement_cutoff = replacement_cutoff_sequence

            conn.execute(
                """
                UPDATE state_assessment_batches
                SET rerun_requested=1,
                    trigger_type=?, request_priority=?,
                    last_trigger_sequence=?,
                    continuation_trigger_type=?,
                    continuation_trigger_sequence=?,
                    continuation_request_priority=?,
                    continuation_candidate_start_sequence=?,
                    continuation_candidate_end_sequence=?,
                    continuation_replacement_of_pipeline_run_id=?,
                    continuation_replacement_reason=?,
                    continuation_replacement_trigger_message_id=?,
                    continuation_replacement_cutoff_sequence=?,
                    replacement_of_pipeline_run_id=?,
                    replacement_reason=?,
                    replacement_trigger_message_id=?,
                    replacement_cutoff_sequence=?,
                    updated_at=?
                WHERE id=? AND status IN ('pending','running')
                """,
                (
                    effective_trigger,
                    effective_priority,
                    effective_last_trigger,
                    queued_trigger,
                    queued_sequence,
                    queued_priority,
                    queued_start,
                    queued_end,
                    queued_replacement_of,
                    queued_replacement_reason,
                    queued_replacement_message,
                    queued_replacement_cutoff,
                    current_replacement_of,
                    current_replacement_reason,
                    current_replacement_message,
                    current_replacement_cutoff,
                    timestamp,
                    active_row["id"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (active_row["id"],),
            ).fetchone()
            conn.commit()
            result.update(
                {
                    "pending_or_running": True,
                    "rerun_requested": True,
                    "reason": "assessment_in_progress",
                    "assessment_batch_id": row["id"],
                    "window_key": row["window_key"],
                    "candidate_start_sequence": row["candidate_start_sequence"],
                    "candidate_end_sequence": row["candidate_end_sequence"],
                    "continuation_trigger_type": row[
                        "continuation_trigger_type"
                    ],
                    "continuation_trigger_sequence": row[
                        "continuation_trigger_sequence"
                    ],
                    "continuation_candidate_start_sequence": row[
                        "continuation_candidate_start_sequence"
                    ],
                    "continuation_candidate_end_sequence": row[
                        "continuation_candidate_end_sequence"
                    ],
                    "replacement_trigger_message_id": row["replacement_trigger_message_id"],
                    "replacement_cutoff_sequence": row["replacement_cutoff_sequence"],
                    "batch": dict(row),
                }
            )
            _log_request("rerun_requested", result)
            return result

        if fixed_candidate_start_sequence is not None:
            student_rows = _student_rows_in_window(
                conn,
                group_id=group_id,
                session_id=session_id,
                discussion_id=discussion_id,
                start_sequence=fixed_candidate_start_sequence,
                end_sequence=fixed_candidate_end_sequence,
            )
        else:
            student_rows = [
                row
                for row in unfinalized_rows
                if request_cutoff_sequence is None
                or int(row["sequence"]) <= request_cutoff_sequence
            ]
        result["candidate_student_message_count"] = len(student_rows)
        if not student_rows:
            reason = (
                "no_student_messages_in_fixed_window"
                if fixed_candidate_start_sequence is not None
                else "no_student_messages_before_trigger_cutoff"
                if request_cutoff_sequence is not None and unfinalized_rows
                else "no_new_student_messages"
            )
            result.update({"skipped": True, "reason": reason})
            conn.commit()
            _log_request("skipped", result)
            return result

        if not continuation and trigger == "message_count_periodic" and len(student_rows) < message_threshold:
            result.update({"skipped": True, "reason": "message_threshold_not_reached"})
            conn.commit()
            _log_request("skipped", result)
            return result
        if not continuation and trigger == "time_periodic":
            time_reference = cursor["last_assessment_completed_at"] or scope["started_at"] or scope["created_at"]
            if not _is_due(time_reference, STATE_LLM_TIME_THRESHOLD_SECONDS, current=current):
                result.update({"skipped": True, "reason": "time_threshold_not_reached"})
                conn.commit()
                _log_request("skipped", result)
                return result
        if (
            not continuation
            and trigger in ORDINARY_PERIODIC_TRIGGERS
            and cursor["last_assessment_completed_at"]
            and not _is_due(cursor["last_assessment_completed_at"], STATE_LLM_MIN_INTERVAL_SECONDS, current=current)
        ):
            result.update({"skipped": True, "reason": "minimum_interval_not_reached"})
            conn.commit()
            _log_request("skipped", result)
            return result

        claimed_rows = student_rows[:max_candidate]
        candidate_start = int(claimed_rows[0]["sequence"])
        candidate_end = int(claimed_rows[-1]["sequence"])
        context_start, context_end = _context_bounds(
            conn,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
            candidate_start=candidate_start,
        )
        stable_key = _window_key(group_id, session_id, discussion_id, candidate_start, candidate_end)
        initial_rerun = (
            len(student_rows) > len(claimed_rows)
            or (
                fixed_candidate_start_sequence is None
                and len(unfinalized_rows) > len(claimed_rows)
            )
        )
        cur = conn.execute(
            """
            INSERT INTO state_assessment_batches(
                group_id, session_id, session_no, task_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence,
                context_start_sequence, context_end_sequence,
                trigger_type, trigger_sequence, window_key, status,
                rerun_requested, request_priority, last_trigger_sequence,
                replacement_of_pipeline_run_id, replacement_reason,
                replacement_trigger_message_id, replacement_cutoff_sequence,
                attempt_count, max_attempts, student_sequences_json,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,0,?,?,?,?)
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
                1 if initial_rerun else 0,
                priority,
                trigger_sequence,
                replacement_of_pipeline_run_id,
                replacement_reason,
                replacement_trigger_message_id,
                replacement_cutoff_sequence,
                max_attempts,
                json.dumps(
                    [int(row["sequence"]) for row in claimed_rows],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
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
            (group_id, session_id, discussion_id, candidate_start, candidate_end),
        ).fetchone()
        if not created and row and row["status"] == "failed":
            retry_at = _parse_dt(row["next_retry_at"])
            retry_due = retry_at is None or retry_at <= current
            if int(row["attempt_count"] or 0) < int(row["max_attempts"] or max_attempts) and retry_due:
                retry_cur = conn.execute(
                    """
                    UPDATE state_assessment_batches
                    SET status='pending', trigger_type=?, request_priority=MAX(request_priority, ?),
                        last_trigger_sequence=COALESCE(?, last_trigger_sequence),
                        error_code=NULL, error_detail=NULL, next_retry_at=NULL,
                        completed_at=NULL, updated_at=?
                    WHERE id=? AND status='failed' AND attempt_count<max_attempts
                    """,
                    (trigger, priority, trigger_sequence, timestamp, row["id"]),
                )
                should_enqueue = retry_cur.rowcount == 1
                row = conn.execute(
                    "SELECT * FROM state_assessment_batches WHERE id=?", (row["id"],)
                ).fetchone()
            else:
                result["reason"] = "retry_backoff" if not retry_due else "retry_limit_reached"
        elif created:
            should_enqueue = True

        if row and row["status"] == "succeeded":
            result["reason"] = "window_already_succeeded"
        elif row and row["status"] in ("pending", "running") and not should_enqueue:
            result["reason"] = result.get("reason") or "window_already_claimed"
        conn.execute(
            """
            UPDATE discussion_assessment_cursors
            SET last_assessment_requested_at=?, updated_at=?
            WHERE group_id=? AND session_id=? AND discussion_id=?
            """,
            (timestamp, timestamp, group_id, session_id, discussion_id),
        )
        conn.commit()
        result.update(
            {
                "created": created,
                "rerun_requested": bool(row["rerun_requested"]) if row else initial_rerun,
                "assessment_batch_id": row["id"] if row else None,
                "window_key": row["window_key"] if row else stable_key,
                "candidate_start_sequence": candidate_start,
                "candidate_end_sequence": candidate_end,
                "batch": dict(row) if row else None,
            }
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if should_enqueue and result.get("assessment_batch_id"):
        try:
            _enqueue_batch(result["assessment_batch_id"])
            StateAssessmentBatchService.mark_enqueued(result["assessment_batch_id"])
            result["enqueued"] = True
        except Exception as exc:
            retry_at = (datetime.now() + timedelta(seconds=max(1, int(STATE_LLM_FAILURE_BACKOFF_SECONDS or 30)))).strftime("%Y-%m-%d %H:%M:%S")
            StateAssessmentBatchService.mark_batch_failed(
                result["assessment_batch_id"],
                error_code="application_error",
                error_detail=str(exc)[:500],
                next_retry_at=retry_at,
            )
            result.update({"enqueued": False, "reason": "enqueue_failed", "error": str(exc)})
    _log_request("requested", result)
    return result


def resolve_message_scope(*, group_id: int, sequence: int) -> Optional[dict]:
    row = query_one(
        """
        SELECT m.group_id, m.session_id, m.sequence, m.discussion_id,
               gsd.status AS discussion_status
        FROM messages AS m
        JOIN group_session_discussions AS gsd
          ON gsd.id=m.discussion_id
         AND gsd.group_id=m.group_id
         AND gsd.session_id=m.session_id
        WHERE m.group_id=? AND m.sequence=?
        LIMIT 1
        """,
        (_as_int(group_id, "group_id"), _as_int(sequence, "sequence")),
    )
    return dict(row) if row else None


def request_state_assessment_for_message(
    *, group_id: int, sequence: int, trigger_type: str
) -> dict:
    scope = resolve_message_scope(group_id=group_id, sequence=sequence)
    if not scope:
        return {
            "group_id": int(group_id),
            "trigger_sequence": int(sequence),
            "skipped": True,
            "enqueued": False,
            "reason": "message_discussion_scope_not_found",
        }
    return request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type=trigger_type,
        trigger_sequence=sequence,
    )


def _segments_from_legacy_result(batch: dict, llm_result: dict) -> list[dict]:
    state = str(
        (llm_result or {}).get("primary_state")
        or (llm_result or {}).get("state_code")
        or "unknown"
    ).strip()
    if state not in KNOWN_LLM_STATES:
        return []
    rows = query_all(
        """
        SELECT id, sequence FROM messages
        WHERE group_id=? AND session_id=? AND discussion_id=?
          AND sequence BETWEEN ? AND ?
        """,
        (
            batch["group_id"],
            batch["session_id"],
            batch["discussion_id"],
            batch["candidate_start_sequence"],
            batch["candidate_end_sequence"],
        ),
    )
    id_to_sequence = {int(row["id"]): int(row["sequence"]) for row in rows}
    all_evidence_sequences = sorted(
        {
            id_to_sequence[int(value)]
            for value in ((llm_result or {}).get("evidence_message_ids") or [])
            if str(value).lstrip("-").isdigit() and int(value) in id_to_sequence
        }
    )
    # The legacy single-state contract is retained in this batch, but context
    # evidence must never create a candidate segment.  No candidate evidence
    # therefore means a successful empty-segment assessment.
    if not all_evidence_sequences:
        return []
    evidence_sequences = all_evidence_sequences[:3]
    try:
        confidence = float((llm_result or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    return [
        {
            "state": state,
            "start_sequence": min(all_evidence_sequences),
            "end_sequence": max(all_evidence_sequences),
            "confidence": confidence,
            "evidence_sequences": evidence_sequences,
            "segment_order": 0,
            "source": "llm",
            "assessment_status": "confirmed",
            "is_active_at_batch_end": max(all_evidence_sequences)
            == int(batch["candidate_end_sequence"]),
            "trigger_type": batch["trigger_type"],
        }
    ]


def _segments_from_multi_result(batch: dict, llm_result: dict) -> list[dict]:
    """Project validated detector output into the transactional segment shape.

    A missing ``segments`` key is accepted only for the Batch 2 compatibility
    path used by older callers/tests.  New Batch 3 detector responses always
    include the key, including successful empty assessments.
    """
    if "segments" not in (llm_result or {}):
        return _segments_from_legacy_result(batch, llm_result)
    active_index = (llm_result or {}).get("active_segment_index")
    projected = []
    for index, item in enumerate((llm_result or {}).get("segments") or []):
        canonical = item.get("canonical_sub_state") or item.get("canonical_sub_state_code")
        state = item.get("state")
        if canonical:
            canonical = normalize_canonical_sub_state(canonical)
            state = legacy_state_for_sub_state(canonical)
        projected.append(
            {
                "state": state,
                "raw_sub_state_code": item.get("raw_sub_state") or item.get("raw_sub_state_code"),
                "canonical_sub_state_code": canonical,
                "secondary_tags": list(item.get("secondary_tags") or []),
                "start_sequence": item.get("start_sequence"),
                "end_sequence": item.get("end_sequence"),
                "confidence": item.get("confidence"),
                "evidence_sequences": list(
                    item.get("evidence_sequences")
                    or item.get("evidence_message_ids")
                    or []
                ),
                "segment_order": index,
                "source": "llm",
                "assessment_status": "confirmed",
                "is_active_at_batch_end": index == active_index,
                "trigger_type": batch["trigger_type"],
            }
        )
    return projected


def _finalize_terminal_pipeline_siblings(batch_id: int) -> dict:
    """Best-effort bridge; the idempotent scope scan retries any audit miss."""
    try:
        return Stage2PipelineService.finalize_terminal_batch_siblings(batch_id)
    except Exception as exc:
        logger.warning(
            "[state_assessment_batch] sibling terminalization deferred %s",
            json.dumps(
                {
                    "event": "state_assessment_pipeline_terminalization_deferred",
                    "batch_id": int(batch_id),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return {
            "finalized": False,
            "reason": "pipeline_terminalization_deferred",
            "assessment_batch_id": int(batch_id),
            "error_type": type(exc).__name__,
            "pipeline_run_ids": [],
        }


def _schedule_limited_retry(batch: dict, *, error_code: str, error_detail: str) -> dict:
    normalized_error = _normalize_batch_error_code(error_code)
    logger.warning(
        "[state_assessment_batch] failure %s",
        json.dumps(
            {
                "event": "state_assessment_batch_failure",
                "group_id": batch.get("group_id"),
                "session_id": batch.get("session_id"),
                "discussion_id": batch.get("discussion_id"),
                "batch_id": batch.get("id"),
                "window_start": batch.get("candidate_start_sequence"),
                "window_end": batch.get("candidate_end_sequence"),
                "attempt_count": batch.get("attempt_count"),
                "max_attempts": batch.get("max_attempts"),
                "error_code": normalized_error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    if error_code in {
        "truncated_response",
        "json_parse_error",
        "schema_validation_error",
        "reasoning_budget_exhausted",
    }:
        # LLMStateDetector already performed the one allowed compact schema
        # repair call.  Re-enqueueing the whole immutable batch here would turn
        # two model calls into an unbounded/duplicated structured-output loop.
        terminal = StateAssessmentBatchService.terminalize_exhausted_batch(
            batch["id"],
            error_code=normalized_error,
            error_detail=error_detail[:500] if error_detail else None,
            force=True,
        )
        pipeline_recovery = _finalize_terminal_pipeline_siblings(batch["id"])
        return {
            "retried": False,
            "batch": terminal["batch"],
            "reason": "structured_output_retry_exhausted",
            "terminal": terminal,
            "pipeline_recovery": pipeline_recovery,
        }
    attempt_count = int(batch.get("attempt_count") or 0)
    max_attempts = int(batch.get("max_attempts") or 1)
    if attempt_count >= max_attempts:
        terminal = StateAssessmentBatchService.terminalize_exhausted_batch(
            batch["id"],
            error_code=normalized_error,
            error_detail=error_detail[:500] if error_detail else None,
        )
        pipeline_recovery = _finalize_terminal_pipeline_siblings(batch["id"])
        return {
            "retried": False,
            "batch": terminal["batch"],
            "reason": "retry_limit_reached",
            "terminal": terminal,
            "pipeline_recovery": pipeline_recovery,
        }
    delay = max(1, int(STATE_LLM_FAILURE_BACKOFF_SECONDS or 30)) * max(1, attempt_count)
    next_retry_at = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
    failed = StateAssessmentBatchService.mark_batch_failed(
        batch["id"],
        error_code=normalized_error,
        error_detail=error_detail[:500] if error_detail else None,
        next_retry_at=next_retry_at,
    )
    prepared = StateAssessmentBatchService.prepare_retry(
        batch["id"], next_retry_at=next_retry_at
    )
    if not prepared["prepared"]:
        return {"retried": False, "batch": prepared["batch"], "reason": "retry_not_prepared"}
    try:
        _enqueue_batch(batch["id"], delay=delay)
        StateAssessmentBatchService.mark_enqueued(batch["id"])
        logger.info(
            "[state_assessment_batch] retry_scheduled %s",
            json.dumps(
                {
                    "event": "state_assessment_batch_retry_scheduled",
                    "group_id": batch.get("group_id"),
                    "session_id": batch.get("session_id"),
                    "discussion_id": batch.get("discussion_id"),
                    "batch_id": batch.get("id"),
                    "attempt_count": attempt_count,
                    "max_attempts": max_attempts,
                    "error_code": normalized_error,
                    "retry_delay_seconds": delay,
                    "next_retry_at": next_retry_at,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return {"retried": True, "batch": prepared["batch"], "delay": delay}
    except Exception as exc:
        failed = StateAssessmentBatchService.mark_batch_failed(
            batch["id"],
            error_code="application_error",
            error_detail=str(exc)[:500],
            next_retry_at=next_retry_at,
        )
        return {"retried": False, "batch": failed, "reason": "retry_enqueue_failed"}


def _continue_after_terminal(batch: dict, retry: dict) -> Optional[dict]:
    terminal = dict((retry or {}).get("terminal") or {})
    terminal_batch = dict(terminal.get("batch") or {})
    if terminal_batch.get("terminal_status") not in {"degraded", "quarantined"}:
        return None
    contract = _continuation_contract(terminal_batch or batch)
    return request_state_assessment(
        group_id=batch["group_id"],
        session_id=batch["session_id"],
        discussion_id=batch["discussion_id"],
        trigger_type=contract["trigger_type"],
        trigger_sequence=contract["trigger_sequence"],
        continuation=True,
        fixed_candidate_start_sequence=contract[
            "fixed_candidate_start_sequence"
        ],
        fixed_candidate_end_sequence=contract["fixed_candidate_end_sequence"],
        replacement_of_pipeline_run_id=contract[
            "replacement_of_pipeline_run_id"
        ],
        replacement_reason=contract["replacement_reason"],
        replacement_trigger_message_id=contract[
            "replacement_trigger_message_id"
        ],
        replacement_cutoff_sequence=contract[
            "replacement_cutoff_sequence"
        ],
    )


def execute_state_assessment_batch(batch_id: int) -> dict:
    """Run the multi-segment detector on the batch's immutable window."""
    claim = StateAssessmentBatchService.claim_batch(batch_id)
    batch = claim["batch"]
    if not claim["claimed"]:
        return {"claimed": False, "batch": batch, "reason": "batch_not_claimable"}
    scope = query_one(
        """
        SELECT gsd.status, es.status AS session_status, g.state AS group_state,
               es.agent_mode, es.strategy_agent_enabled,
               es.emotion_agent_enabled,
               es.research_state_monitoring_enabled
        FROM group_session_discussions AS gsd
        JOIN experiment_sessions AS es ON es.id=gsd.session_id
        JOIN groups AS g ON g.id=gsd.group_id
        WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
        """,
        (batch["discussion_id"], batch["group_id"], batch["session_id"]),
    )
    if (
        not scope
        or scope["status"] != "running"
        or scope["session_status"] != "running"
        or str(scope["group_state"] or "").upper() == "CLOSED"
    ):
        failed = StateAssessmentBatchService.mark_batch_failed(
            batch["id"],
            error_code="discussion_not_running",
            error_detail="discussion closed before assessment execution",
        )
        return {"claimed": True, "succeeded": False, "batch": failed, "reason": "discussion_not_running"}

    pipeline_mode = pipeline_mode_from_session(dict(scope))
    if pipeline_mode is None:
        failed = StateAssessmentBatchService.mark_batch_failed(
            batch["id"],
            error_code="state_monitoring_disabled",
            error_detail="state monitoring disabled before assessment execution",
        )
        return {
            "claimed": True,
            "succeeded": False,
            "batch": failed,
            "reason": "state_monitoring_disabled",
        }
    batch = {**batch, "pipeline_mode": pipeline_mode}

    stage2_preparation = None
    stage2_initial_lease = None
    stage2_heartbeat = None
    try:
        from services.discussion_pipeline_v2.monitoring_service import MonitoringService

        stage2_preparation = Stage2PipelineService.prepare_for_batch(
            batch, pipeline_mode=pipeline_mode
        )
        if (
            pipeline_mode == "strategy"
            and stage2_preparation.get("coarse_should_escalate")
        ):
            stage2_initial_lease = RoomLeaseService.claim_strategy_pipeline(
                stage2_preparation["pipeline_run_id"]
            )
            if not stage2_initial_lease.get("acquired"):
                lease_reason = (
                    stage2_initial_lease.get("reason")
                    or "ROOM_LOCK_UNAVAILABLE"
                )
                waiting = Stage2PipelineService.mark_waiting_for_lock(
                    batch=batch,
                    pipeline_run_id=stage2_preparation["pipeline_run_id"],
                    reason=str(lease_reason),
                )
                current_batch = dict(
                    StateAssessmentBatchService.get_batch(batch["id"]) or batch
                )
                retry = _schedule_limited_retry(
                    current_batch,
                    error_code="room_lease_not_acquired",
                    error_detail=str(lease_reason),
                )
                terminal_pipeline = None
                if not retry.get("retried"):
                    terminal_pipeline = Stage2PipelineService.mark_failed(
                        batch=current_batch,
                        error_code="room_lease_not_acquired",
                        error_detail=str(lease_reason),
                        stage2_started=False,
                    )
                    retry["pipeline_recovery"] = (
                        _finalize_terminal_pipeline_siblings(batch["id"])
                    )
                return {
                    "claimed": True,
                    "succeeded": False,
                    "reason": "room_lease_not_acquired",
                    "lease_reason": lease_reason,
                    "stage2_preparation": stage2_preparation,
                    "stage2_initial_lease": {
                        key: value
                        for key, value in stage2_initial_lease.items()
                        if key != "lock_token"
                    },
                    "stage2_pipeline": terminal_pipeline or waiting,
                    "retry": retry,
                }
        stage2_started = Stage2PipelineService.mark_started(
            batch=batch,
            pipeline_run_id=stage2_preparation["pipeline_run_id"],
        )
        if not stage2_started.get("started"):
            raise RuntimeError(
                stage2_started.get("reason") or "pipeline_not_stage2_startable"
            )
        stage2_preparation["stage2_started"] = stage2_started
        if stage2_initial_lease and stage2_initial_lease.get("acquired"):
            stage2_heartbeat = RoomLeaseService.strategy_pipeline_heartbeat(
                stage2_preparation["pipeline_run_id"],
                stage2_initial_lease["lock_token"],
            ).start()
        try:
            detection = MonitoringService.run_detection(
                group_id=batch["group_id"],
                trigger_type=batch["trigger_type"],
                assessment_batch_id=batch["id"],
                fixed_candidate_start_sequence=batch["candidate_start_sequence"],
                fixed_candidate_end_sequence=batch["candidate_end_sequence"],
                allow_state_llm=True,
                # The assessment-batch transaction owns confirmed segments and
                # cursor advancement.  Valid multi-segment results are published
                # exactly once below from their persisted target segment.  The
                # legacy-only switch keeps older single-state integrations working
                # without allowing a second review for multi-segment output.
                persist_state_segment=False,
                schedule_strategy=(
                    "legacy_only" if pipeline_mode == "strategy" else False
                ),
                pipeline_mode=pipeline_mode,
            )
        finally:
            if stage2_heartbeat:
                stage2_heartbeat.stop()
        if stage2_heartbeat:
            lease_check = stage2_heartbeat.last_result
            if not lease_check or lease_check.get("renewed"):
                lease_check = stage2_heartbeat.pulse()
            if not lease_check.get("renewed"):
                raise _StrategyLeaseHeartbeatError(lease_check.get("reason"))
    except Exception as exc:
        if stage2_heartbeat:
            stage2_heartbeat.stop()
        current_batch = dict(StateAssessmentBatchService.get_batch(batch["id"]) or batch)
        lease_failure = isinstance(exc, _StrategyLeaseHeartbeatError)
        failure_code = exc.reason if lease_failure else "application_error"
        try:
            stage2_failure = Stage2PipelineService.mark_failed(
                batch=current_batch,
                error_code=failure_code,
                error_detail=str(exc)[:500],
            )
        except Exception as stage2_exc:
            logger.warning(
                "stage2 pipeline failure audit failed batch=%s: %s",
                batch["id"],
                stage2_exc,
            )
            stage2_failure = {"updated": False, "error": str(stage2_exc)}
        if lease_failure:
            failed_batch = StateAssessmentBatchService.mark_batch_failed(
                batch["id"],
                error_code=failure_code,
                error_detail=str(exc)[:500],
            )
            retry = {
                "retried": False,
                "batch": failed_batch,
                "reason": failure_code,
            }
        else:
            retry = _schedule_limited_retry(
                current_batch,
                error_code="application_error",
                error_detail=str(exc),
            )
        return {
            "claimed": True,
            "succeeded": False,
            "reason": failure_code,
            "stage2_pipeline": stage2_failure,
            "retry": retry,
            "continuation": _continue_after_terminal(batch, retry),
        }

    llm_meta = dict(detection.get("state_llm_meta") or {})
    llm_result = dict(detection.get("state_llm_result") or {})
    if detection.get("error") or llm_meta.get("analysis_failed") or not llm_meta.get("success"):
        error_code = (
            llm_meta.get("failure_type")
            or llm_meta.get("skip_reason")
            or ("monitoring_failed" if detection.get("error") else "state_llm_failed")
        )
        current_batch = dict(StateAssessmentBatchService.get_batch(batch["id"]) or batch)
        try:
            stage2_failure = Stage2PipelineService.mark_failed(
                batch=current_batch,
                error_code=str(error_code),
                error_detail=str(
                    detection.get("error")
                    or llm_meta.get("failure_message")
                    or error_code
                )[:500],
                llm_meta=llm_meta,
            )
        except Exception as stage2_exc:
            logger.warning(
                "stage2 pipeline failure audit failed batch=%s: %s",
                batch["id"],
                stage2_exc,
            )
            stage2_failure = {"updated": False, "error": str(stage2_exc)}
        retry = _schedule_limited_retry(
            current_batch,
            error_code=str(error_code),
            error_detail=str(detection.get("error") or llm_meta.get("failure_message") or error_code),
        )
        return {
            "claimed": True,
            "succeeded": False,
            "reason": error_code,
            "error_code": _normalize_batch_error_code(error_code),
            "stage2_pipeline": stage2_failure,
            "retry": retry,
            "continuation": _continue_after_terminal(batch, retry),
        }

    fresh_batch = StateAssessmentBatchService.get_batch(batch["id"]) or batch
    rerun_requested = bool(fresh_batch.get("rerun_requested"))
    segments = _segments_from_multi_result(batch, llm_result)
    try:
        saved = StateAssessmentBatchService.save_successful_segments(
            batch["id"],
            segments,
            raw_response=llm_meta.get("raw_response"),
            parsed_response=llm_result,
            model=llm_meta.get("model_name"),
            prompt_version=llm_meta.get("prompt_version"),
        )
    except Exception as exc:
        current_batch = dict(StateAssessmentBatchService.get_batch(batch["id"]) or batch)
        try:
            stage2_failure = Stage2PipelineService.mark_failed(
                batch=current_batch,
                error_code="application_error",
                error_detail=str(exc)[:500],
                llm_meta=llm_meta,
            )
        except Exception as stage2_exc:
            logger.warning(
                "stage2 pipeline failure audit failed batch=%s: %s",
                batch["id"],
                stage2_exc,
            )
            stage2_failure = {"updated": False, "error": str(stage2_exc)}
        retry = _schedule_limited_retry(
            current_batch,
            error_code="application_error",
            error_detail=str(exc),
        )
        return {
            "claimed": True,
            "succeeded": False,
            "reason": "application_error",
            "stage2_pipeline": stage2_failure,
            "retry": retry,
            "continuation": _continue_after_terminal(batch, retry),
        }
    intervention_result = None
    stage2_pipeline_result = None
    if is_stage2_result(llm_result) and pipeline_mode == "state_only":
        stage2_pipeline_result = Stage2PipelineService.persist_state_only_success(
            batch={**saved["batch"], "pipeline_mode": pipeline_mode},
            stage2_result=llm_result,
            llm_meta=llm_meta,
            saved_segments=saved["segments"],
            monitor_run_id=detection.get("monitor_run_id"),
        )
        intervention_result = {
            "reason": "state_only_completed",
            "published": False,
            "skipped": True,
            "assessment_batch_id": batch["id"],
            "strategy_pipeline_run_id": None,
            "stage3_pipeline": None,
        }
    elif is_stage2_result(llm_result):
        stage2_pipeline_result = Stage2PipelineService.persist_success(
            batch={**saved["batch"], "pipeline_mode": pipeline_mode},
            stage2_result=llm_result,
            llm_meta=llm_meta,
            saved_segments=saved["segments"],
            monitor_run_id=detection.get("monitor_run_id"),
            suppress_intervention=(
                saved["batch"].get("fallback_action") == "state_only_replay"
            ),
        )
        if stage2_pipeline_result.get("stale"):
            stage2_pipeline_result["replacement_assessment"] = (
                request_state_assessment(
                    group_id=saved["batch"]["group_id"],
                    session_id=saved["batch"]["session_id"],
                    discussion_id=saved["batch"]["discussion_id"],
                    trigger_type="message_count_periodic",
                    trigger_sequence=stage2_pipeline_result.get(
                        "latest_student_sequence"
                    ),
                    continuation=True,
                    replacement_of_pipeline_run_id=stage2_pipeline_result.get(
                        "pipeline_run_id"
                    ),
                    replacement_reason="STALE_NEW_STUDENT_MESSAGE",
                    replacement_trigger_message_id=stage2_pipeline_result.get(
                        "latest_student_message_id"
                    ),
                    replacement_cutoff_sequence=stage2_pipeline_result.get(
                        "latest_student_sequence"
                    ),
                )
            )
        observation_result = record_observation_assessment(
            observation_pipeline_run_id=stage2_pipeline_result.get("pipeline_run_id"),
            batch=saved["batch"],
            stage2_result=llm_result,
        )
        stage2_pipeline_result["post_intervention_observation"] = observation_result
        stage3_pipeline_result = None
        if stage2_pipeline_result.get("should_enter_stage3"):
            if is_stage3_enabled():
                pipeline_id = stage2_pipeline_result["pipeline_run_id"]
                preflight = InterventionDecisionGate.evaluate_preflight(pipeline_id)
                stage2_pipeline_result["preflight_gate"] = preflight
                if not preflight.get("allowed"):
                    stage3_pipeline_result = (
                        ThreeStageInterventionPublisher.finish_preflight(
                            pipeline_id,
                            preflight,
                        )
                    )
                else:
                    ThreeStageInterventionPublisher.record_preflight(
                        pipeline_id,
                        preflight,
                    )
                    stage3_lease = RoomLeaseService.claim_strategy_pipeline(
                        pipeline_id
                    )
                    stage2_pipeline_result["room_lease"] = {
                        key: value
                        for key, value in stage3_lease.items()
                        if key != "lock_token"
                    }
                    if stage3_lease.get("acquired"):
                        stage3_pipeline_result = Stage3PipelineService.execute_for_pipeline(
                            pipeline_id
                        )
                        if stage3_pipeline_result:
                            stage3_pipeline_result["preflight_gate"] = preflight
                        if (
                            stage3_pipeline_result
                            and stage3_pipeline_result.get("stage3_status") == "SUCCEEDED"
                        ):
                            stage3_pipeline_result["decision_gate"] = (
                                ThreeStageInterventionPublisher.publish_ready_pipeline(
                                    pipeline_id
                                )
                            )
                    else:
                        lease_reason = (
                            stage3_lease.get("reason")
                            or "ROOM_LOCK_UNAVAILABLE"
                        )
                        stage3_pipeline_result = Stage3PipelineService.mark_failed(
                            pipeline_id,
                            str(lease_reason),
                            "Stage 3 did not start because the authoritative "
                            "pipeline could not own the room lease.",
                        )
                        stage3_pipeline_result["preflight_gate"] = preflight
        intervention_result = {
            "reason": (
                "three_stage_decision_gate_published"
                if (
                    stage3_pipeline_result
                    and (stage3_pipeline_result.get("decision_gate") or {}).get("published")
                )
                else "stage3_decision_gate_skipped"
                if stage3_pipeline_result and stage3_pipeline_result.get("stage3_status") == "SUCCEEDED"
                else stage3_pipeline_result.get("failure_code")
                if stage3_pipeline_result
                else "stage2_deferred_to_stage3"
                if stage2_pipeline_result.get("should_enter_stage3")
                else stage2_pipeline_result.get("skip_reason")
                or "stage2_no_intervention"
            ),
            "published": bool(
                stage3_pipeline_result
                and (stage3_pipeline_result.get("decision_gate") or {}).get("published")
            ),
            "skipped": not bool(
                stage3_pipeline_result
                and (stage3_pipeline_result.get("decision_gate") or {}).get("published")
            ),
            "assessment_batch_id": batch["id"],
            "strategy_pipeline_run_id": stage2_pipeline_result.get("pipeline_run_id"),
            "stage3_pipeline": stage3_pipeline_result,
        }
    else:
        from config import LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED

        if LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
            from services.assessment_batch_intervention_service import (
                AssessmentBatchInterventionService,
            )

            intervention_result = AssessmentBatchInterventionService.execute(
                batch["id"],
                monitor_run_id=detection.get("monitor_run_id"),
            )
        else:
            intervention_result = {
                "published": False,
                "skipped": True,
                "reason": "legacy_state_batch_direct_publish_disabled",
                "assessment_batch_id": batch["id"],
            }
    continuation_result = None
    remaining = query_one(
        """
        SELECT COUNT(*) AS count
        FROM messages AS m
        JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
          AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
          AND m.sequence>?
        """,
        (
            batch["group_id"],
            batch["session_id"],
            batch["discussion_id"],
            batch["candidate_end_sequence"],
        ),
    )
    if rerun_requested or int(remaining["count"] if remaining else 0) > 0:
        contract = _continuation_contract(fresh_batch)
        continuation_result = request_state_assessment(
            group_id=batch["group_id"],
            session_id=batch["session_id"],
            discussion_id=batch["discussion_id"],
            trigger_type=contract["trigger_type"],
            trigger_sequence=contract["trigger_sequence"],
            continuation=True,
            fixed_candidate_start_sequence=contract[
                "fixed_candidate_start_sequence"
            ],
            fixed_candidate_end_sequence=contract[
                "fixed_candidate_end_sequence"
            ],
            replacement_of_pipeline_run_id=contract[
                "replacement_of_pipeline_run_id"
            ],
            replacement_reason=contract["replacement_reason"],
            replacement_trigger_message_id=contract[
                "replacement_trigger_message_id"
            ],
            replacement_cutoff_sequence=contract[
                "replacement_cutoff_sequence"
            ],
        )
    return {
        "claimed": True,
        "succeeded": True,
        "batch": saved["batch"],
        "segments": saved["segments"],
        "monitor_run_id": detection.get("monitor_run_id"),
        "stage2_preparation": stage2_preparation,
        "stage2_initial_lease": {
            key: value
            for key, value in (stage2_initial_lease or {}).items()
            if key != "lock_token"
        }
        if stage2_initial_lease
        else None,
        "stage2_pipeline": stage2_pipeline_result,
        "intervention": intervention_result,
        "continuation": continuation_result,
    }


def scan_due_state_assessments(*, limit: int = 100) -> dict:
    """Single periodic scanner; it never calls the detector directly."""
    rows = query_all(
        """
        SELECT gsd.id AS discussion_id, gsd.group_id, gsd.session_id
        FROM group_session_discussions AS gsd
        JOIN experiment_sessions AS es ON es.id=gsd.session_id
        JOIN groups AS g ON g.id=gsd.group_id
        WHERE gsd.status='running' AND es.status='running'
          AND UPPER(COALESCE(g.state, ''))<>'CLOSED'
        ORDER BY gsd.id ASC
        LIMIT ?
        """,
        (max(1, int(limit or 100)),),
    )
    summary = {"scanned": len(rows), "requested": 0, "enqueued": 0, "skipped": 0, "results": []}
    for row in rows:
        first = request_state_assessment(
            group_id=row["group_id"],
            session_id=row["session_id"],
            discussion_id=row["discussion_id"],
            trigger_type="message_count_periodic",
        )
        result = first
        if first.get("reason") == "message_threshold_not_reached":
            result = request_state_assessment(
                group_id=row["group_id"],
                session_id=row["session_id"],
                discussion_id=row["discussion_id"],
                trigger_type="time_periodic",
            )
        summary["results"].append(result)
        if result.get("enqueued"):
            summary["enqueued"] += 1
        if result.get("assessment_batch_id"):
            summary["requested"] += 1
        if result.get("skipped"):
            summary["skipped"] += 1
    return summary


__all__ = [
    "TRIGGER_PRIORITIES",
    "execute_state_assessment_batch",
    "request_state_assessment",
    "request_state_assessment_for_message",
    "resolve_message_scope",
    "scan_due_state_assessments",
]
