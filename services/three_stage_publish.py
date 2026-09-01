# -*- coding: utf-8 -*-
"""Unified decision gate and publisher for three-stage strategy runs."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

from config import STRATEGY_COOLDOWN_SECONDS
from db import begin_discussion_observation, db, now_str
from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
from services.state_strategy_router import StateStrategyRouter
from services.three_stage_coordination import (
    link_pipeline_replacement,
    priority_for_pipeline_row,
    record_replacement_request,
)
from services.three_stage_schema import OI_STRATEGY_IDS, dumps_json, normalize_canonical_sub_state
from services.three_stage_stage3 import STAGE3_MAX_TEXT_CHARS, validate_stage3_text
from services.three_stage_strategy_library import get_strategy_definition
from services.three_stage_latency import (
    elapsed_ms as latency_elapsed_ms,
    latency_timer,
    latency_timestamp,
    normalize_publish_runtime_reason,
    record_latency_event,
    record_pipeline_summary,
)


THREE_STAGE_PUBLISH_CHAIN_VERSION = "state_strategy_text_publish.v1"
THREE_STAGE_STRATEGY_COOLDOWN_SECONDS = STRATEGY_COOLDOWN_SECONDS
THREE_STAGE_MAX_STRATEGY_INTERVENTIONS_PER_SESSION = int(
    os.environ.get("THREE_STAGE_MAX_STRATEGY_INTERVENTIONS_PER_SESSION", "8")
)
THREE_STAGE_DB_LOCK_MAX_RETRIES = int(
    os.environ.get("THREE_STAGE_DB_LOCK_MAX_RETRIES", "3")
)
THREE_STAGE_DB_LOCK_RETRY_BASE_SECONDS = float(
    os.environ.get("THREE_STAGE_DB_LOCK_RETRY_BASE_SECONDS", "0.05")
)

_ACTIVE_FINAL_STATUSES = {
    "PENDING",
    "ASSESSING",
    "WAITING_FOR_LOCK",
    "LOCKED",
    "PENDING_STAGE2",
    "PENDING_STAGE3",
    "GENERATING",
    "VALIDATING",
    "PENDING_DECISION_GATE",
    "READY_TO_PUBLISH",
}
_TERMINAL_RUN_STATUSES = {"PUBLISHED", "SUPPRESSED", "STALE", "FAILED", "CANCELLED", "SUPERSEDED"}
_ACTION_TERMS = ("先", "把", "说", "写", "列", "确认", "比较", "分", "选", "补", "看")
_TASK_DIRECTIVE_TERMS = ("你们应该", "必须马上", "照着写", "直接写答案")


class StudentFacingInterventionValidator:
    """Validate the final student-visible strategy sentence before publishing."""

    @staticmethod
    def validate(
        text: str,
        *,
        selected_strategy_id: str,
        canonical_sub_state: str = None,
        recent_agent_messages: list[dict] = None,
    ) -> dict:
        context = {"recent_agent_messages": list(recent_agent_messages or [])}
        base = validate_stage3_text(
            text,
            selected_strategy_id=str(selected_strategy_id or ""),
            context=context,
        )
        if not base.get("passed"):
            return base

        value = str(text or "").strip()
        if len(value) > STAGE3_MAX_TEXT_CHARS:
            return _invalid_text("text_too_long")
        if any(term in value for term in _TASK_DIRECTIVE_TERMS):
            return _invalid_text("direct_task_directive")
        if not any(term in value for term in _ACTION_TERMS):
            return _invalid_text("missing_minimal_action")
        if _looks_like_multiple_independent_actions(value):
            return _invalid_text("multiple_independent_actions")

        strategy = get_strategy_definition(str(selected_strategy_id or ""))
        canonical = normalize_canonical_sub_state(canonical_sub_state)
        if not strategy or not strategy.should_intervene:
            return _invalid_text("strategy_not_intervention")
        if canonical not in strategy.applicable_sub_states:
            return _invalid_text("strategy_not_applicable")
        return {"passed": True, "failure_code": None}


class InterventionDecisionGate:
    """Run all publish-time checks for a Stage3-complete pipeline row."""

    @staticmethod
    def evaluate(pipeline_run_id: int) -> dict:
        conn = db()
        try:
            row = _load_pipeline(conn, pipeline_run_id)
            if not row:
                return _blocked("pipeline_not_found")
            if row["publish_status"] == "PUBLISHED" and row["published_message_id"]:
                return {
                    "allowed": False,
                    "reason": "already_published",
                    "duplicate": True,
                    "published_message_id": row["published_message_id"],
                }

            structural = _structural_check(row)
            if not structural["allowed"]:
                return structural

            route_binding = _route_binding_check(row)
            if not route_binding["allowed"]:
                return route_binding

            config_check = _agent_config_check(conn, row)
            if not config_check["allowed"]:
                return config_check

            lock_check = _lock_check(conn, row)
            if not lock_check["allowed"]:
                return lock_check

            latest = _latest_student_sequence(conn, row)
            cutoff = int(row["input_cutoff_student_sequence"] or 0)
            if latest is not None and latest > cutoff:
                return _blocked(
                    "STALE_NEW_STUDENT_MESSAGE",
                    latest_student_sequence=latest,
                    input_cutoff_student_sequence=cutoff,
                    terminal_status="STALE",
                )

            help_check = _help_request_check(conn, row)
            if not help_check["allowed"]:
                return help_check

            priority_check = _higher_priority_new_run_check(conn, row)
            if not priority_check["allowed"]:
                return priority_check

            evidence_binding = _evidence_binding_check(conn, row)
            if not evidence_binding["allowed"]:
                return evidence_binding

            cooldown = _cooldown_check(conn, row)
            if not cooldown["allowed"]:
                return cooldown

            evidence_check = _evidence_overlap_check(conn, row)
            if not evidence_check["allowed"]:
                return evidence_check

            return {
                "allowed": True,
                "reason": "allowed",
                "pipeline_run_id": int(row["id"]),
                "latest_student_sequence": latest,
                "route_binding": route_binding,
                "evidence_binding": evidence_binding,
            }
        finally:
            conn.close()

    @staticmethod
    def evaluate_preflight(pipeline_run_id: int) -> dict:
        """Check cheap runtime gates before taking the Stage 3 lease.

        This is deliberately a separate, incomplete gate.  Stage 3 has not
        selected a strategy yet and no lease is required to run these checks.
        The publish-time gate below remains authoritative for state changes
        that happen after this read-only check.
        """
        conn = db()
        try:
            row = _load_pipeline(conn, pipeline_run_id)
            if not row:
                return _with_preflight_fields(
                    _blocked("pipeline_not_found", terminal_status="FAILED")
                )
            if row["publish_status"] == "PUBLISHED" and row["published_message_id"]:
                return _with_preflight_fields(
                    {
                        "allowed": False,
                        "reason": "already_published",
                        "duplicate": True,
                        "published_message_id": row["published_message_id"],
                        "terminal_status": "SUPPRESSED",
                    }
                )

            if str(row["stage2_status"] or "").upper() != "SUCCEEDED":
                return _with_preflight_fields(
                    _blocked("stage2_not_succeeded", terminal_status="FAILED")
                )
            if int(row["should_intervene"] or 0) != 1:
                return _with_preflight_fields(
                    _blocked("should_intervene_false", terminal_status="SUPPRESSED")
                )
            if (
                (
                    "fresh_detected_self_regulation" in row.keys()
                    and int(row["fresh_detected_self_regulation"] or 0) == 1
                )
                or (
                    "suppression_type" in row.keys()
                    and str(row["suppression_type"] or "").upper()
                    in {"OI", "SELF_REGULATION"}
                )
                or row["inhibition_strategy_id"]
                or str(row["selected_strategy_id"] or "").startswith("OI-")
            ):
                return _with_preflight_fields(
                    _blocked("oi_or_inhibited_strategy", terminal_status="SUPPRESSED")
                )

            route_check = _preflight_route_binding_check(row)
            if not route_check["allowed"]:
                return _with_preflight_fields(route_check)

            config_check = _agent_config_check(conn, row)
            if not config_check["allowed"]:
                return _with_preflight_fields(config_check)

            cooldown = _cooldown_check(conn, row)
            if not cooldown["allowed"]:
                return _with_preflight_fields(cooldown)

            return _with_preflight_fields(
                {
                    "allowed": True,
                    "reason": "allowed",
                    "pipeline_run_id": int(row["id"]),
                    "route_binding": route_check,
                }
            )
        finally:
            conn.close()


class ThreeStageInterventionPublisher:
    """Publish a Stage3-complete pipeline through the single decision gate."""

    @staticmethod
    def record_preflight(pipeline_run_id: int, preflight: dict) -> dict:
        """Record the bounded preflight outcome without persisting message data."""
        return record_latency_event(
            stage="preflight",
            event="preflight_gate_evaluated",
            pipeline_run_id=int(pipeline_run_id),
            details={
                "preflight_gate_result": preflight.get("preflight_gate_result"),
                "preflight_gate_reason": preflight.get("preflight_gate_reason"),
                "preflight_terminal_reason": preflight.get(
                    "preflight_terminal_reason"
                ),
                "stage3_skipped_by_preflight": bool(
                    preflight.get("stage3_skipped_by_preflight")
                ),
                "lock_skipped_by_preflight": bool(
                    preflight.get("lock_skipped_by_preflight")
                ),
                "llm_call_saved": bool(preflight.get("llm_call_saved")),
            },
            pipeline_context=True,
        )

    @staticmethod
    def finish_preflight(pipeline_run_id: int, preflight: dict) -> dict:
        """Terminalize a preflight-blocked pipeline before Stage 3 or a lease."""
        pipeline_id = int(pipeline_run_id)
        terminal_reason = str(
            preflight.get("preflight_terminal_reason")
            or preflight.get("reason")
            or "preflight_blocked"
        )
        raw_reason = str(preflight.get("reason") or terminal_reason)
        terminal_status = str(preflight.get("terminal_status") or "SUPPRESSED").upper()
        if terminal_status not in {"SUPPRESSED", "FAILED"}:
            terminal_status = "SUPPRESSED"
        publish_status = "FAILED" if terminal_status == "FAILED" else "SKIPPED"
        if preflight.get("duplicate"):
            ThreeStageInterventionPublisher.record_preflight(pipeline_id, preflight)
            return {
                "published": True,
                "duplicate": True,
                "pipeline_run_id": pipeline_id,
                "message_id": preflight.get("published_message_id"),
                "reason": "already_published",
                "failure_code": terminal_reason,
                "preflight_gate": preflight,
            }

        timestamp = latency_timestamp()
        conn = db()
        released = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _load_pipeline(conn, pipeline_id)
            if not row:
                conn.rollback()
                return {
                    "published": False,
                    "pipeline_run_id": pipeline_id,
                    "reason": "pipeline_not_found",
                    "failure_code": "pipeline_not_found",
                }
            if row["publish_status"] == "PUBLISHED" and row["published_message_id"]:
                conn.commit()
                duplicate = {
                    **preflight,
                    "allowed": False,
                    "duplicate": True,
                    "published_message_id": row["published_message_id"],
                    "preflight_terminal_reason": "ALREADY_PUBLISHED",
                }
                ThreeStageInterventionPublisher.record_preflight(
                    pipeline_id, duplicate
                )
                return {
                    "published": True,
                    "duplicate": True,
                    "pipeline_run_id": pipeline_id,
                    "message_id": row["published_message_id"],
                    "reason": "already_published",
                    "failure_code": "ALREADY_PUBLISHED",
                    "preflight_gate": duplicate,
                }

            released = _release_pipeline_lock(conn, row, timestamp)
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET stage3_status='SKIPPED',
                       stage3_completed_at=COALESCE(stage3_completed_at, ?),
                       publish_status=?,
                       final_status=?,
                       skip_reason=?,
                       failure_code=?,
                       failure_detail=?,
                       room_lock_released_at=COALESCE(room_lock_released_at, ?),
                       updated_at=?
                 WHERE id=?
                """,
                (
                    timestamp,
                    publish_status,
                    terminal_status,
                    terminal_reason,
                    terminal_reason,
                    raw_reason,
                    timestamp if released else None,
                    timestamp,
                    pipeline_id,
                ),
            )
            conn.execute(
                """
                UPDATE intervention_runs
                   SET status=?,
                       decision='SKIPPED',
                       publish_status=?,
                       skip_reason=?,
                       failure_reason=COALESCE(failure_reason, ?),
                       completed_at=?,
                       lock_acquired=0
                 WHERE strategy_pipeline_run_id=?
                """,
                (
                    "FAILED" if terminal_status == "FAILED" else "SUPPRESSED",
                    publish_status,
                    terminal_reason,
                    raw_reason,
                    timestamp,
                    pipeline_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        blocked = {
            "published": False,
            "pipeline_run_id": pipeline_id,
            "reason": raw_reason,
            "failure_code": terminal_reason,
            "final_status": terminal_status,
            "stage3_status": "SKIPPED",
            "publish_status": publish_status,
            "lock_released": released,
            "preflight_gate": preflight,
            "stage3_skipped_by_preflight": True,
            "lock_skipped_by_preflight": True,
            "llm_call_saved": True,
        }
        ThreeStageInterventionPublisher.record_preflight(pipeline_id, preflight)
        record_latency_event(
            stage="stage3",
            event="stage3_skipped_by_preflight",
            pipeline_run_id=pipeline_id,
            occurred_at=timestamp,
            details={
                "reason": raw_reason,
                "preflight_gate_result": "blocked",
                "preflight_gate_reason": raw_reason,
                "preflight_terminal_reason": terminal_reason,
                "stage3_skipped_by_preflight": True,
                "lock_skipped_by_preflight": True,
                "llm_call_saved": True,
                "stage3_status": "SKIPPED",
                "publish_status": publish_status,
                "final_status": terminal_status,
            },
            pipeline_context=True,
        )
        record_pipeline_summary(
            pipeline_id,
            event="pipeline_completed",
            publish_gate_allowed=False,
            publish_gate_reason=None,
            preflight_gate_result="blocked",
            preflight_gate_reason=raw_reason,
            stage3_skipped_by_preflight=True,
            lock_skipped_by_preflight=True,
            llm_call_saved=True,
            occurred_at=timestamp,
        )
        return blocked

    @staticmethod
    def publish_ready_pipeline(pipeline_run_id: int) -> dict:
        pipeline_id = int(pipeline_run_id)
        publish_timer = latency_timer()
        record_latency_event(
            stage="publish",
            event="publish_started",
            pipeline_run_id=pipeline_id,
            pipeline_context=True,
        )
        gate = InterventionDecisionGate.evaluate(pipeline_id)
        gate_category = normalize_publish_runtime_reason(gate.get("reason"))
        record_latency_event(
            stage="publish",
            event="publish_gate_evaluated",
            pipeline_run_id=pipeline_id,
            details={
                "publish_gate_allowed": bool(gate.get("allowed")),
                "publish_gate_result": (
                    "already_published"
                    if gate.get("duplicate")
                    else "allowed"
                    if gate.get("allowed")
                    else "blocked"
                ),
                "publish_gate_reason": gate_category,
                "failure_category": gate_category,
                "published_message_id": gate.get("published_message_id"),
            },
            pipeline_context=True,
        )
        if gate.get("duplicate"):
            record_latency_event(
                stage="publish",
                event="publish_finished",
                pipeline_run_id=pipeline_id,
                elapsed=latency_elapsed_ms(publish_timer),
                details={
                    "success": True,
                    "duplicate": True,
                    "publish_gate_allowed": False,
                    "publish_gate_result": "already_published",
                    "publish_gate_reason": "already_published",
                    "failure_category": "already_published",
                    "published_message_id": gate.get("published_message_id"),
                },
                pipeline_context=True,
            )
            record_pipeline_summary(
                pipeline_id,
                event="pipeline_duplicate",
                publish_gate_allowed=False,
                publish_gate_reason="already_published",
            )
            return {
                "published": True,
                "duplicate": True,
                "pipeline_run_id": pipeline_id,
                "message_id": gate.get("published_message_id"),
                "reason": gate.get("reason"),
            }
        if not gate.get("allowed"):
            blocked = _finish_without_publish(
                pipeline_id,
                reason=gate.get("reason") or "decision_gate_blocked",
                final_status=gate.get("terminal_status"),
                text_validation=gate.get("text_validation"),
                superseding_run_id=gate.get("superseding_run_id"),
                failure_category=gate_category,
            )
            if gate.get("reason") == "STALE_NEW_STUDENT_MESSAGE":
                blocked["replacement_assessment"] = _request_replacement_assessment(
                    pipeline_id,
                    gate.get("latest_student_sequence"),
                )
            return blocked

        lease_row = _pipeline_dict(pipeline_id)
        lease_token = lease_row.get("room_lock_token")
        if lease_token:
            lease_check = RoomLeaseService.renew_strategy_pipeline(
                pipeline_id,
                lease_token,
            )
            if not lease_check.get("renewed"):
                return _finish_without_publish(
                    pipeline_id,
                    reason=lease_check.get("reason")
                    or "room_lease_heartbeat_failed",
                    final_status="FAILED",
                    failure_category=normalize_publish_runtime_reason(
                        lease_check.get("reason") or "invalid_room_lease"
                    ),
                )

        run_result = _get_or_create_intervention_run(pipeline_id, gate=gate)
        if not run_result.get("ok"):
            return _finish_without_publish(
                pipeline_id,
                reason=run_result.get("reason") or "intervention_run_claim_failed",
                final_status="FAILED",
                failure_category=normalize_publish_runtime_reason(
                    run_result.get("reason") or "runtime_gate_blocked"
                ),
            )
        intervention_run_id = run_result["intervention_run_id"]
        row = _pipeline_dict(pipeline_id)
        audit_metadata = _publish_chain_metadata(row, pipeline_id, gate=gate)

        from services.agent_intervention_publisher import publish_agent_intervention

        try:
            publish_result = publish_agent_intervention(
                group_id=row["group_id"],
                message=row["validated_intervention_text"],
                trigger_source="auto_state",
                agent_type="strategy",
                intervention_run_id=intervention_run_id,
                session_id=row["session_id"],
                discussion_id=row["discussion_id"],
                task_id=row["task_id"],
                session_no=row["session_no"],
                cutoff_sequence=row["input_cutoff_student_sequence"],
                strategy_id=row["selected_strategy_id"],
                sub_category=audit_metadata.get("sub_category"),
                strategy_type=row["selected_strategy_type"],
                strategy_version=row["strategy_library_version"],
                strategy_pool_json=dumps_json(audit_metadata.get("strategy_pool") or []),
                selected_strategy=audit_metadata.get("selected_strategy"),
                strategy_source=audit_metadata.get("strategy_source"),
                title=row["selected_strategy_name"] or "协作策略介入",
                teacher_reason=row["strategy_selection_reason"],
                prompt_version=row["strategy_prompt_version"],
                model_name=row["strategy_model_name"],
                detected_state=row["canonical_sub_state_code"],
                confidence=row["sub_state_confidence"],
                lock_token=row["room_lock_token"],
                evidence_sequences=_json_int_list(row["sub_state_evidence_message_ids_json"]),
                guard_result="allowed",
                guard_reason=None,
                raw_response=row["strategy_raw_response_json"],
                metadata=audit_metadata,
                strategy_pipeline_run_id=pipeline_id,
                canonical_sub_state_code=row["canonical_sub_state_code"],
                strategy_candidate_ids_json=row["strategy_candidate_ids_json"],
                strategy_selection_reason=row["strategy_selection_reason"],
                evidence_message_ids_json=row["sub_state_evidence_message_ids_json"],
                input_cutoff_student_sequence=row["input_cutoff_student_sequence"],
                generated_text=row["generated_intervention_text"],
                validated_text=row["validated_intervention_text"],
                publish_status="PUBLISHED",
                expected_latest_student_sequence=row["input_cutoff_student_sequence"],
                expected_lock_owner_run_id=-pipeline_id,
            )
        except Exception as exc:
            return _finish_without_publish(
                pipeline_id,
                reason="publish_worker_exception",
                final_status="FAILED",
                failure_code="FAILED_PUBLISH",
                intervention_run_id=intervention_run_id,
                failure_category="runtime_gate_blocked",
            )
        if not publish_result.get("ok"):
            if _is_db_locked_publish_result(publish_result):
                for attempt in range(2, max(1, THREE_STAGE_DB_LOCK_MAX_RETRIES) + 1):
                    time.sleep(THREE_STAGE_DB_LOCK_RETRY_BASE_SECONDS * attempt)
                    gate = InterventionDecisionGate.evaluate(pipeline_id)
                    if gate.get("duplicate"):
                        return {
                            "published": True,
                            "duplicate": True,
                            "pipeline_run_id": pipeline_id,
                            "message_id": gate.get("published_message_id"),
                            "reason": gate.get("reason"),
                        }
                    if not gate.get("allowed"):
                        return _finish_without_publish(
                            pipeline_id,
                            reason=gate.get("reason") or "decision_gate_blocked",
                            final_status=gate.get("terminal_status"),
                            text_validation=gate.get("text_validation"),
                            superseding_run_id=gate.get("superseding_run_id"),
                            intervention_run_id=intervention_run_id,
                            failure_category=normalize_publish_runtime_reason(
                                gate.get("reason") or "runtime_gate_blocked"
                            ),
                        )
                    try:
                        publish_result = _call_publish_agent_intervention(
                            _pipeline_dict(pipeline_id),
                            pipeline_id=pipeline_id,
                            intervention_run_id=intervention_run_id,
                            gate=gate,
                        )
                    except Exception:
                        publish_result = {
                            "ok": False,
                            "reason": "publish_worker_exception",
                            "error": "publish_worker_exception",
                        }
                    if publish_result.get("ok") or not _is_db_locked_publish_result(publish_result):
                        break
            if not publish_result.get("ok"):
                publish_reason = publish_result.get("reason") or "FAILED_PUBLISH"
                if _is_db_locked_publish_result(publish_result):
                    publish_reason = "DB_LOCK_RETRY_EXHAUSTED"
                blocked = _finish_without_publish(
                    pipeline_id,
                    reason=publish_reason,
                    final_status="STALE"
                    if publish_reason == "stale_student_sequence"
                    else "FAILED",
                    failure_code=publish_reason,
                    intervention_run_id=intervention_run_id,
                    failure_category=normalize_publish_runtime_reason(publish_reason),
                )
                if publish_reason == "stale_student_sequence":
                    blocked["replacement_assessment"] = _request_replacement_assessment(
                        pipeline_id,
                        publish_result.get("latest_student_sequence"),
                    )
                return blocked
        committed_at = latency_timestamp()
        publish_elapsed = latency_elapsed_ms(publish_timer)
        record_latency_event(
            stage="publish",
            event="message_committed",
            pipeline_run_id=pipeline_id,
            occurred_at=committed_at,
            elapsed=publish_elapsed,
            details={
                "success": True,
                "publish_gate_allowed": True,
                "publish_gate_result": "published",
                "publish_gate_reason": "allowed",
                "published_message_id": publish_result.get("message_id"),
            },
            pipeline_context=True,
        )
        if row.get("room_lock_token"):
            record_latency_event(
                stage="lock",
                event="room_lock_released",
                pipeline_run_id=pipeline_id,
                occurred_at=committed_at,
                elapsed=publish_elapsed,
                lock_token=row["room_lock_token"],
                details={
                    "reason": "message_committed",
                    "lease_action": "release",
                    "lease_released": True,
                },
                pipeline_context=True,
            )
        return _mark_published(
            pipeline_id,
            intervention_run_id=publish_result["intervention_run_id"],
            message_id=publish_result["message_id"],
        )


def _call_publish_agent_intervention(
    row,
    *,
    pipeline_id: int,
    intervention_run_id: int,
    gate: dict = None,
) -> dict:
    from services.agent_intervention_publisher import publish_agent_intervention

    audit_metadata = _publish_chain_metadata(row, pipeline_id, gate=gate)
    return publish_agent_intervention(
        group_id=row["group_id"],
        message=row["validated_intervention_text"],
        trigger_source="auto_state",
        agent_type="strategy",
        intervention_run_id=intervention_run_id,
        session_id=row["session_id"],
        discussion_id=row["discussion_id"],
        task_id=row["task_id"],
        session_no=row["session_no"],
        cutoff_sequence=row["input_cutoff_student_sequence"],
        strategy_id=row["selected_strategy_id"],
        sub_category=audit_metadata.get("sub_category"),
        strategy_type=row["selected_strategy_type"],
        strategy_version=row["strategy_library_version"],
        strategy_pool_json=dumps_json(audit_metadata.get("strategy_pool") or []),
        selected_strategy=audit_metadata.get("selected_strategy"),
        strategy_source=audit_metadata.get("strategy_source"),
        title=row["selected_strategy_name"] or "Strategy intervention",
        teacher_reason=row["strategy_selection_reason"],
        prompt_version=row["strategy_prompt_version"],
        model_name=row["strategy_model_name"],
        detected_state=row["canonical_sub_state_code"],
        confidence=row["sub_state_confidence"],
        lock_token=row["room_lock_token"],
        evidence_sequences=_json_int_list(row["sub_state_evidence_message_ids_json"]),
        guard_result="allowed",
        guard_reason=None,
        raw_response=row["strategy_raw_response_json"],
        metadata=audit_metadata,
        strategy_pipeline_run_id=pipeline_id,
        canonical_sub_state_code=row["canonical_sub_state_code"],
        strategy_candidate_ids_json=row["strategy_candidate_ids_json"],
        strategy_selection_reason=row["strategy_selection_reason"],
        evidence_message_ids_json=row["sub_state_evidence_message_ids_json"],
        input_cutoff_student_sequence=row["input_cutoff_student_sequence"],
        generated_text=row["generated_intervention_text"],
        validated_text=row["validated_intervention_text"],
        publish_status="PUBLISHED",
        expected_latest_student_sequence=row["input_cutoff_student_sequence"],
        expected_lock_owner_run_id=-pipeline_id,
    )


def _selected_strategy_audit(row) -> dict:
    strategy_id = str(row["selected_strategy_id"] or "").strip()
    definition = get_strategy_definition(strategy_id) if strategy_id else None
    return {
        "strategy_id": strategy_id,
        "strategy_name": row["selected_strategy_name"] or (
            definition.strategy_name if definition else None
        ),
        "strategy_type": row["selected_strategy_type"] or (
            definition.strategy_type if definition else None
        ),
        "supporting_strategy_ids": _json_string_list(row["supporting_strategy_ids_json"]),
        "selection_reason": row["strategy_selection_reason"],
    }


def _publish_chain_metadata(row, pipeline_id: int, *, gate: dict = None) -> dict:
    route_binding = (gate or {}).get("route_binding") or {}
    if not route_binding:
        route_binding = _route_binding_details(row)
    evidence_binding = (gate or {}).get("evidence_binding") or {}
    text_validation = (gate or {}).get("text_validation") or {}
    evidence_message_ids = _json_int_list(row["sub_state_evidence_message_ids_json"])
    strategy_pool = list(route_binding.get("strategy_pool") or [])
    selected_strategy = _selected_strategy_audit(row)
    return {
        "strategy_pipeline_run_id": pipeline_id,
        "publish_chain_version": THREE_STAGE_PUBLISH_CHAIN_VERSION,
        "sub_category": route_binding.get("sub_category"),
        "canonical_sub_state_code": row["canonical_sub_state_code"],
        "strategy_pool": strategy_pool,
        "selected_strategy_id": row["selected_strategy_id"],
        "selected_strategy": selected_strategy,
        "strategy_candidate_ids": _json_string_list(row["strategy_candidate_ids_json"]),
        "strategy_source": route_binding.get("strategy_source"),
        "strategy_library_version": row["strategy_library_version"],
        "strategy_library_hash": row["strategy_library_hash"],
        "evidence_message_ids": evidence_message_ids,
        "route_binding": route_binding,
        "evidence_binding": evidence_binding,
        "text_validation": text_validation,
    }


def _is_db_locked_publish_result(result: dict) -> bool:
    text = " ".join(
        str((result or {}).get(key) or "")
        for key in ("reason", "error", "failure_code", "failure_detail")
    ).lower()
    return "database is locked" in text or "database locked" in text


def _structural_check(row) -> dict:
    if row["stage2_status"] != "SUCCEEDED":
        return _blocked("stage2_not_succeeded", terminal_status="FAILED")
    if row["stage3_status"] != "SUCCEEDED":
        return _blocked("stage3_not_succeeded", terminal_status="FAILED")
    if (
        "fresh_detected_self_regulation" in row.keys()
        and int(row["fresh_detected_self_regulation"] or 0) == 1
    ):
        return _blocked(
            "SELF_REGULATION_SUPPRESSED",
            terminal_status="SUPPRESSED",
        )
    if (
        "suppression_type" in row.keys()
        and str(row["suppression_type"] or "").upper() in {"OI", "SELF_REGULATION"}
    ):
        return _blocked(
            (row["suppression_decision_reason"] or "strong_suppression_gate")
            if "suppression_decision_reason" in row.keys()
            else "strong_suppression_gate",
            terminal_status="SUPPRESSED",
        )
    if int(row["should_intervene"] or 0) != 1:
        return _blocked("should_intervene_false", terminal_status="SUPPRESSED")
    if row["inhibition_strategy_id"] or str(row["selected_strategy_id"] or "").startswith("OI-"):
        return _blocked("oi_or_inhibited_strategy", terminal_status="SUPPRESSED")
    if not row["selected_strategy_id"]:
        return _blocked("missing_selected_strategy_id", terminal_status="FAILED")
    if not row["validated_intervention_text"]:
        return _blocked("missing_validated_text", terminal_status="FAILED")
    if row["final_status"] not in {"PENDING_DECISION_GATE", "READY_TO_PUBLISH", "VALIDATING"}:
        return _blocked("pipeline_not_ready_to_publish", terminal_status="FAILED")
    return {"allowed": True}


def _route_binding_details(row) -> dict:
    selected_id = str(row["selected_strategy_id"] or "").strip()
    secondary_tags = _json_string_list(row["secondary_sub_state_tags_json"])
    route = StateStrategyRouter().route(
        row["canonical_sub_state_code"],
        secondary_tags=secondary_tags,
    )
    strategy_pool = list(route.strategy_pool)
    persisted_candidates = _json_string_list(row["strategy_candidate_ids_json"])
    effective_candidates = [
        strategy_id for strategy_id in persisted_candidates if strategy_id in strategy_pool
    ]
    details = {
        "publish_chain_version": THREE_STAGE_PUBLISH_CHAIN_VERSION,
        "sub_category": route.sub_category,
        "canonical_state": route.canonical_state,
        "route_mode": route.route_mode,
        "terminal_decision": route.terminal_decision,
        "should_intervene": route.should_intervene,
        "route_overlay_tag": route.route_overlay_tag,
        "strategy_pool": strategy_pool,
        "persisted_candidate_ids": persisted_candidates,
        "effective_candidate_ids": effective_candidates,
        "selected_strategy_id": selected_id,
        "strategy_source": route.strategy_source,
    }
    return details


def _route_binding_check(row) -> dict:
    details = _route_binding_details(row)
    selected_id = details["selected_strategy_id"]
    strategy_pool = details["strategy_pool"]
    persisted_candidates = details["persisted_candidate_ids"]
    effective_candidates = details["effective_candidate_ids"]
    if not strategy_pool:
        return _blocked(
            "state_route_has_no_strategy_pool",
            terminal_status="FAILED",
            route_binding=details,
        )
    if not (
        details.get("should_intervene")
        or details.get("terminal_decision") == "OPTIONAL_SUPPORT"
    ):
        return _blocked(
            "state_route_not_publishable",
            terminal_status="SUPPRESSED",
            route_binding=details,
        )
    if not persisted_candidates:
        return _blocked(
            "missing_strategy_candidate_pool",
            terminal_status="FAILED",
            route_binding=details,
        )
    if not effective_candidates:
        return _blocked(
            "strategy_candidate_pool_not_in_route",
            terminal_status="FAILED",
            route_binding=details,
        )
    if selected_id not in strategy_pool:
        return _blocked(
            "selected_strategy_not_in_route_pool",
            terminal_status="FAILED",
            route_binding=details,
        )
    if selected_id not in effective_candidates:
        return _blocked(
            "selected_strategy_not_in_candidate_pool",
            terminal_status="FAILED",
            route_binding=details,
        )
    return {"allowed": True, **details}


def _preflight_route_binding_check(row) -> dict:
    """Validate the Stage2 route without requiring Stage3's selected strategy."""
    details = _route_binding_details(row)
    strategy_pool = details["strategy_pool"]
    persisted_candidates = details["persisted_candidate_ids"]
    effective_candidates = details["effective_candidate_ids"]
    if not strategy_pool:
        return _blocked(
            "state_route_has_no_strategy_pool",
            terminal_status="FAILED",
            route_binding=details,
        )
    if not details.get("should_intervene"):
        return _blocked(
            "state_route_not_publishable",
            terminal_status="SUPPRESSED",
            route_binding=details,
        )
    if not persisted_candidates:
        return _blocked(
            "missing_strategy_candidate_pool",
            terminal_status="FAILED",
            route_binding=details,
        )
    if not effective_candidates:
        return _blocked(
            "strategy_candidate_pool_not_in_route",
            terminal_status="FAILED",
            route_binding=details,
        )
    return {"allowed": True, **details}


def _request_replacement_assessment(
    pipeline_id: int,
    latest_student_sequence: Optional[int],
) -> dict:
    """Queue a fresh authoritative window after an old Stage 3 result goes stale."""
    row = _pipeline_dict(pipeline_id)
    if not row:
        return {"created": False, "enqueued": False, "reason": "pipeline_not_found"}
    try:
        from services.state_assessment_scheduler import request_state_assessment

        return request_state_assessment(
            group_id=row["group_id"],
            session_id=row["session_id"],
            discussion_id=row["discussion_id"],
            trigger_type="message_count_periodic",
            trigger_sequence=latest_student_sequence,
            continuation=True,
            replacement_of_pipeline_run_id=pipeline_id,
            replacement_reason="STALE_NEW_STUDENT_MESSAGE",
            replacement_cutoff_sequence=latest_student_sequence,
        )
    except Exception as exc:
        return {
            "created": False,
            "enqueued": False,
            "reason": "replacement_assessment_failed",
            "error": str(exc)[:300],
        }


def _agent_config_check(conn, row) -> dict:
    import config
    from db import get_agent_intervention_enabled_for_task
    from services.intervention_pipeline_v2.agent_research_helper import check_strategy_agent_enabled
    from services.session_lifecycle import check_agent_allowed

    if not bool(getattr(config, "AUTO_INTERVENTION_V2_ENABLED", False)):
        return _blocked("auto_intervention_disabled", terminal_status="SUPPRESSED")
    group_row = conn.execute(
        "SELECT COALESCE(auto_intervention_enabled, 1) AS enabled FROM groups WHERE id=?",
        (row["group_id"],),
    ).fetchone()
    if not group_row or not bool(group_row["enabled"]):
        return _blocked("group_auto_intervention_disabled", terminal_status="SUPPRESSED")
    if not check_strategy_agent_enabled(row["group_id"]):
        return _blocked("strategy_agent_disabled", terminal_status="SUPPRESSED")
    allowed, reason = check_agent_allowed(
        row["group_id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        session_no=row["session_no"],
        agent_type="strategy",
    )
    if not allowed:
        return _blocked(reason, terminal_status="SUPPRESSED")
    if row["task_id"] and not get_agent_intervention_enabled_for_task(
        row["task_id"], group_id=row["group_id"]
    ):
        return _blocked("task_agent_intervention_disabled", terminal_status="SUPPRESSED")
    return {"allowed": True}


def _lock_check(conn, row) -> dict:
    token = row["room_lock_token"]
    if not token:
        return _blocked("missing_room_lock", terminal_status="FAILED")
    group = conn.execute(
        """
        SELECT state, lock_token, lock_expires_at, active_intervention_run_id
        FROM groups
        WHERE id=?
        """,
        (row["group_id"],),
    ).fetchone()
    if not group:
        return _blocked("group_not_found", terminal_status="FAILED")
    if group["state"] != "AI_INTERVENING":
        return _blocked("room_not_locked", terminal_status="FAILED")
    if group["lock_token"] != token:
        return _blocked("lock_token_mismatch", terminal_status="FAILED")
    if group["active_intervention_run_id"] != -int(row["id"]):
        return _blocked("lock_owner_mismatch", terminal_status="FAILED")
    expires_at = _parse_dt(group["lock_expires_at"])
    if expires_at and expires_at <= datetime.now():
        return _blocked("lock_expired", terminal_status="FAILED")
    return {"allowed": True}


def _help_request_check(conn, row) -> dict:
    if str(row["trigger_source"] or "") in {"student_help", "student_help_request", "help_request"}:
        return {"allowed": True}
    pending = conn.execute(
        """
        SELECT id, status
        FROM help_requests
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND status IN ('QUEUED','RUNNING')
        ORDER BY id ASC
        LIMIT 1
        """,
        (row["group_id"], row["session_id"], row["discussion_id"]),
    ).fetchone()
    if pending:
        return _blocked(
            "pending_help_request",
            terminal_status="SUPPRESSED",
            help_request_id=pending["id"],
        )
    return {"allowed": True}


def _higher_priority_new_run_check(conn, row) -> dict:
    current_priority = priority_for_pipeline_row(row)
    current_cutoff = int(row["input_cutoff_student_sequence"] or 0)
    newer_rows = conn.execute(
        """
        SELECT *
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND id<>?
          AND COALESCE(input_cutoff_student_sequence, 0)>?
          AND COALESCE(final_status, '') NOT IN (
              'PUBLISHED','SUPPRESSED','STALE','FAILED','CANCELLED','SUPERSEDED'
          )
        ORDER BY COALESCE(input_cutoff_student_sequence, 0) DESC, id DESC
        LIMIT 8
        """,
        (
            row["group_id"],
            row["session_id"],
            row["discussion_id"],
            row["id"],
            current_cutoff,
        ),
    ).fetchall()
    higher = [
        candidate
        for candidate in newer_rows
        if priority_for_pipeline_row(candidate) < current_priority
    ]
    if higher:
        higher.sort(
            key=lambda item: (
                priority_for_pipeline_row(item),
                -int(item["input_cutoff_student_sequence"] or 0),
                -int(item["id"]),
            )
        )
        return _blocked(
            "higher_priority_newer_run",
            terminal_status="SUPERSEDED",
            superseding_run_id=higher[0]["id"],
        )
    return {"allowed": True}


def _evidence_binding_check(conn, row) -> dict:
    evidence_sequences = _json_int_list(row["sub_state_evidence_message_ids_json"])
    if not evidence_sequences:
        return _blocked("missing_evidence", terminal_status="FAILED")

    start_sequence = _optional_int(row["input_start_sequence"])
    end_sequence = _optional_int(row["input_end_sequence"])
    if end_sequence is None:
        end_sequence = _optional_int(row["input_cutoff_student_sequence"])
    if start_sequence is not None and end_sequence is not None:
        outside = [
            sequence
            for sequence in evidence_sequences
            if sequence < start_sequence or sequence > end_sequence
        ]
        if outside:
            return _blocked(
                "evidence_outside_pipeline_window",
                terminal_status="FAILED",
                evidence_sequences=evidence_sequences,
                outside_evidence_sequences=outside,
            )

    placeholders = ",".join("?" for _ in evidence_sequences)
    rows = conn.execute(
        f"""
        SELECT m.id, m.sequence
        FROM messages AS m
        LEFT JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=?
          AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
          AND m.sequence IN ({placeholders})
          AND COALESCE(NULLIF(TRIM(m.role), ''), m.sender_type, u.role)='student'
        """,
        (
            row["group_id"],
            row["session_id"],
            row["discussion_id"],
            *evidence_sequences,
        ),
    ).fetchall()
    found_by_sequence = {
        int(item["sequence"]): int(item["id"])
        for item in rows
        if item["sequence"] is not None
    }
    missing = [
        sequence for sequence in evidence_sequences if sequence not in found_by_sequence
    ]
    if missing:
        return _blocked(
            "evidence_not_bound_to_student_messages",
            terminal_status="FAILED",
            evidence_sequences=evidence_sequences,
            missing_evidence_sequences=missing,
        )

    segment = conn.execute(
        """
        SELECT evidence_sequences, evidence_message_ids_json
        FROM collaboration_state_segments
        WHERE strategy_pipeline_run_id=?
          AND COALESCE(is_active_at_batch_end, 0)=1
        ORDER BY id DESC
        LIMIT 1
        """,
        (row["id"],),
    ).fetchone()
    if segment:
        segment_sequences = _json_int_list(segment["evidence_sequences"])
        if not segment_sequences:
            segment_sequences = _json_int_list(segment["evidence_message_ids_json"])
        if segment_sequences and set(segment_sequences) != set(evidence_sequences):
            return _blocked(
                "evidence_segment_mismatch",
                terminal_status="FAILED",
                evidence_sequences=evidence_sequences,
                segment_evidence_sequences=segment_sequences,
            )

    return {
        "allowed": True,
        "evidence_sequences": evidence_sequences,
        "evidence_message_ids": [
            found_by_sequence[sequence] for sequence in evidence_sequences
        ],
    }


def _cooldown_check(conn, row) -> dict:
    latest_strategy = _latest_strategy_publish(conn, row)
    if latest_strategy:
        seconds = _seconds_since(latest_strategy["published_at"])
        if seconds is not None and seconds < THREE_STAGE_STRATEGY_COOLDOWN_SECONDS:
            return _blocked(
                "strategy_cooldown",
                terminal_status="SUPPRESSED",
                seconds_since_strategy=seconds,
            )
        last_sequence = _message_sequence(conn, latest_strategy["published_message_id"])
        cutoff = int(row["input_cutoff_student_sequence"] or 0)
        if last_sequence is not None and cutoff <= last_sequence:
            return _blocked("no_student_feedback_after_last_intervention", terminal_status="SUPPRESSED")

    count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND publish_status='PUBLISHED'
        """,
        (row["group_id"], row["session_id"]),
    ).fetchone()
    if int(count["count"] if count else 0) >= THREE_STAGE_MAX_STRATEGY_INTERVENTIONS_PER_SESSION:
        return _blocked("strategy_intervention_cap_reached", terminal_status="SUPPRESSED")
    return {"allowed": True}


def _evidence_overlap_check(conn, row) -> dict:
    evidence = set(_json_int_list(row["sub_state_evidence_message_ids_json"]))
    if not evidence:
        return _blocked("missing_evidence", terminal_status="FAILED")
    rows = conn.execute(
        """
        SELECT id, sub_state_evidence_message_ids_json
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND id<>?
          AND publish_status='PUBLISHED'
        ORDER BY id DESC
        LIMIT 8
        """,
        (row["group_id"], row["session_id"], row["discussion_id"], row["id"]),
    ).fetchall()
    for previous in rows:
        prior = set(_json_int_list(previous["sub_state_evidence_message_ids_json"]))
        if prior and len(evidence & prior) / min(len(evidence), len(prior)) >= 0.5:
            return _blocked(
                "overlapping_evidence_already_published",
                terminal_status="SUPPRESSED",
                overlapping_run_id=previous["id"],
            )
    return {"allowed": True}


def _get_or_create_intervention_run(pipeline_id: int, *, gate: dict = None) -> dict:
    timestamp = now_str()
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _load_pipeline(conn, pipeline_id)
        if not row:
            conn.rollback()
            return {"ok": False, "reason": "pipeline_not_found"}
        existing = conn.execute(
            """
            SELECT id, message_id, status
            FROM intervention_runs
            WHERE strategy_pipeline_run_id=?
            ORDER BY id ASC
            LIMIT 1
            """,
            (pipeline_id,),
        ).fetchone()
        if existing:
            conn.commit()
            return {
                "ok": True,
                "intervention_run_id": existing["id"],
                "existing": True,
                "message_id": existing["message_id"],
                "status": existing["status"],
            }
        audit_metadata = _publish_chain_metadata(row, pipeline_id, gate=gate)
        cur = conn.execute(
            """
            INSERT INTO intervention_runs(
                group_id, session_id, session_no, discussion_id, task_id,
                cutoff_sequence, status, decision, trigger_type,
                detected_state, confidence, context_from_sequence,
                context_to_sequence, input_message_sequences_json,
                evidence_sequences_json, selected_strategy_id, strategy_id,
                selected_strategy, sub_category, strategy_pool_json,
                strategy_source, generated_message, prompt_version, model_profile, lock_token,
                lock_acquired, strategy_pipeline_run_id,
                canonical_sub_state_code, strategy_candidate_ids_json,
                strategy_selection_reason, evidence_message_ids_json,
                input_cutoff_student_sequence, generated_text, validated_text,
                publish_status, metadata_json, agent_type, dry_run,
                started_at, actual_started_at, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["group_id"],
                row["session_id"],
                row["session_no"],
                row["discussion_id"],
                row["task_id"],
                row["input_cutoff_student_sequence"],
                "LOCKED",
                "INTERVENE",
                "auto_state",
                row["canonical_sub_state_code"],
                row["sub_state_confidence"],
                row["input_start_sequence"],
                row["input_end_sequence"],
                dumps_json(
                    list(
                        range(
                            int(row["input_start_sequence"] or row["input_cutoff_student_sequence"] or 0),
                            int(row["input_end_sequence"] or row["input_cutoff_student_sequence"] or 0) + 1,
                        )
                    )
                ),
                row["sub_state_evidence_message_ids_json"],
                row["selected_strategy_id"],
                row["selected_strategy_id"],
                dumps_json(audit_metadata.get("selected_strategy")),
                audit_metadata.get("sub_category"),
                dumps_json(audit_metadata.get("strategy_pool") or []),
                audit_metadata.get("strategy_source"),
                row["validated_intervention_text"],
                row["strategy_prompt_version"],
                row["strategy_model_name"],
                row["room_lock_token"],
                1,
                pipeline_id,
                row["canonical_sub_state_code"],
                row["strategy_candidate_ids_json"],
                row["strategy_selection_reason"],
                row["sub_state_evidence_message_ids_json"],
                row["input_cutoff_student_sequence"],
                row["generated_intervention_text"],
                row["validated_intervention_text"],
                "PENDING",
                dumps_json(audit_metadata),
                "strategy",
                0,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        return {"ok": True, "intervention_run_id": int(cur.lastrowid), "existing": False}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "reason": "intervention_run_claim_failed", "error": str(exc)}
    finally:
        conn.close()


def _finish_without_publish(
    pipeline_id: int,
    *,
    reason: str,
    final_status: str = None,
    failure_code: str = None,
    text_validation: dict = None,
    intervention_run_id: int = None,
    superseding_run_id: int = None,
    failure_category: str = None,
) -> dict:
    final = final_status or _default_final_status(reason)
    publish_status = "FAILED" if final == "FAILED" else "SKIPPED"
    timestamp = latency_timestamp()
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _load_pipeline(conn, pipeline_id)
        if not row:
            conn.rollback()
            return {"published": False, "reason": "pipeline_not_found", "pipeline_run_id": pipeline_id}
        replacement_request = None
        replacement_link = None
        if final == "STALE":
            latest_sequence = _latest_student_sequence(conn, row)
            replacement_request = record_replacement_request(
                conn,
                pipeline_id,
                reason=str(reason or "STALE_NEW_STUDENT_MESSAGE"),
                cutoff_sequence=latest_sequence,
            )
        released = _release_pipeline_lock(conn, row, timestamp)
        conn.execute(
            """
            UPDATE strategy_pipeline_runs
            SET publish_status=?,
                final_status=?,
                skip_reason=?,
                superseded_by_run_id=COALESCE(?, superseded_by_run_id),
                failure_code=COALESCE(?, failure_code),
                text_validation_result_json=COALESCE(?, text_validation_result_json),
                room_lock_released_at=COALESCE(room_lock_released_at, ?),
                updated_at=?
            WHERE id=?
            """,
            (
                publish_status,
                final,
                reason,
                superseding_run_id,
                failure_code,
                dumps_json(text_validation) if text_validation else None,
                timestamp if released else None,
                timestamp,
                pipeline_id,
            ),
        )
        if intervention_run_id:
            conn.execute(
                """
                UPDATE intervention_runs
                SET status=?,
                    decision='SKIPPED',
                    publish_status=?,
                    skip_reason=?,
                    failure_reason=COALESCE(?, failure_reason),
                    completed_at=?,
                    lock_acquired=0
                WHERE id=?
                """,
                (
                    "FAILED" if final == "FAILED" else final,
                    publish_status,
                    reason,
                    failure_code or reason,
                    timestamp,
                    intervention_run_id,
                ),
            )
        if superseding_run_id is not None:
            replacement_link = link_pipeline_replacement(
                conn,
                pipeline_id,
                int(superseding_run_id),
                reason=reason,
            )
        conn.commit()
        category = failure_category or normalize_publish_runtime_reason(reason)
        record_latency_event(
            stage="publish",
            event="publish_finished",
            pipeline_run_id=pipeline_id,
            occurred_at=timestamp,
            details={
                "success": False,
                "publish_gate_allowed": False,
                "publish_gate_result": "blocked",
                "publish_gate_reason": category,
                "failure_category": category,
                "lease_released": bool(released),
            },
            pipeline_context=True,
        )
        record_pipeline_summary(
            pipeline_id,
            event="pipeline_completed",
            publish_gate_allowed=False,
            publish_gate_reason=category,
            occurred_at=timestamp,
        )
        return {
            "published": False,
            "pipeline_run_id": pipeline_id,
            "reason": reason,
            "final_status": final,
            "publish_status": publish_status,
            "lock_released": released,
            "superseding_run_id": superseding_run_id,
            "replacement_request": replacement_request,
            "replacement_link": replacement_link,
            "failure_category": category,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_published(pipeline_id: int, *, intervention_run_id: int, message_id: int) -> dict:
    timestamp = latency_timestamp()
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _load_pipeline(conn, pipeline_id)
        if not row:
            conn.rollback()
            return {"published": False, "reason": "pipeline_not_found", "pipeline_run_id": pipeline_id}
        audit_metadata = _publish_chain_metadata(row, pipeline_id)
        latest = _latest_student_sequence(conn, row)
        from services.three_stage_observation import mark_observation_started

        observation_started = mark_observation_started(
            conn,
            pipeline_row=row,
            message_id=message_id,
            timestamp=timestamp,
        )
        observation_cursor = begin_discussion_observation(
            conn,
            group_id=row["group_id"],
            session_id=row["session_id"],
            discussion_id=row["discussion_id"],
            intervention_sequence=observation_started.get("published_sequence"),
            updated_at=timestamp,
        )
        observation_started["cursor"] = observation_cursor
        conn.execute(
            """
            UPDATE strategy_pipeline_runs
            SET publish_status='PUBLISHED',
                published_message_id=?,
                published_at=COALESCE(published_at, ?),
                latest_sequence_at_publish=?,
                sub_category=?,
                evidence_message_ids_json=?,
                strategy_pool_json=?,
                selected_strategy_json=?,
                strategy_source=?,
                final_status='PUBLISHED',
                skip_reason=NULL,
                failure_code=NULL,
                failure_detail=NULL,
                room_lock_released_at=COALESCE(room_lock_released_at, ?),
                updated_at=?
            WHERE id=?
            """,
            (
                message_id,
                timestamp,
                latest,
                audit_metadata.get("sub_category"),
                dumps_json(audit_metadata.get("evidence_message_ids") or []),
                dumps_json(audit_metadata.get("strategy_pool") or []),
                dumps_json(audit_metadata.get("selected_strategy")),
                audit_metadata.get("strategy_source"),
                timestamp,
                timestamp,
                pipeline_id,
            ),
        )
        conn.execute(
            """
            UPDATE intervention_runs
            SET publish_status='PUBLISHED',
                strategy_pipeline_run_id=?,
                canonical_sub_state_code=?,
                sub_category=?,
                strategy_candidate_ids_json=?,
                strategy_pool_json=?,
                strategy_selection_reason=?,
                selected_strategy=?,
                strategy_source=?,
                evidence_message_ids_json=?,
                input_cutoff_student_sequence=?,
                generated_text=?,
                validated_text=?
            WHERE id=?
            """,
            (
                pipeline_id,
                row["canonical_sub_state_code"],
                audit_metadata.get("sub_category"),
                row["strategy_candidate_ids_json"],
                dumps_json(audit_metadata.get("strategy_pool") or []),
                row["strategy_selection_reason"],
                dumps_json(audit_metadata.get("selected_strategy")),
                audit_metadata.get("strategy_source"),
                row["sub_state_evidence_message_ids_json"],
                row["input_cutoff_student_sequence"],
                row["generated_intervention_text"],
                row["validated_intervention_text"],
                intervention_run_id,
            ),
        )
        conn.execute(
            """
            UPDATE collaboration_state_segments
            SET intervention_run_id=?,
                intervention_published_at=COALESCE(intervention_published_at, ?),
                intervention_disposition='PUBLISHED',
                selected_strategy_id=?,
                updated_at=?
            WHERE strategy_pipeline_run_id=?
              AND COALESCE(is_active_at_batch_end, 0)=1
            """,
            (
                intervention_run_id,
                timestamp,
                row["selected_strategy_id"],
                timestamp,
                pipeline_id,
            ),
        )
        conn.commit()
        record_pipeline_summary(
            pipeline_id,
            event="pipeline_completed",
            publish_gate_allowed=True,
            publish_gate_reason="allowed",
            occurred_at=timestamp,
        )
        return {
            "published": True,
            "pipeline_run_id": pipeline_id,
            "intervention_run_id": intervention_run_id,
            "message_id": message_id,
            "publish_status": "PUBLISHED",
            "final_status": "PUBLISHED",
            "observation_started": observation_started,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_pipeline_lock(conn, row, timestamp: str) -> bool:
    token = row["room_lock_token"] if "room_lock_token" in row.keys() else None
    if not token:
        return False
    cur = conn.execute(
        """
        UPDATE groups
        SET state='OPEN',
            version=version+1,
            lock_token=NULL,
            lock_expires_at=NULL,
            active_intervention_run_id=NULL
        WHERE id=? AND lock_token=? AND active_intervention_run_id=?
        """,
        (row["group_id"], token, -int(row["id"])),
    )
    released = cur.rowcount == 1
    if released:
        record_latency_event(
            stage="lock",
            event="room_lock_released",
            pipeline_run_id=row["id"],
            assessment_batch_id=(
                row["assessment_batch_id"]
                if "assessment_batch_id" in row.keys()
                else None
            ),
            occurred_at=timestamp,
            lock_token=token,
            details={
                "reason": "publish_terminal_without_message",
                "lease_action": "release",
                "lease_released": True,
            },
            conn=conn,
            pipeline_context=True,
        )
    return released


def _load_pipeline(conn, pipeline_run_id: int):
    return conn.execute(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?",
        (int(pipeline_run_id),),
    ).fetchone()


def _pipeline_dict(pipeline_id: int) -> dict:
    conn = db()
    try:
        row = _load_pipeline(conn, pipeline_id)
        if not row:
            raise ValueError("pipeline_not_found")
        return dict(row)
    finally:
        conn.close()


def _latest_student_sequence(conn, row) -> Optional[int]:
    result = conn.execute(
        """
        SELECT MAX(m.sequence) AS latest_sequence
        FROM messages AS m
        LEFT JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=?
          AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(NULLIF(TRIM(m.role), ''), m.sender_type, u.role)='student'
          AND m.sequence IS NOT NULL
        """,
        (row["group_id"], row["session_id"], row["discussion_id"]),
    ).fetchone()
    return int(result["latest_sequence"]) if result and result["latest_sequence"] is not None else None


def _latest_agent_message(conn, row):
    return conn.execute(
        """
        SELECT id, sequence, created_at, agent_type, content
        FROM messages
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(NULLIF(TRIM(role), ''), sender_type)='agent'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (row["group_id"], row["session_id"], row["discussion_id"]),
    ).fetchone()


def _recent_agent_messages(conn, row, limit: int = 3) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, sequence, content, created_at, role, user_id
        FROM messages
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(NULLIF(TRIM(role), ''), sender_type)='agent'
        ORDER BY sequence DESC, id DESC
        LIMIT ?
        """,
        (row["group_id"], row["session_id"], row["discussion_id"], limit),
    ).fetchall()
    return [
        {"message_id": item["id"], "sequence": item["sequence"], "content": item["content"]}
        for item in reversed(rows)
    ]


def _latest_strategy_publish(conn, row):
    return conn.execute(
        """
        SELECT id, published_message_id, published_at
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND id<>?
          AND publish_status='PUBLISHED'
        ORDER BY COALESCE(published_at, updated_at, created_at) DESC, id DESC
        LIMIT 1
        """,
        (row["group_id"], row["session_id"], row["discussion_id"], row["id"]),
    ).fetchone()


def _message_sequence(conn, message_id: Any) -> Optional[int]:
    if not message_id:
        return None
    row = conn.execute("SELECT sequence FROM messages WHERE id=?", (message_id,)).fetchone()
    return int(row["sequence"]) if row and row["sequence"] is not None else None


def _json_int_list(value: Any) -> list[int]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    result = []
    for item in parsed or []:
        if isinstance(item, bool):
            continue
        try:
            parsed_item = int(item)
        except (TypeError, ValueError):
            continue
        if parsed_item not in result:
            result.append(parsed_item)
    return result


def _json_string_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    result = []
    for item in parsed or []:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _seconds_since(value: Any) -> Optional[float]:
    parsed = _parse_dt(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now() - parsed).total_seconds())


def _looks_like_multiple_independent_actions(value: str) -> bool:
    if "；" in value or ";" in value:
        return True
    action_hits = sum(1 for term in _ACTION_TERMS if term in value)
    return action_hits >= 5 and value.count("，") >= 3


def _default_final_status(reason: str) -> str:
    text = str(reason or "").upper()
    if "SUPERSEDED" in text or "HIGHER_PRIORITY" in text:
        return "SUPERSEDED"
    if "STALE" in text or "NEW_STUDENT_MESSAGE" in text:
        return "STALE"
    if "VALIDATION" in text or "FAILED" in text or "LOCK" in text:
        return "FAILED"
    return "SUPPRESSED"


_PREFLIGHT_COOLDOWN_REASONS = frozenset(
    {
        "strategy_cooldown",
        "no_student_feedback_after_last_intervention",
        "strategy_intervention_cap_reached",
    }
)


def _preflight_terminal_reason(reason: Any) -> str:
    raw = str(reason or "preflight_blocked").strip()
    if raw in _PREFLIGHT_COOLDOWN_REASONS:
        return "SUPPRESSED_COOLDOWN"
    category = normalize_publish_runtime_reason(raw)
    if category == "cooldown_active":
        return "SUPPRESSED_COOLDOWN"
    if category == "session_closed":
        return "SUPPRESSED_SESSION_CLOSED"
    if category == "discussion_closed":
        return "SUPPRESSED_DISCUSSION_CLOSED"
    if category == "agent_disabled":
        return "SUPPRESSED_AGENT_DISABLED"
    if category == "already_published":
        return "ALREADY_PUBLISHED"
    return raw


def _with_preflight_fields(result: dict) -> dict:
    result = dict(result or {})
    allowed = bool(result.get("allowed"))
    raw_reason = str(result.get("reason") or ("allowed" if allowed else "preflight_blocked"))
    return {
        **result,
        "preflight_gate_result": "allowed" if allowed else "blocked",
        "preflight_gate_reason": raw_reason,
        "preflight_terminal_reason": None if allowed else _preflight_terminal_reason(raw_reason),
        "stage3_skipped_by_preflight": not allowed,
        "lock_skipped_by_preflight": not allowed,
        "llm_call_saved": not allowed,
    }


def _blocked(reason: str, **fields: Any) -> dict:
    return {
        "allowed": False,
        "reason": reason,
        "failure_category": normalize_publish_runtime_reason(reason),
        **fields,
    }


def _invalid_text(code: str) -> dict:
    return {"passed": False, "failure_code": code}


__all__ = [
    "InterventionDecisionGate",
    "StudentFacingInterventionValidator",
    "ThreeStageInterventionPublisher",
]
