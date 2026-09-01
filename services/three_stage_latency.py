# -*- coding: utf-8 -*-
"""Structured, best-effort latency telemetry for the three-stage pipeline.

The telemetry path must never change a pipeline decision. Events are written
to an additive audit table when available and are always emitted as structured
logs. Lock tokens are represented only by a SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from db import db


logger = logging.getLogger(__name__)

REQUIRED_LOG_FIELDS = (
    "group_id",
    "session_id",
    "discussion_id",
    "task_id",
    "pipeline_run_id",
    "assessment_batch_id",
    "cutoff_sequence",
    "lock_owner",
    "lock_token_hash",
    "call_id",
    "attempt",
    "stage",
    "event",
    "elapsed_ms",
)

STAGE3_FAILURE_CATEGORIES = frozenset(
    {
        "transport_timeout",
        "transport_error",
        "response_truncated",
        "json_unparseable",
        "missing_selected_strategy_id",
        "strategy_not_allowed",
        "missing_intervention_text",
        "repair_failed",
    }
)

STAGE2_FAILURE_CATEGORIES = frozenset(
    {
        "transport_timeout",
        "transport_error",
        "response_truncated",
        "json_unparseable",
        "missing_primary_state",
        "missing_should_intervene",
        "invalid_evidence",
        "repair_failed",
    }
)

PUBLISH_RUNTIME_GATE_CATEGORIES = frozenset(
    {
        "stale_new_student_message",
        "invalid_room_lease",
        "session_closed",
        "discussion_closed",
        "agent_disabled",
        "already_published",
        "cooldown_active",
        "help_already_covered",
        "runtime_gate_blocked",
    }
)


def latency_timestamp() -> str:
    """Return a local database-compatible timestamp with millisecond precision."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def latency_timer() -> int:
    return time.perf_counter_ns()


def elapsed_ms(started_ns: int) -> float:
    return round(max(0, time.perf_counter_ns() - int(started_ns)) / 1_000_000, 3)


def duration_ms(started_at: Any, finished_at: Any) -> float:
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        return round(max(0.0, (finished - started).total_seconds() * 1000), 3)
    except (TypeError, ValueError):
        return 0.0


def lock_token_hash(lock_token: Any) -> Optional[str]:
    if not lock_token:
        return None
    return hashlib.sha256(str(lock_token).encode("utf-8")).hexdigest()


def record_latency_event(
    *,
    stage: str,
    event: str,
    pipeline_run_id: int = None,
    assessment_batch_id: int = None,
    group_id: int = None,
    session_id: int = None,
    discussion_id: int = None,
    task_id: int = None,
    cutoff_sequence: int = None,
    lock_owner: int = None,
    lock_token: str = None,
    call_id: str = None,
    attempt: int = None,
    occurred_at: str = None,
    elapsed: float = None,
    details: dict = None,
    conn=None,
    pipeline_context: bool = False,
) -> dict:
    """Persist and log one event without allowing telemetry to break work."""
    owns_connection = conn is None
    event_conn = conn or db()
    occurred_at = occurred_at or latency_timestamp()
    try:
        scope = _resolve_scope(
            event_conn,
            pipeline_run_id=pipeline_run_id,
            assessment_batch_id=assessment_batch_id,
        )
        payload = {
            "group_id": _first(group_id, scope.get("group_id")),
            "session_id": _first(session_id, scope.get("session_id")),
            "discussion_id": _first(discussion_id, scope.get("discussion_id")),
            "task_id": _first(task_id, scope.get("task_id")),
            "pipeline_run_id": _first(pipeline_run_id, scope.get("pipeline_run_id")),
            "assessment_batch_id": _first(
                assessment_batch_id, scope.get("assessment_batch_id")
            ),
            "cutoff_sequence": _first(
                cutoff_sequence, scope.get("cutoff_sequence")
            ),
            "lock_owner": _first(lock_owner, scope.get("lock_owner")),
            "lock_token_hash": lock_token_hash(lock_token)
            or scope.get("lock_token_hash"),
            "call_id": str(call_id) if call_id is not None else None,
            "attempt": int(attempt) if attempt is not None else None,
            "stage": str(stage or "unknown"),
            "event": str(event or "unknown"),
            "elapsed_ms": round(float(elapsed or 0.0), 3),
        }
        safe_details = _safe_details(details or {})
        if pipeline_context:
            safe_details = {
                **_pipeline_trace_details(scope),
                **safe_details,
            }
        log_payload = {key: payload.get(key) for key in REQUIRED_LOG_FIELDS}
        log_payload["occurred_at"] = occurred_at
        if safe_details:
            log_payload["details"] = safe_details
        logger.info(
            "[three_stage_latency] %s",
            json.dumps(
                log_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
        event_conn.execute(
            """
            INSERT INTO strategy_pipeline_latency_events(
                group_id, session_id, discussion_id, task_id,
                pipeline_run_id, assessment_batch_id, cutoff_sequence,
                lock_owner, lock_token_hash, call_id, attempt,
                stage, event, occurred_at, elapsed_ms, details_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload["group_id"],
                payload["session_id"],
                payload["discussion_id"],
                payload["task_id"],
                payload["pipeline_run_id"],
                payload["assessment_batch_id"],
                payload["cutoff_sequence"],
                payload["lock_owner"],
                payload["lock_token_hash"],
                payload["call_id"],
                payload["attempt"],
                payload["stage"],
                payload["event"],
                occurred_at,
                payload["elapsed_ms"],
                json.dumps(
                    safe_details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
                occurred_at,
            ),
        )
        if owns_connection:
            event_conn.commit()
        return {"recorded": True, **payload, "occurred_at": occurred_at}
    except Exception as exc:
        if owns_connection:
            try:
                event_conn.rollback()
            except Exception:
                pass
        logger.warning(
            "three-stage latency event persistence failed stage=%s event=%s: %s",
            stage,
            event,
            exc,
        )
        return {"recorded": False, "stage": stage, "event": event, "error": str(exc)}
    finally:
        if owns_connection:
            event_conn.close()


def record_pipeline_summary(
    pipeline_run_id: int,
    *,
    event: str = "pipeline_completed",
    publish_gate_allowed: bool = None,
    publish_gate_reason: str = None,
    stage3_failure_category: str = None,
    preflight_gate_result: str = None,
    preflight_gate_reason: str = None,
    stage3_skipped_by_preflight: bool = False,
    lock_skipped_by_preflight: bool = False,
    llm_call_saved: bool = False,
    occurred_at: str = None,
    conn=None,
) -> dict:
    """Write one compact end-of-chain snapshot for a pipeline run.

    The snapshot is additive audit data.  It deliberately contains IDs and
    operational categories only; message text and lock tokens never enter the
    event payload.
    """

    owns_connection = conn is None
    summary_conn = conn or db()
    try:
        scope = _resolve_scope(summary_conn, pipeline_run_id=int(pipeline_run_id))
        row = summary_conn.execute(
            """
            SELECT stage3_status, publish_status, failure_code,
                   published_message_id, room_lock_acquired_at,
                   room_lock_released_at
            FROM strategy_pipeline_runs
            WHERE id=?
            """,
            (int(pipeline_run_id),),
        ).fetchone()
        attempt_row = summary_conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM strategy_pipeline_latency_events
            WHERE pipeline_run_id=?
              AND stage='stage3'
              AND (
                  event LIKE 'stage3_llm_attempt_%_finished'
                  OR event='stage3_repair_finished'
              )
            """,
            (int(pipeline_run_id),),
        ).fetchone()
        attempt_count = int(attempt_row["count"] if attempt_row else 0)
        stage3_status = str(row["stage3_status"] or "").upper() if row else ""
        publish_status = str(row["publish_status"] or "").upper() if row else ""
        if publish_gate_allowed is None:
            publish_gate_allowed = publish_status == "PUBLISHED"
        gate_category = normalize_publish_runtime_reason(publish_gate_reason)
        if gate_category is None and publish_status == "PUBLISHED":
            gate_category = "allowed"
        acquired_at = row["room_lock_acquired_at"] if row else None
        released_at = row["room_lock_released_at"] if row else None
        details = {
            "stage3_attempt_count": attempt_count,
            "stage3_success": stage3_status == "SUCCEEDED",
            "stage3_failure_category": (
                stage3_failure_category
                or normalize_stage3_failure(row["failure_code"])
                if stage3_status == "FAILED"
                else None
            ),
            "publish_gate_allowed": bool(publish_gate_allowed),
            "publish_gate_result": (
                "not_reached"
                if preflight_gate_result == "blocked"
                else
                "published"
                if publish_status == "PUBLISHED"
                else "blocked"
                if publish_status in {"FAILED", "SKIPPED"}
                else "not_reached"
            ),
            "publish_gate_reason": gate_category,
            "preflight_gate_result": preflight_gate_result,
            "preflight_gate_reason": preflight_gate_reason,
            "stage3_skipped_by_preflight": bool(stage3_skipped_by_preflight),
            "lock_skipped_by_preflight": bool(lock_skipped_by_preflight),
            "llm_call_saved": bool(llm_call_saved),
            "published_message_id": (
                int(row["published_message_id"])
                if row and row["published_message_id"] is not None
                else None
            ),
            "lease_acquired": bool(acquired_at),
            "lease_released": bool(released_at),
            "lease_hold_duration_ms": (
                duration_ms(acquired_at, released_at)
                if acquired_at and released_at
                else None
            ),
        }
        result = record_latency_event(
            stage="pipeline",
            event=event,
            pipeline_run_id=int(pipeline_run_id),
            occurred_at=occurred_at,
            details=details,
            conn=summary_conn,
            pipeline_context=True,
        )
        if owns_connection:
            summary_conn.commit()
        return result
    except Exception as exc:
        if owns_connection:
            try:
                summary_conn.rollback()
            except Exception:
                pass
        logger.warning(
            "three-stage pipeline summary persistence failed pipeline=%s: %s",
            pipeline_run_id,
            exc,
        )
        return {
            "recorded": False,
            "pipeline_run_id": int(pipeline_run_id),
            "event": event,
            "error": str(exc),
        }
    finally:
        if owns_connection:
            summary_conn.close()


def normalize_stage3_failure(
    reason: Any = None,
    *,
    finish_reason: Any = None,
    attempt_type: str = None,
    exception: bool = False,
) -> str:
    """Map internal Stage3 errors to the small operational category set."""

    raw = str(reason or "").strip().lower()
    finish = str(finish_reason or "").strip().lower()
    if finish == "length" or "truncated" in raw or "finish_reason_length" in raw:
        return "response_truncated"
    if "timeout" in raw or "timed out" in raw:
        return "transport_timeout"
    if exception or any(
        token in raw
        for token in (
            "network",
            "transport",
            "http_error",
            "worker_exception",
            "gateway_exception",
            "connection",
        )
    ):
        return "transport_error"
    if raw in {"invalid_json", "json_unparseable", "invalid_response", "stage3_no_output"}:
        category = "json_unparseable"
    elif "missing_selected_strategy" in raw:
        category = "missing_selected_strategy_id"
    elif raw in {
        "strategy_not_candidate",
        "strategy_not_allowed",
        "missing_candidate_strategy_ids",
    } or "candidate" in raw:
        category = "strategy_not_allowed"
    elif raw in {"missing_intervention_text", "empty_text", "missing_text"}:
        category = "missing_intervention_text"
    else:
        category = "json_unparseable"
    if attempt_type == "repair":
        return "repair_failed"
    return category


def normalize_stage2_failure(
    reason: Any = None,
    *,
    finish_reason: Any = None,
    attempt_type: str = None,
    error_message: Any = None,
    response_incomplete: bool = False,
    exception: bool = False,
) -> str:
    """Map Stage 2 transport/parser/schema failures to bounded categories."""

    raw = str(reason or "").strip().lower()
    detail = str(error_message or "").strip().lower()
    combined = f"{raw} {detail}"
    finish = str(finish_reason or "").strip().lower()

    if "reasoning_budget_exhausted" in combined:
        return "reasoning_budget_exhausted"
    if response_incomplete or finish == "length" or "truncated" in combined:
        return "response_truncated"
    if "timeout" in combined or "timed out" in combined:
        return "transport_timeout"
    if exception or any(
        token in combined
        for token in (
            "network",
            "transport",
            "http_error",
            "gateway_exception",
            "connection",
            "authentication_error",
            "rate_limited",
            "upstream_5xx",
            "llm_transport_error",
            "application_error",
        )
    ):
        return "transport_error"
    if "sub_category_canonical_mismatch" in combined:
        category = "sub_category_canonical_mismatch"
    elif "secondary_tag_primary_mismatch" in combined:
        category = "sub_category_canonical_mismatch"
    elif "overlay_requires_primary_state" in combined:
        category = "sub_category_canonical_mismatch"
    elif "missing_should_intervene" in combined or (
        "missing_top_level_field" in combined and "should_intervene" in combined
    ):
        category = "missing_should_intervene"
    elif (
        "missing_primary_state" in combined
        or "missing_sub_category" in combined
        or "missing_canonical_state" in combined
        or (
            "missing_top_level_field" in combined
            and any(field in combined for field in ("sub_category", "canonical_state"))
        )
    ):
        category = "missing_primary_state"
    elif "evidence" in combined:
        category = "invalid_evidence"
    elif raw in {
        "invalid_json",
        "json_parse_error",
        "json_unparseable",
        "invalid_response",
        "empty_content",
        "empty_response",
        "no_output",
        "not_a_dict",
    } or "json" in combined:
        category = "json_unparseable"
    else:
        category = "json_unparseable"

    if attempt_type == "repair" and category != "sub_category_canonical_mismatch":
        return "repair_failed"
    return category


def normalize_publish_runtime_reason(reason: Any) -> Optional[str]:
    """Map publish gate reasons to runtime-only observability categories."""

    raw = str(reason or "").strip()
    if not raw or raw.lower() in {"allowed", "none"}:
        return None
    text = raw.lower()
    if "already_published" in text or text == "published":
        return "already_published"
    if "stale" in text or "new_student" in text or "student_sequence" in text:
        return "stale_new_student_message"
    if any(token in text for token in ("lease", "lock", "room_lease", "room_locked")):
        return "invalid_room_lease"
    if "session" in text and any(
        token in text
        for token in ("closed", "ended", "not_running", "not_active", "disabled")
    ):
        return "session_closed"
    if "discussion" in text and any(token in text for token in ("closed", "ended", "not_running")):
        return "discussion_closed"
    if (
        ("agent" in text or "intervention" in text or text == "auto_intervention_disabled")
        and "disabled" in text
    ):
        return "agent_disabled"
    if "help" in text:
        return "help_already_covered"
    if any(
        token in text
        for token in (
            "cooldown",
            "spacing",
            "emotion_slot",
            "no_student_feedback",
            "intervention_cap",
            "overlapping_evidence",
        )
    ):
        return "cooldown_active"
    return "runtime_gate_blocked"


def _resolve_scope(conn, *, pipeline_run_id: int = None, assessment_batch_id: int = None) -> dict:
    scope: dict[str, Any] = {}
    pipeline = None
    batch = None
    if pipeline_run_id is not None:
        pipeline = conn.execute(
            """
            SELECT id, group_id, session_id, discussion_id, task_id,
                   trigger_message_id, input_cutoff_student_sequence,
                   canonical_sub_state_code, strategy_candidate_ids_json,
                   selected_strategy_id, room_lock_token, assessment_batch_id
            FROM strategy_pipeline_runs WHERE id=?
            """,
            (int(pipeline_run_id),),
        ).fetchone()
    if assessment_batch_id is not None:
        batch = conn.execute(
            """
            SELECT id, group_id, session_id, discussion_id, task_id,
                   candidate_end_sequence, trigger_sequence,
                   last_trigger_sequence
            FROM state_assessment_batches WHERE id=?
            """,
            (int(assessment_batch_id),),
        ).fetchone()
    if pipeline and batch is None and pipeline["assessment_batch_id"] is not None:
        batch = conn.execute(
            """
            SELECT id, group_id, session_id, discussion_id, task_id,
                   candidate_end_sequence, trigger_sequence,
                   last_trigger_sequence
            FROM state_assessment_batches WHERE id=?
            """,
            (int(pipeline["assessment_batch_id"]),),
        ).fetchone()
    if not pipeline and batch:
        pipeline = conn.execute(
            """
            SELECT id, group_id, session_id, discussion_id, task_id,
                   trigger_message_id, input_cutoff_student_sequence,
                   canonical_sub_state_code, strategy_candidate_ids_json,
                   selected_strategy_id, room_lock_token, assessment_batch_id
            FROM strategy_pipeline_runs
            WHERE group_id=?
              AND COALESCE(session_id, 0)=COALESCE(?, 0)
              AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
              AND input_cutoff_student_sequence=?
            ORDER BY CASE WHEN stage2_status IN ('RUNNING','SUCCEEDED','FAILED') THEN 0 ELSE 1 END,
                     id DESC
            LIMIT 1
            """,
            (
                batch["group_id"],
                batch["session_id"],
                batch["discussion_id"],
                batch["candidate_end_sequence"],
            ),
        ).fetchone()
    if pipeline:
        trigger_message_id = pipeline["trigger_message_id"]
        trigger_sequence = None
        if batch:
            trigger_sequence = batch["last_trigger_sequence"] or batch["trigger_sequence"]
        if trigger_message_id is None and (
            trigger_sequence is not None
            or pipeline["input_cutoff_student_sequence"] is not None
        ):
            try:
                trigger = conn.execute(
                    """
                    SELECT id
                    FROM messages
                    WHERE group_id=?
                      AND COALESCE(session_id, 0)=COALESCE(?, 0)
                      AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
                      AND sequence=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        pipeline["group_id"],
                        pipeline["session_id"],
                        pipeline["discussion_id"],
                        trigger_sequence
                        if trigger_sequence is not None
                        else pipeline["input_cutoff_student_sequence"],
                    ),
                ).fetchone()
                trigger_message_id = trigger["id"] if trigger else None
            except Exception:
                trigger_message_id = None
        scope.update(
            {
                "pipeline_run_id": int(pipeline["id"]),
                "group_id": pipeline["group_id"],
                "session_id": pipeline["session_id"],
                "discussion_id": pipeline["discussion_id"],
                "task_id": pipeline["task_id"],
                "trigger_message_id": trigger_message_id,
                "cutoff_sequence": pipeline["input_cutoff_student_sequence"],
                "canonical_substate": pipeline["canonical_sub_state_code"],
                "allowed_strategy_ids": _json_string_list(
                    pipeline["strategy_candidate_ids_json"]
                ),
                "selected_strategy_id": pipeline["selected_strategy_id"],
                "lock_owner": -int(pipeline["id"]),
                "lock_token_hash": lock_token_hash(pipeline["room_lock_token"]),
                "assessment_batch_id": pipeline["assessment_batch_id"],
            }
        )
    if batch:
        for key, value in {
            "assessment_batch_id": int(batch["id"]),
            "group_id": batch["group_id"],
            "session_id": batch["session_id"],
            "discussion_id": batch["discussion_id"],
            "task_id": batch["task_id"],
            "cutoff_sequence": batch["candidate_end_sequence"],
        }.items():
            if scope.get(key) is None:
                scope[key] = value
    return scope


def _safe_details(details: dict) -> dict:
    """Allow only operational metadata; message text and credentials stay out."""
    allowed = {
        "success",
        "reason",
        "failure_type",
        "prompt_version",
        "model",
        "profile",
        "timeout_seconds",
        "gateway_retries",
        "prompt_chars",
        "prompt_estimated_tokens",
        "prompt_character_count",
        "attempt_type",
        "gateway_max_attempts",
        "gateway_attempt_count",
        "gateway_retry_count",
        "external_call_budget",
        "max_tokens",
        "temperature",
        "timeout",
        "finish_reason",
        "response_chars",
        "response_character_count",
        "error",
        "parser_result",
        "response_starts_with_brace",
        "response_ends_with_brace",
        "open_brace_present",
        "close_brace_present",
        "json_extractable",
        "core_json_extractable",
        "incomplete_response",
        "response_incomplete",
        "local_parse_success",
        "entered_repair",
        "selected_strategy_id",
        "stage3_total_elapsed_ms",
        "validation_result",
        "transferred_from_pipeline_id",
        "renewed",
        "terminal_status",
        "lock_ttl_seconds",
        "lock_heartbeat_seconds",
        "lock_max_total_seconds",
        "strategy_cooldown_seconds",
        "emotion_strategy_spacing_seconds",
        "pipeline_run_id",
        "group_id",
        "session_id",
        "discussion_id",
        "task_id",
        "trigger_message_id",
        "cutoff_student_sequence",
        "canonical_substate",
        "allowed_strategy_ids",
        "selected_strategy_id",
        "stage3_attempt_count",
        "stage3_success",
        "stage3_failure_category",
        "stage2_attempt_count",
        "stage2_external_call_count",
        "stage2_repair_attempt_count",
        "stage2_failure_category",
        "structural_failure_reason",
        "failure_category",
        "preflight_gate_result",
        "preflight_gate_reason",
        "preflight_terminal_reason",
        "stage3_skipped_by_preflight",
        "lock_skipped_by_preflight",
        "llm_call_saved",
        "stage3_status",
        "publish_status",
        "final_status",
        "publish_gate_allowed",
        "publish_gate_result",
        "publish_gate_reason",
        "published_message_id",
        "lease_acquired",
        "lease_released",
        "lease_hold_duration_ms",
        "lease_action",
    }
    return {key: value for key, value in dict(details or {}).items() if key in allowed}


def _pipeline_trace_details(scope: dict) -> dict:
    return {
        "pipeline_run_id": scope.get("pipeline_run_id"),
        "group_id": scope.get("group_id"),
        "session_id": scope.get("session_id"),
        "discussion_id": scope.get("discussion_id"),
        "task_id": scope.get("task_id"),
        "trigger_message_id": scope.get("trigger_message_id"),
        "cutoff_student_sequence": scope.get("cutoff_sequence"),
        "canonical_substate": scope.get("canonical_substate"),
        "allowed_strategy_ids": list(scope.get("allowed_strategy_ids") or []),
        "selected_strategy_id": scope.get("selected_strategy_id"),
    }


def _json_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:32]:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:80])
    return result


def _first(primary, fallback):
    return fallback if primary is None else primary


__all__ = [
    "PUBLISH_RUNTIME_GATE_CATEGORIES",
    "REQUIRED_LOG_FIELDS",
    "STAGE3_FAILURE_CATEGORIES",
    "STAGE2_FAILURE_CATEGORIES",
    "duration_ms",
    "elapsed_ms",
    "latency_timer",
    "latency_timestamp",
    "lock_token_hash",
    "record_latency_event",
    "record_pipeline_summary",
    "normalize_publish_runtime_reason",
    "normalize_stage3_failure",
    "normalize_stage2_failure",
]
