# -*- coding: utf-8 -*-
"""Publish strategy interventions owned by a successful multi-segment batch.

The state detector already made the intervention decision and produced the
student-visible sentence.  This service validates that persisted decision,
claims exactly one run for the target risk segment, and delegates the atomic
message/audit write to the shared publisher.  It never calls another LLM.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

from config import HUEY_IMMEDIATE
from db import db, now_str, query_one
from services.discussion_pipeline_v2.llm_state_detector import RISK_SEGMENT_STATE_CODES
from services.help_request_coverage_service import HelpRequestCoverageService

logger = logging.getLogger(__name__)

ACTIVE_RUN_STATUSES = {
    "PENDING",
    "RUNNING",
    "REVALIDATING",
    "LOCKED",
    "GENERATING",
    "VALIDATING",
}
SUCCESS_RUN_STATUSES = {"PUBLISHED", "FALLBACK"}


def _json_value(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sequence_set(value: Any) -> set[int]:
    result: set[int] = set()
    for item in _json_value(value, []) or []:
        if isinstance(item, bool):
            continue
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _highly_overlapping(left: set[int], right: set[int]) -> bool:
    if not left or not right:
        return False
    return len(left & right) / min(len(left), len(right)) >= 0.5


def _log_event(event: str, **fields: Any) -> None:
    logger.info(
        "[assessment_batch_intervention] %s %s",
        event,
        json.dumps(
            {key: value for key, value in fields.items() if value is not None},
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ),
    )


def _load_candidate(batch_id: int) -> dict:
    row = query_one(
        """
        SELECT b.*, es.task_id, es.session_no, es.status AS session_status,
               gsd.status AS discussion_status, g.state AS group_state
        FROM state_assessment_batches AS b
        JOIN experiment_sessions AS es ON es.id=b.session_id
        JOIN group_session_discussions AS gsd
          ON gsd.id=b.discussion_id
         AND gsd.group_id=b.group_id
         AND gsd.session_id=b.session_id
        JOIN groups AS g ON g.id=b.group_id
        WHERE b.id=?
        """,
        (batch_id,),
    )
    if not row:
        return {"valid": False, "reason": "assessment_batch_not_found"}
    batch = dict(row)
    if batch.get("status") != "succeeded":
        return {"valid": False, "reason": "assessment_batch_not_succeeded", "batch": batch}

    parsed = _json_value(batch.get("parsed_response"), {}) or {}
    intervention = parsed.get("intervention")
    if not isinstance(intervention, dict) or not intervention.get("needed"):
        return {
            "valid": False,
            "reason": "intervention_not_needed",
            "batch": batch,
            "parsed": parsed,
        }
    target_segment_id = intervention.get("target_segment_id")
    target_index = intervention.get("target_segment_index")
    active_index = parsed.get("active_segment_index")
    if isinstance(target_segment_id, bool) or target_segment_id is None:
        return {"valid": False, "reason": "target_segment_missing", "batch": batch}
    segment_row = query_one(
        """
        SELECT * FROM collaboration_state_segments
        WHERE id=? AND assessment_batch_id=?
          AND group_id=? AND session_id=?
        """,
        (target_segment_id, batch["id"], batch["group_id"], batch["session_id"]),
    )
    if not segment_row:
        return {"valid": False, "reason": "target_segment_not_found", "batch": batch}
    segment = dict(segment_row)
    if (
        segment.get("source") != "llm"
        or segment.get("assessment_status") != "confirmed"
        or segment.get("state_code") not in RISK_SEGMENT_STATE_CODES
    ):
        return {"valid": False, "reason": "target_segment_not_confirmed_risk", "batch": batch}
    explicit_help = str(intervention.get("reason_code") or "") == "explicit_help_request"
    if not explicit_help and (
        target_index != active_index or not bool(segment.get("is_active_at_batch_end"))
    ):
        return {"valid": False, "reason": "target_segment_not_active", "batch": batch}
    message = str(intervention.get("message") or "").strip()
    if not message:
        return {"valid": False, "reason": "intervention_message_missing", "batch": batch}
    return {
        "valid": True,
        "batch": batch,
        "parsed": parsed,
        "intervention": intervention,
        "segment": segment,
        "message": message,
        "active_segment_index": active_index,
    }


def _help_guard(conn, candidate: dict) -> dict:
    batch = candidate["batch"]
    segment = candidate["segment"]
    guard = HelpRequestCoverageService.evaluate(
        batch["group_id"],
        batch.get("session_id"),
        segment.get("state_code"),
        segment.get("id"),
        segment.get("start_sequence"),
        segment.get("end_sequence"),
        connection=conn,
    )
    return {
        **guard,
        "allowed": not guard["blocked"],
        "reason": guard.get("reason_code"),
    }


def _duplicate_guard(conn, candidate: dict) -> dict:
    batch = candidate["batch"]
    segment = candidate["segment"]
    evidence = _sequence_set(segment.get("evidence_sequences"))
    rows = conn.execute(
        """
        SELECT * FROM intervention_runs
        WHERE group_id=? AND session_id=?
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(agent_type, 'strategy')='strategy'
          AND status IN (
              'PENDING','RUNNING','REVALIDATING','LOCKED','GENERATING','VALIDATING',
              'PUBLISHED','FALLBACK'
          )
        ORDER BY id DESC
        """,
        (batch["group_id"], batch["session_id"], batch["discussion_id"]),
    ).fetchall()
    for row in rows:
        if row["target_segment_id"] == segment["id"]:
            return {
                "allowed": False,
                "reason": "target_segment_already_claimed",
                "existing_run": dict(row),
            }
        prior_evidence = _sequence_set(row["evidence_sequences_json"])
        if _highly_overlapping(evidence, prior_evidence):
            return {
                "allowed": False,
                "reason": "overlapping_evidence_already_claimed",
                "existing_run": dict(row),
            }
    return {"allowed": True, "reason": None, "existing_run": None}


def _claim_run(candidate: dict, monitor_run_id: int = None) -> dict:
    batch = candidate["batch"]
    segment = candidate["segment"]
    intervention = candidate["intervention"]
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """
            SELECT b.status, es.status AS session_status,
                   gsd.status AS discussion_status, g.state AS group_state
            FROM state_assessment_batches AS b
            JOIN experiment_sessions AS es ON es.id=b.session_id
            JOIN group_session_discussions AS gsd ON gsd.id=b.discussion_id
            JOIN groups AS g ON g.id=b.group_id
            WHERE b.id=? AND b.group_id=? AND b.session_id=? AND b.discussion_id=?
            """,
            (batch["id"], batch["group_id"], batch["session_id"], batch["discussion_id"]),
        ).fetchone()
        if (
            not current
            or current["status"] != "succeeded"
            or current["session_status"] != "running"
            or current["discussion_status"] != "running"
            or str(current["group_state"] or "").upper() != "OPEN"
        ):
            conn.commit()
            return {"claimed": False, "reason": "discussion_not_running_or_room_busy"}

        help_guard = _help_guard(conn, candidate)
        if not help_guard["allowed"]:
            conn.commit()
            return {"claimed": False, **help_guard}
        duplicate_guard = _duplicate_guard(conn, candidate)
        if not duplicate_guard["allowed"]:
            conn.commit()
            existing = duplicate_guard.get("existing_run") or {}
            return {
                "claimed": False,
                "reason": duplicate_guard["reason"],
                "existing_run_id": existing.get("id"),
                "existing_status": existing.get("status"),
                "message_id": existing.get("message_id"),
                "duplicate": True,
            }

        timestamp = now_str()
        evidence = sorted(_sequence_set(segment.get("evidence_sequences")))
        strategy_id = f"state_batch:{intervention.get('reason_code') or segment['state_code']}"
        metadata = {
            "assessment_batch_id": batch["id"],
            "discussion_id": batch["discussion_id"],
            "window_key": batch.get("window_key"),
            "candidate_start_sequence": batch.get("candidate_start_sequence"),
            "candidate_end_sequence": batch.get("candidate_end_sequence"),
            "target_segment_id": segment["id"],
            "batch_trigger_type": batch.get("trigger_type"),
            "help_request_guard": help_guard,
        }
        try:
            cur = conn.execute(
                """
                INSERT INTO intervention_runs(
                    group_id, session_id, session_no, discussion_id, task_id, monitor_run_id,
                    cutoff_sequence, status, decision, assessment_batch_id,
                    target_segment_id, trigger_type, reason_code, detected_state,
                    confidence, context_from_sequence, context_to_sequence,
                    input_message_sequences_json, evidence_sequences_json,
                    selected_strategy_id, strategy_id, generated_message,
                    prompt_version, model_profile, active_segment_index,
                    guard_result, guard_reason, retry_count, raw_response,
                    metadata_json, agent_type, dry_run, started_at,
                    actual_started_at, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    batch["group_id"],
                    batch["session_id"],
                    batch.get("session_no"),
                    batch["discussion_id"],
                    batch.get("task_id"),
                    monitor_run_id,
                    batch["candidate_end_sequence"],
                    "PENDING",
                    "INTERVENE",
                    batch["id"],
                    segment["id"],
                    "auto_state",
                    intervention.get("reason_code"),
                    segment["state_code"],
                    segment.get("confidence"),
                    segment["start_sequence"],
                    segment["end_sequence"],
                    _json_text(
                        list(
                            range(
                                int(batch["candidate_start_sequence"]),
                                int(batch["candidate_end_sequence"]) + 1,
                            )
                        )
                    ),
                    _json_text(evidence),
                    strategy_id,
                    strategy_id,
                    candidate["message"],
                    batch.get("prompt_version"),
                    batch.get("model"),
                    candidate.get("active_segment_index"),
                    "allowed",
                    help_guard.get("reason_code"),
                    0,
                    batch.get("raw_response"),
                    _json_text(metadata),
                    "strategy",
                    0,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                """
                SELECT * FROM intervention_runs
                WHERE group_id=? AND session_id=?
                  AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
                  AND (
                      target_segment_id=?
                      OR (cutoff_sequence=? AND COALESCE(trigger_type, 'auto_state')='auto_state')
                  )
                ORDER BY id DESC LIMIT 1
                """,
                (
                    batch["group_id"],
                    batch["session_id"],
                    batch["discussion_id"],
                    segment["id"],
                    batch["candidate_end_sequence"],
                ),
            ).fetchone()
            conn.commit()
            return {
                "claimed": False,
                "reason": "intervention_already_claimed",
                "duplicate": True,
                "existing_run_id": existing["id"] if existing else None,
                "existing_status": existing["status"] if existing else None,
                "message_id": existing["message_id"] if existing else None,
            }
        run_id = int(cur.lastrowid)
        conn.commit()
        return {"claimed": True, "run_id": run_id, "strategy_id": strategy_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_run_status(run_id: int, status: str, **fields: Any) -> None:
    conn = db()
    try:
        sets = ["status=?"]
        params: list[Any] = [status]
        for key, value in fields.items():
            sets.append(f"{key}=?")
            params.append(value)
        params.append(run_id)
        conn.execute(f"UPDATE intervention_runs SET {', '.join(sets)} WHERE id=?", tuple(params))
        conn.commit()
    finally:
        conn.close()


class AssessmentBatchInterventionService:
    """Formal Batch 5 bridge from confirmed segments to strategy publishing."""

    @staticmethod
    def execute(batch_id: int, *, monitor_run_id: int = None) -> dict:
        from config import LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED

        if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
            return {
                "published": False,
                "skipped": True,
                "reason": "legacy_state_batch_direct_publish_disabled",
                "assessment_batch_id": int(batch_id),
            }

        candidate = _load_candidate(int(batch_id))
        if not candidate.get("valid"):
            return {
                "published": False,
                "skipped": True,
                "reason": candidate.get("reason"),
                "assessment_batch_id": int(batch_id),
            }

        batch = candidate["batch"]
        segment = candidate["segment"]
        from config import AUTO_INTERVENTION_V2_ENABLED, AUTO_INTERVENTION_V2_DRY_RUN

        if not AUTO_INTERVENTION_V2_ENABLED:
            return {
                "published": False,
                "skipped": True,
                "reason": "auto_intervention_disabled",
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }
        from services.intervention_pipeline_v2.agent_research_helper import (
            check_strategy_agent_enabled,
        )

        if not check_strategy_agent_enabled(batch["group_id"]):
            return {
                "published": False,
                "skipped": True,
                "reason": "strategy_agent_disabled",
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }
        from services.session_lifecycle import check_agent_allowed

        allowed, gate_reason = check_agent_allowed(
            batch["group_id"],
            session_id=batch["session_id"],
            task_id=batch.get("task_id"),
            session_no=batch.get("session_no"),
            agent_type="strategy",
        )
        if not allowed:
            return {
                "published": False,
                "skipped": True,
                "reason": gate_reason,
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }
        from db import get_agent_intervention_enabled_for_task

        if batch.get("task_id") and not get_agent_intervention_enabled_for_task(
            batch["task_id"], group_id=batch["group_id"]
        ):
            return {
                "published": False,
                "skipped": True,
                "reason": "task_agent_intervention_disabled",
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }

        claim = _claim_run(candidate, monitor_run_id=monitor_run_id)
        if not claim.get("claimed"):
            _log_event(
                "guard_blocked",
                group_id=batch["group_id"],
                session_id=batch["session_id"],
                discussion_id=batch["discussion_id"],
                assessment_batch_id=batch["id"],
                target_segment_id=segment["id"],
                reason=claim.get("reason"),
                existing_run_id=claim.get("existing_run_id"),
                help_request_guard={
                    key: value
                    for key, value in claim.items()
                    if key
                    in {
                        "help_request_id",
                        "help_status",
                        "handled_state_code",
                        "handled_segment_id",
                        "handled_evidence_range",
                        "target_state_code",
                        "target_segment_id",
                        "target_evidence_range",
                        "same_state",
                        "same_segment",
                        "evidence_overlap",
                        "grace_remaining_seconds",
                        "guard_evaluated",
                        "guard_blocked",
                        "reason_code",
                    }
                },
            )
            return {
                "published": bool(claim.get("message_id")),
                "skipped": True,
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
                **claim,
            }

        run_id = claim["run_id"]
        if AUTO_INTERVENTION_V2_DRY_RUN:
            _set_run_status(
                run_id,
                "DRY_RUN",
                decision="DRY_RUN",
                completed_at=now_str(),
                guard_result="allowed",
                guard_reason="dry_run_no_publish",
            )
            return {
                "published": False,
                "dry_run": True,
                "intervention_run_id": run_id,
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }

        from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

        lock_token = RoomLeaseService.acquire(batch["group_id"], run_id)
        if not lock_token:
            _set_run_status(
                run_id,
                "SKIPPED",
                decision="SKIPPED",
                guard_result="blocked",
                guard_reason="room_lease_not_acquired",
                skip_reason="room_lease_not_acquired",
                completed_at=now_str(),
            )
            return {
                "published": False,
                "skipped": True,
                "reason": "room_lease_not_acquired",
                "intervention_run_id": run_id,
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }

        lock_info = RoomLeaseService.get_lock_info(batch["group_id"])
        _set_run_status(
            run_id,
            "LOCKED",
            lock_token=lock_token,
            lock_acquired=1,
            lock_expires_at=lock_info.get("lock_expires_at"),
        )
        if not HUEY_IMMEDIATE:
            from services.intervention_pipeline_v2.intervention_service import (
                InterventionService,
            )

            InterventionService._schedule_release_expired(
                batch["group_id"],
                lock_token,
            )
        from services.agent_intervention_publisher import publish_agent_intervention

        result = None
        attempts = 0
        try:
            for attempts in range(1, 3):
                result = publish_agent_intervention(
                    group_id=batch["group_id"],
                    message=candidate["message"],
                    trigger_source="auto_state",
                    agent_type="strategy",
                    intervention_run_id=run_id,
                    monitor_run_id=monitor_run_id,
                    session_id=batch["session_id"],
                    discussion_id=batch["discussion_id"],
                    task_id=batch.get("task_id"),
                    session_no=batch.get("session_no"),
                    cutoff_sequence=batch["candidate_end_sequence"],
                    strategy_id=claim["strategy_id"],
                    title="协作策略介入",
                    teacher_reason=candidate["intervention"].get("reason_code"),
                    prompt_version=batch.get("prompt_version"),
                    model_name=batch.get("model"),
                    detected_state=segment["state_code"],
                    confidence=segment.get("confidence"),
                    lock_token=lock_token,
                    assessment_batch_id=batch["id"],
                    target_segment_id=segment["id"],
                    reason_code=candidate["intervention"].get("reason_code"),
                    active_segment_index=candidate.get("active_segment_index"),
                    evidence_sequences=sorted(
                        _sequence_set(segment.get("evidence_sequences"))
                    ),
                    guard_result="allowed",
                    guard_reason=None,
                    retry_count=attempts - 1,
                    raw_response=batch.get("raw_response"),
                    expected_lock_owner_run_id=run_id,
                    metadata={
                        "window_key": batch.get("window_key"),
                        "candidate_start_sequence": batch.get("candidate_start_sequence"),
                        "candidate_end_sequence": batch.get("candidate_end_sequence"),
                        "batch_trigger_type": batch.get("trigger_type"),
                    },
                )
                if result.get("ok") or result.get("reason") != "publish_failed":
                    break
            if result and result.get("ok"):
                _log_event(
                    "published",
                    group_id=batch["group_id"],
                    session_id=batch["session_id"],
                    discussion_id=batch["discussion_id"],
                    assessment_batch_id=batch["id"],
                    target_segment_id=segment["id"],
                    intervention_run_id=run_id,
                    message_id=result.get("message_id"),
                    trigger_type=batch.get("trigger_type"),
                    candidate_start_sequence=batch.get("candidate_start_sequence"),
                    candidate_end_sequence=batch.get("candidate_end_sequence"),
                    window_key=batch.get("window_key"),
                )
                return {
                    **result,
                    "published": True,
                    "assessment_batch_id": batch["id"],
                    "target_segment_id": segment["id"],
                    "publish_attempts": attempts,
                }
            reason = (result or {}).get("reason") or "publish_failed"
            _set_run_status(
                run_id,
                "FAILED",
                decision="FAILED",
                failure_reason=reason,
                guard_result="allowed",
                guard_reason=reason,
                retry_count=max(0, attempts - 1),
                completed_at=now_str(),
            )
            RoomLeaseService.release(batch["group_id"], lock_token)
            return {
                "published": False,
                "reason": reason,
                "intervention_run_id": run_id,
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
                "publish_attempts": attempts,
            }
        except Exception as exc:
            _set_run_status(
                run_id,
                "FAILED",
                decision="FAILED",
                failure_reason=f"{exc.__class__.__name__}: {str(exc)[:300]}",
                guard_result="allowed",
                guard_reason="publish_exception",
                retry_count=max(0, attempts - 1),
                completed_at=now_str(),
            )
            RoomLeaseService.release(batch["group_id"], lock_token)
            logger.exception("assessment batch intervention failed batch=%s", batch["id"])
            return {
                "published": False,
                "reason": "publish_exception",
                "error": str(exc),
                "intervention_run_id": run_id,
                "assessment_batch_id": batch["id"],
                "target_segment_id": segment["id"],
            }


__all__ = ["AssessmentBatchInterventionService"]
