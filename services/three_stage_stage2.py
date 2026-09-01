# -*- coding: utf-8 -*-
"""Stage 2 persistence for precise sub-state assessment results."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
import json
import uuid

from db import db, now_str
from services.three_stage_schema import (
    OI_STRATEGY_IDS,
    STAGE2_SCHEMA_VERSION,
    dumps_json,
    normalize_canonical_sub_state,
)
from services.state_strategy_router import StateStrategyRouter
from services.three_stage_strategy_library import sync_strategy_definitions
from services.three_stage_coordination import (
    finalize_preliminary_runs_for_batch_row,
    link_pipeline_replacement,
    record_replacement_request,
    sync_latest_state_from_replacement,
    supersede_preliminary_runs_for_batch_row,
)
from services.three_stage_latency import (
    duration_ms,
    latency_timestamp,
    record_latency_event,
)
from services.three_stage_route_manifest import OPTIONAL_SUPPORT


def is_stage2_result(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("schema_version") == STAGE2_SCHEMA_VERSION


class Stage2PipelineService:
    """Persist precise Stage 2 state facts into three-stage audit rows."""

    @staticmethod
    def prepare_for_batch(batch: dict, *, pipeline_mode: str = "strategy") -> dict:
        """Resolve an authoritative row without declaring Stage 2 started."""
        if pipeline_mode not in {"strategy", "state_only"}:
            raise ValueError("invalid_pipeline_mode")
        batch = {**batch, "pipeline_mode": pipeline_mode}
        timestamp = latency_timestamp()
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pipeline = _get_or_create_pipeline_row(conn, batch, timestamp)
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                SET pipeline_mode=?,
                    assessment_batch_id=COALESCE(assessment_batch_id, ?),
                    assessment_owner_pipeline_run_id=COALESCE(
                        assessment_owner_pipeline_run_id, id
                    ),
                    stage2_status=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN stage2_status
                        ELSE 'PENDING'
                    END,
                    stage2_started_at=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN stage2_started_at
                        ELSE NULL
                    END,
                    stage2_completed_at=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN stage2_completed_at
                        ELSE NULL
                    END,
                    publish_status=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN publish_status
                        ELSE 'NOT_READY'
                    END,
                    final_status=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN final_status
                        ELSE 'PENDING_STAGE2'
                    END,
                    skip_reason=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN skip_reason
                        ELSE NULL
                    END,
                    failure_code=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN failure_code
                        ELSE NULL
                    END,
                    failure_detail=CASE
                        WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                            THEN failure_detail
                        ELSE NULL
                    END,
                    updated_at=?
                WHERE id=?
                """,
                (pipeline_mode, batch["id"], timestamp, pipeline["id"]),
            )
            pipeline = conn.execute(
                "SELECT * FROM strategy_pipeline_runs WHERE id=?",
                (pipeline["id"],),
            ).fetchone()
            conn.commit()
            return {
                "prepared": True,
                "pipeline_run_id": int(pipeline["id"]),
                "pipeline_mode": pipeline["pipeline_mode"],
                "coarse_should_escalate": bool(
                    pipeline["coarse_should_escalate"]
                ),
                "room_lock_token": pipeline["room_lock_token"],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def mark_started(*, batch: dict, pipeline_run_id: int) -> dict:
        """Move the prepared row to Stage 2 only after its lease decision."""
        timestamp = latency_timestamp()
        pipeline_id = int(pipeline_run_id)
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET stage2_status='RUNNING',
                       stage2_started_at=?,
                       stage2_completed_at=NULL,
                       final_status='ASSESSING',
                       skip_reason=NULL,
                       failure_code=NULL,
                       failure_detail=NULL,
                       updated_at=?
                 WHERE id=? AND assessment_batch_id=?
                   AND UPPER(COALESCE(stage2_status, 'PENDING'))='PENDING'
                   AND UPPER(COALESCE(final_status, 'PENDING_STAGE2'))
                       IN ('PENDING_STAGE2', 'WAITING_FOR_LOCK', 'LOCKED')
                """,
                (timestamp, timestamp, pipeline_id, int(batch["id"])),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {
                    "started": False,
                    "reason": "pipeline_not_stage2_startable",
                    "pipeline_run_id": pipeline_id,
                }
            record_latency_event(
                stage="stage2",
                event="stage2_started",
                pipeline_run_id=pipeline_id,
                assessment_batch_id=batch.get("id"),
                occurred_at=timestamp,
                conn=conn,
                pipeline_context=True,
            )
            conn.commit()
            return {
                "started": True,
                "pipeline_run_id": pipeline_id,
                "stage2_started_at": timestamp,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def mark_waiting_for_lock(
        *, batch: dict, pipeline_run_id: int, reason: str
    ) -> dict:
        """Audit a pre-Stage-2 lease miss without pretending the LLM ran."""
        timestamp = latency_timestamp()
        pipeline_id = int(pipeline_run_id)
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET stage2_status='PENDING',
                       final_status='WAITING_FOR_LOCK',
                       skip_reason=?,
                       updated_at=?
                 WHERE id=? AND assessment_batch_id=?
                   AND UPPER(COALESCE(stage2_status, 'PENDING'))='PENDING'
                """,
                (str(reason or "ROOM_LOCK_UNAVAILABLE"), timestamp,
                 pipeline_id, int(batch["id"])),
            )
            conn.commit()
            return {
                "updated": cur.rowcount == 1,
                "pipeline_run_id": pipeline_id,
                "final_status": "WAITING_FOR_LOCK",
                "reason": str(reason or "ROOM_LOCK_UNAVAILABLE"),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def finalize_terminal_batch_siblings(batch_id: int) -> dict:
        """Close deterministic preliminary siblings for one terminal batch."""
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_row = conn.execute(
                "SELECT * FROM state_assessment_batches WHERE id=?",
                (int(batch_id),),
            ).fetchone()
            if not batch_row:
                conn.rollback()
                return {
                    "finalized": False,
                    "reason": "assessment_batch_not_found",
                    "assessment_batch_id": int(batch_id),
                    "pipeline_run_ids": [],
                }
            batch = dict(batch_row)
            if batch.get("status") == "succeeded":
                outcome = "succeeded"
                owner_stage2_status = "SUCCEEDED"
            elif batch.get("terminal_status") in {"degraded", "quarantined"}:
                outcome = "failed"
                owner_stage2_status = "FAILED"
            else:
                conn.commit()
                return {
                    "finalized": False,
                    "reason": "assessment_batch_not_terminal",
                    "assessment_batch_id": int(batch_id),
                    "pipeline_run_ids": [],
                }

            owner = conn.execute(
                """
                SELECT *
                FROM strategy_pipeline_runs
                WHERE assessment_batch_id=?
                  AND UPPER(COALESCE(stage2_status, ''))=?
                ORDER BY
                  CASE
                    WHEN input_cutoff_student_sequence=? THEN 0
                    ELSE 1
                  END,
                  id DESC
                LIMIT 1
                """,
                (
                    int(batch["id"]),
                    owner_stage2_status,
                    int(batch["candidate_end_sequence"]),
                ),
            ).fetchone()
            if not owner:
                conn.commit()
                return {
                    "finalized": False,
                    "reason": "authoritative_pipeline_not_terminal",
                    "assessment_batch_id": int(batch_id),
                    "pipeline_run_ids": [],
                }

            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET assessment_owner_pipeline_run_id=COALESCE(
                       assessment_owner_pipeline_run_id, id
                   )
                 WHERE id=?
                """,
                (int(owner["id"]),),
            )
            finalized_ids = finalize_preliminary_runs_for_batch_row(
                conn,
                owner,
                start_sequence=int(batch["candidate_start_sequence"]),
                end_sequence=int(batch["candidate_end_sequence"]),
                assessment_batch_id=int(batch["id"]),
                outcome=outcome,
                failure_code=batch.get("error_code"),
                failure_detail=(
                    str(batch.get("error_detail"))[:500]
                    if batch.get("error_detail")
                    else None
                ),
            )
            conn.commit()
            return {
                "finalized": bool(finalized_ids),
                "reason": (
                    "terminal_batch_siblings_finalized"
                    if finalized_ids
                    else "no_deterministic_orphans"
                ),
                "assessment_batch_id": int(batch["id"]),
                "authoritative_pipeline_run_id": int(owner["id"]),
                "outcome": outcome,
                "window_start_sequence": int(batch["candidate_start_sequence"]),
                "window_end_sequence": int(batch["candidate_end_sequence"]),
                "pipeline_run_ids": finalized_ids,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def recover_terminal_batch_orphans(
        *,
        group_id: int,
        session_id: int,
        discussion_id: int,
    ) -> list[dict]:
        """Idempotently recover only orphans covered by terminal batches."""
        conn = db()
        try:
            rows = conn.execute(
                """
                SELECT id
                FROM state_assessment_batches
                WHERE group_id=?
                  AND COALESCE(session_id, 0)=COALESCE(?, 0)
                  AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
                  AND (
                    status='succeeded'
                    OR terminal_status IN ('degraded','quarantined')
                  )
                ORDER BY candidate_end_sequence, id
                """,
                (int(group_id), int(session_id), int(discussion_id)),
            ).fetchall()
        finally:
            conn.close()
        return [
            Stage2PipelineService.finalize_terminal_batch_siblings(int(row["id"]))
            for row in rows
        ]

    @staticmethod
    def persist_success(
        *,
        batch: dict,
        stage2_result: dict,
        llm_meta: dict = None,
        saved_segments: list[dict] = None,
        monitor_run_id: int = None,
        suppress_intervention: bool = False,
    ) -> dict:
        if not is_stage2_result(stage2_result):
            return {"updated": False, "reason": "not_stage2_result"}

        timestamp = latency_timestamp()
        llm_meta = dict(llm_meta or {})
        active = dict(stage2_result.get("active_sub_state") or {})
        canonical = normalize_canonical_sub_state(active.get("canonical_sub_state"))
        state_route = StateStrategyRouter().route(
            canonical,
            secondary_tags=active.get("secondary_tags") or [],
        )
        route = state_route.to_legacy_route_payload()
        requested_should_intervene = bool(stage2_result.get("should_intervene"))
        inhibition = dict(stage2_result.get("inhibition") or {})
        route_inhibition_strategy_id = route.get("inhibition_strategy_id")
        route_mode = str(route.get("route_mode") or "").strip()
        # The deterministic canonical route is the only authority for OI.
        # Preserve any conflicting model value in state_raw_response_json, but
        # never promote it into the publish gate or suppression audit.
        inhibition_strategy_id = route_inhibition_strategy_id
        candidate_strategy_ids = list(route.get("candidate_strategy_ids") or [])
        evidence_ids = _int_list(active.get("evidence_message_ids"))
        segment_payloads = _segments_with_db_ids(stage2_result, saved_segments or [])
        latest_message = _latest_student_message(batch)
        latest_sequence = (
            int(latest_message["sequence"])
            if latest_message and latest_message["sequence"] is not None
            else None
        )
        latest_message_id = (
            int(latest_message["id"])
            if latest_message and latest_message["id"] is not None
            else None
        )
        cutoff = int(batch.get("candidate_end_sequence") or 0)
        stale = latest_sequence is not None and latest_sequence > cutoff
        active_segment_id = _active_segment_id(
            active,
            saved_segments or [],
        )
        evidence_audit = _audit_active_evidence(
            batch=batch,
            active=active,
            canonical=canonical,
            evidence_sequences=evidence_ids,
        )
        raw_detected_self_regulation = bool(
            active.get("detected_self_regulation")
        )
        fresh_detected_self_regulation = bool(
            raw_detected_self_regulation and evidence_audit["valid"]
        )
        effective_oi_strategy_id = (
            route_inhibition_strategy_id
            if route_inhibition_strategy_id in OI_STRATEGY_IDS
            and evidence_audit["valid"]
            else None
        )
        invalid_suppression_evidence = bool(
            not evidence_audit["valid"]
            and (
                raw_detected_self_regulation
                or route_inhibition_strategy_id in OI_STRATEGY_IDS
            )
        )
        suppression_type = None
        suppression_strategy_id = None
        suppression_reason = None
        failure_code = None
        failure_detail = None
        should_intervene = state_route.should_intervene
        if route_mode == OPTIONAL_SUPPORT:
            should_intervene = requested_should_intervene
        optional_support_gate = None
        if route_mode == OPTIONAL_SUPPORT and should_intervene:
            optional_support_gate = _optional_support_gate(
                batch,
                candidate_strategy_ids=candidate_strategy_ids,
                timestamp=timestamp,
            )
            if not optional_support_gate["allowed"]:
                should_intervene = False

        if stale:
            final_status = "STALE"
            publish_status = "NOT_READY"
            stage3_status = "SKIPPED"
            skip_reason = "STALE_NEW_STUDENT_MESSAGE"
            should_enter_stage3 = False
            release_lock = True
        elif invalid_suppression_evidence:
            final_status = "FAILED"
            publish_status = "NOT_READY"
            stage3_status = "SKIPPED"
            skip_reason = "INVALID_SUPPRESSION_EVIDENCE"
            failure_code = "INVALID_SUPPRESSION_EVIDENCE"
            failure_detail = evidence_audit["reason"]
            should_intervene = False
            should_enter_stage3 = False
            release_lock = True
        elif effective_oi_strategy_id:
            suppression_type = "OI"
            suppression_strategy_id = effective_oi_strategy_id
            suppression_reason = f"OI_SUPPRESSED:{effective_oi_strategy_id}"
            final_status = "SUPPRESSED"
            publish_status = "SUPPRESSED"
            stage3_status = "SKIPPED"
            skip_reason = suppression_reason
            should_intervene = False
            should_enter_stage3 = False
            release_lock = True
            candidate_strategy_ids = []
        elif fresh_detected_self_regulation:
            suppression_type = "SELF_REGULATION"
            suppression_reason = "SELF_REGULATION_SUPPRESSED"
            final_status = "SUPPRESSED"
            publish_status = "SUPPRESSED"
            stage3_status = "SKIPPED"
            skip_reason = suppression_reason
            should_intervene = False
            should_enter_stage3 = False
            release_lock = True
            candidate_strategy_ids = []
        elif suppress_intervention:
            final_status = "SUPPRESSED"
            publish_status = "SUPPRESSED"
            stage3_status = "SKIPPED"
            skip_reason = "STATE_ONLY_REPLAY"
            should_enter_stage3 = False
            release_lock = True
        elif should_intervene:
            final_status = "PENDING_STAGE3"
            publish_status = "NOT_READY"
            stage3_status = "PENDING"
            skip_reason = None
            should_enter_stage3 = True
            release_lock = False
        elif route_mode == OPTIONAL_SUPPORT and optional_support_gate:
            final_status = "SUPPRESSED"
            publish_status = "SUPPRESSED"
            stage3_status = "SKIPPED"
            skip_reason = (
                "OPTIONAL_SUPPORT_GATE_CLOSED:"
                + str(optional_support_gate.get("reason") or "not_allowed")
            )
            should_enter_stage3 = False
            release_lock = True
        else:
            final_status = "SUPPRESSED"
            publish_status = "SUPPRESSED"
            stage3_status = "SKIPPED"
            skip_reason = "STAGE2_NO_INTERVENTION"
            should_enter_stage3 = False
            release_lock = True

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pipeline = _get_or_create_pipeline_row(conn, batch, timestamp)
            library_meta = sync_strategy_definitions(conn)
            pipeline_id = int(pipeline["id"])
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                SET stage2_status='SUCCEEDED',
                    stage2_started_at=COALESCE(stage2_started_at, ?),
                    stage2_completed_at=?,
                    raw_sub_state_code=?,
                    canonical_sub_state_code=?,
                    trigger_level_state=?,
                    latest_state=?,
                    latest_should_intervene=?,
                    latest_state_pipeline_run_id=?,
                    sub_category=?,
                    secondary_sub_state_tags_json=?,
                    sub_state_confidence=?,
                    sub_state_reason=?,
                    sub_state_start_sequence=?,
                    sub_state_end_sequence=?,
                    sub_state_evidence_message_ids_json=?,
                    evidence_message_ids_json=?,
                    all_state_segments_json=?,
                    detected_self_regulation=?,
                    fresh_detected_self_regulation=?,
                    should_intervene=?,
                    inhibition_strategy_id=?,
                    inhibition_reason=?,
                    suppression_type=?,
                    suppression_strategy_id=?,
                    suppression_evidence_message_ids_json=?,
                    suppression_source_batch_id=?,
                    suppression_source_segment_id=?,
                    suppression_decision_reason=?,
                    suppression_decision_at=?,
                    state_model_name=?,
                    state_model_version=?,
                    state_prompt_version=?,
                    state_raw_response_json=?,
                    stage3_status=?,
                    strategy_candidate_ids_json=?,
                    strategy_pool_json=?,
                    strategy_source=?,
                    strategy_library_version=?,
                    strategy_library_hash=?,
                    publish_status=?,
                    final_status=?,
                    skip_reason=?,
                    failure_code=?,
                    failure_detail=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    batch.get("started_at") or timestamp,
                    timestamp,
                    active.get("raw_sub_state"),
                    canonical,
                    canonical,
                    None if stale else canonical,
                    None if stale else (1 if should_intervene else 0),
                    None if stale else pipeline_id,
                    state_route.sub_category,
                    dumps_json(active.get("secondary_tags") or []),
                    _float_or_none(active.get("confidence")),
                    stage2_result.get("decision_reason") or active.get("reason"),
                    _int_or_none(active.get("start_sequence")),
                    _int_or_none(active.get("end_sequence")),
                    dumps_json(evidence_ids),
                    dumps_json(evidence_ids),
                    dumps_json(segment_payloads),
                    1 if fresh_detected_self_regulation else 0,
                    1 if fresh_detected_self_regulation else 0,
                    1 if should_intervene else 0,
                    inhibition_strategy_id,
                    inhibition.get("reason") or suppression_reason,
                    suppression_type,
                    suppression_strategy_id,
                    dumps_json(
                        evidence_audit["message_ids"]
                        if suppression_type
                        else []
                    ),
                    batch.get("id") if suppression_type else None,
                    active_segment_id if suppression_type else None,
                    suppression_reason,
                    timestamp if suppression_type else None,
                    llm_meta.get("model_name"),
                    llm_meta.get("model_version"),
                    llm_meta.get("prompt_version"),
                    llm_meta.get("raw_response")
                    or dumps_json(stage2_result),
                    stage3_status,
                    dumps_json(candidate_strategy_ids),
                    dumps_json(list(state_route.strategy_pool)),
                    state_route.strategy_source,
                    library_meta["version"],
                    library_meta["library_hash"],
                    publish_status,
                    final_status,
                    skip_reason,
                    failure_code,
                    failure_detail,
                    timestamp,
                    pipeline_id,
                ),
            )
            _update_segment_three_stage_fields(
                conn,
                pipeline_id=pipeline_id,
                coarse_state_code=(
                    pipeline["coarse_state_code"]
                    if "coarse_state_code" in pipeline.keys()
                    else None
                ),
                saved_segments=saved_segments or [],
                strategy_library_version=library_meta["version"],
                active_segment_id=active_segment_id,
                active_should_intervene=should_intervene,
            )
            superseded_pipeline_ids = supersede_preliminary_runs_for_batch_row(
                conn,
                pipeline,
                start_sequence=int(batch.get("candidate_start_sequence") or cutoff),
                end_sequence=cutoff,
            )
            for superseded_pipeline_id in superseded_pipeline_ids:
                sync_latest_state_from_replacement(
                    conn,
                    superseded_pipeline_id,
                    pipeline_id,
                )
            replacement_request = None
            if batch.get("replacement_of_pipeline_run_id"):
                link_pipeline_replacement(
                    conn,
                    int(batch["replacement_of_pipeline_run_id"]),
                    pipeline_id,
                    reason=batch.get("replacement_reason"),
                    trigger_message_id=batch.get("replacement_trigger_message_id"),
                    cutoff_sequence=batch.get("replacement_cutoff_sequence"),
                )
                sync_latest_state_from_replacement(
                    conn,
                    int(batch["replacement_of_pipeline_run_id"]),
                    pipeline_id,
                )
            if stale:
                replacement_request = record_replacement_request(
                    conn,
                    pipeline_id,
                    reason="STALE_NEW_STUDENT_MESSAGE",
                    trigger_message_id=latest_message_id,
                    cutoff_sequence=latest_sequence,
                )
            released = False
            if release_lock:
                released = _release_pipeline_lock(conn, pipeline, timestamp)
            record_latency_event(
                stage="stage2",
                event="stage2_finished",
                pipeline_run_id=pipeline_id,
                assessment_batch_id=batch.get("id"),
                occurred_at=timestamp,
                elapsed=duration_ms(pipeline["stage2_started_at"], timestamp),
                details={"success": True, "terminal_status": final_status},
                conn=conn,
                pipeline_context=True,
            )
            conn.commit()
            return {
                "updated": True,
                "pipeline_run_id": pipeline_id,
                "should_intervene": should_intervene,
                "should_enter_stage3": should_enter_stage3,
                "final_status": final_status,
                "skip_reason": skip_reason,
                "stale": stale,
                "latest_student_sequence": latest_sequence,
                "latest_student_message_id": latest_message_id,
                "trigger_level_state": canonical,
                "latest_state": None if stale else canonical,
                "latest_should_intervene": None if stale else should_intervene,
                "replacement_request": replacement_request,
                "fresh_detected_self_regulation": fresh_detected_self_regulation,
                "optional_support_gate": optional_support_gate,
                "suppression_type": suppression_type,
                "suppression_strategy_id": suppression_strategy_id,
                "suppression_source_segment_id": active_segment_id,
                "lock_released": released,
                "superseded_pipeline_ids": superseded_pipeline_ids,
                "monitor_run_id": monitor_run_id,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def persist_state_only_success(
        *,
        batch: dict,
        stage2_result: dict,
        llm_meta: dict = None,
        saved_segments: list[dict] = None,
        monitor_run_id: int = None,
    ) -> dict:
        """Persist canonical state facts without performing strategy routing."""
        if not is_stage2_result(stage2_result):
            return {"updated": False, "reason": "not_stage2_result"}

        timestamp = latency_timestamp()
        llm_meta = dict(llm_meta or {})
        batch = {**batch, "pipeline_mode": "state_only"}
        active = dict(stage2_result.get("active_sub_state") or {})
        canonical = normalize_canonical_sub_state(active.get("canonical_sub_state"))
        evidence_ids = _int_list(active.get("evidence_message_ids"))
        segment_payloads = _segments_with_db_ids(
            stage2_result, saved_segments or []
        )
        active_segment_id = _active_segment_id(active, saved_segments or [])

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pipeline = _get_or_create_pipeline_row(conn, batch, timestamp)
            pipeline_id = int(pipeline["id"])
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                SET pipeline_mode='state_only',
                    stage2_status='SUCCEEDED',
                    stage2_started_at=COALESCE(stage2_started_at, ?),
                    stage2_completed_at=?,
                    raw_sub_state_code=?,
                    canonical_sub_state_code=?,
                    trigger_level_state=?,
                    latest_state=?,
                    latest_should_intervene=NULL,
                    latest_state_pipeline_run_id=?,
                    sub_category=?,
                    secondary_sub_state_tags_json=?,
                    sub_state_confidence=?,
                    sub_state_reason=?,
                    sub_state_start_sequence=?,
                    sub_state_end_sequence=?,
                    sub_state_evidence_message_ids_json=?,
                    evidence_message_ids_json=?,
                    all_state_segments_json=?,
                    detected_self_regulation=?,
                    fresh_detected_self_regulation=?,
                    should_intervene=NULL,
                    inhibition_strategy_id=NULL,
                    inhibition_reason=NULL,
                    suppression_type=NULL,
                    suppression_strategy_id=NULL,
                    suppression_evidence_message_ids_json='[]',
                    suppression_source_batch_id=NULL,
                    suppression_source_segment_id=NULL,
                    suppression_decision_reason=NULL,
                    suppression_decision_at=NULL,
                    state_model_name=?,
                    state_model_version=?,
                    state_prompt_version=?,
                    state_raw_response_json=?,
                    stage3_status='SKIPPED',
                    strategy_candidate_ids_json='[]',
                    strategy_pool_json='[]',
                    selected_strategy_id=NULL,
                    selected_strategy_name=NULL,
                    selected_strategy_type=NULL,
                    selected_strategy_json=NULL,
                    supporting_strategy_ids_json='[]',
                    strategy_selection_reason=NULL,
                    strategy_application_plan=NULL,
                    strategy_library_version=NULL,
                    strategy_library_hash=NULL,
                    strategy_source=NULL,
                    publish_status='SKIPPED',
                    final_status='STATE_ONLY_COMPLETED',
                    skip_reason='STATE_ONLY_COMPLETE',
                    failure_code=NULL,
                    failure_detail=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (
                    batch.get("started_at") or timestamp,
                    timestamp,
                    active.get("raw_sub_state"),
                    canonical,
                    canonical,
                    canonical,
                    pipeline_id,
                    canonical,
                    dumps_json(active.get("secondary_tags") or []),
                    _float_or_none(active.get("confidence")),
                    stage2_result.get("decision_reason") or active.get("reason"),
                    _int_or_none(active.get("start_sequence")),
                    _int_or_none(active.get("end_sequence")),
                    dumps_json(evidence_ids),
                    dumps_json(evidence_ids),
                    dumps_json(segment_payloads),
                    1 if active.get("detected_self_regulation") else 0,
                    1 if active.get("detected_self_regulation") else 0,
                    llm_meta.get("model_name"),
                    llm_meta.get("model_version"),
                    llm_meta.get("prompt_version"),
                    llm_meta.get("raw_response") or dumps_json(stage2_result),
                    timestamp,
                    pipeline_id,
                ),
            )
            conn.execute(
                """
                UPDATE collaboration_state_segments
                SET strategy_pipeline_run_id=?,
                    source_stage='stage2',
                    should_intervene=NULL,
                    selected_strategy_id=NULL,
                    strategy_library_version=NULL,
                    updated_at=?
                WHERE assessment_batch_id=?
                """,
                (pipeline_id, timestamp, int(batch["id"])),
            )
            superseded_pipeline_ids = supersede_preliminary_runs_for_batch_row(
                conn,
                pipeline,
                start_sequence=int(batch.get("candidate_start_sequence") or 0),
                end_sequence=int(batch.get("candidate_end_sequence") or 0),
            )
            for superseded_pipeline_id in superseded_pipeline_ids:
                sync_latest_state_from_replacement(
                    conn, superseded_pipeline_id, pipeline_id
                )
            if batch.get("replacement_of_pipeline_run_id"):
                link_pipeline_replacement(
                    conn,
                    int(batch["replacement_of_pipeline_run_id"]),
                    pipeline_id,
                    reason=batch.get("replacement_reason"),
                    trigger_message_id=batch.get("replacement_trigger_message_id"),
                    cutoff_sequence=batch.get("replacement_cutoff_sequence"),
                )
                sync_latest_state_from_replacement(
                    conn,
                    int(batch["replacement_of_pipeline_run_id"]),
                    pipeline_id,
                )
            released = _release_pipeline_lock(conn, pipeline, timestamp)
            record_latency_event(
                stage="stage2",
                event="stage2_finished",
                pipeline_run_id=pipeline_id,
                assessment_batch_id=batch.get("id"),
                occurred_at=timestamp,
                elapsed=duration_ms(pipeline["stage2_started_at"], timestamp),
                details={
                    "success": True,
                    "terminal_status": "STATE_ONLY_COMPLETED",
                    "pipeline_mode": "state_only",
                },
                conn=conn,
                pipeline_context=True,
            )
            conn.commit()
            return {
                "updated": True,
                "pipeline_run_id": pipeline_id,
                "pipeline_mode": "state_only",
                "canonical_sub_state_code": canonical,
                "state_overlays": list(active.get("secondary_tags") or []),
                "confidence": _float_or_none(active.get("confidence")),
                "evidence_message_ids": evidence_ids,
                "message_range": [
                    _int_or_none(active.get("start_sequence")),
                    _int_or_none(active.get("end_sequence")),
                ],
                "assessment_batch_id": int(batch["id"]),
                "should_intervene": None,
                "should_enter_stage3": False,
                "final_status": "STATE_ONLY_COMPLETED",
                "published": False,
                "lock_released": released,
                "superseded_pipeline_ids": superseded_pipeline_ids,
                "monitor_run_id": monitor_run_id,
                "active_segment_id": active_segment_id,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def mark_failed(
        *,
        batch: dict,
        error_code: str,
        error_detail: str = None,
        llm_meta: dict = None,
        stage2_started: bool = True,
    ) -> dict:
        timestamp = latency_timestamp()
        llm_meta = dict(llm_meta or {})
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pipeline = _get_or_create_pipeline_row(conn, batch, timestamp)
            pipeline_id = int(pipeline["id"])
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                SET stage2_status='FAILED',
                    stage2_started_at=CASE
                        WHEN ?=1 THEN COALESCE(stage2_started_at, ?)
                        ELSE stage2_started_at
                    END,
                    stage2_completed_at=?,
                    stage3_status='SKIPPED',
                    publish_status='NOT_READY',
                    final_status='FAILED',
                    skip_reason='STAGE2_FAILED',
                    failure_code=?,
                    failure_detail=?,
                    state_model_name=?,
                    state_model_version=?,
                    state_prompt_version=?,
                    state_raw_response_json=COALESCE(?, state_raw_response_json),
                    should_intervene=0,
                    updated_at=?
                WHERE id=?
                """,
                (
                    1 if stage2_started else 0,
                    batch.get("started_at") or timestamp,
                    timestamp,
                    str(error_code or "stage2_failed"),
                    error_detail,
                    llm_meta.get("model_name"),
                    llm_meta.get("model_version"),
                    llm_meta.get("prompt_version"),
                    llm_meta.get("raw_response"),
                    timestamp,
                    pipeline_id,
                ),
            )
            released = _release_pipeline_lock(conn, pipeline, timestamp)
            if stage2_started or pipeline["stage2_started_at"]:
                record_latency_event(
                    stage="stage2",
                    event="stage2_finished",
                    pipeline_run_id=pipeline_id,
                    assessment_batch_id=batch.get("id"),
                    occurred_at=timestamp,
                    elapsed=duration_ms(pipeline["stage2_started_at"], timestamp),
                    details={"success": False, "failure_type": error_code},
                    conn=conn,
                    pipeline_context=True,
                )
            conn.commit()
            return {
                "updated": True,
                "pipeline_run_id": pipeline_id,
                "final_status": "FAILED",
                "lock_released": released,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _get_or_create_pipeline_row(conn, batch: dict, timestamp: str):
    pipeline_mode = str(batch.get("pipeline_mode") or "strategy")
    if pipeline_mode not in {"strategy", "state_only"}:
        raise ValueError("invalid_pipeline_mode")
    row = conn.execute(
        """
        SELECT *
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND input_cutoff_student_sequence=?
          AND (assessment_batch_id=? OR assessment_batch_id IS NULL)
          AND UPPER(COALESCE(publish_status, ''))<>'PUBLISHED'
        ORDER BY
          CASE WHEN assessment_batch_id=? THEN 0 ELSE 1 END,
          CASE WHEN stage1_status='SUCCEEDED' THEN 0 ELSE 1 END,
          id DESC
        LIMIT 1
        """,
        (
            batch["group_id"],
            batch.get("session_id"),
            batch.get("discussion_id"),
            batch.get("candidate_end_sequence"),
            batch.get("id"),
            batch.get("id"),
        ),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE strategy_pipeline_runs
            SET pipeline_mode=?,
                assessment_batch_id=COALESCE(assessment_batch_id, ?),
                parent_run_id=COALESCE(parent_run_id, ?),
                trigger_message_id=COALESCE(trigger_message_id, ?)
            WHERE id=?
            """,
            (
                pipeline_mode,
                batch.get("id"),
                batch.get("replacement_of_pipeline_run_id"),
                batch.get("replacement_trigger_message_id"),
                row["id"],
            ),
        )
        row = conn.execute(
            "SELECT * FROM strategy_pipeline_runs WHERE id=?", (row["id"],)
        ).fetchone()
        if batch.get("replacement_of_pipeline_run_id"):
            link_pipeline_replacement(
                conn,
                int(batch["replacement_of_pipeline_run_id"]),
                int(row["id"]),
                reason=batch.get("replacement_reason"),
                trigger_message_id=batch.get("replacement_trigger_message_id"),
                cutoff_sequence=batch.get("replacement_cutoff_sequence"),
            )
        return row

    run_uuid = str(uuid.uuid4())
    idempotency_key = (
        "stage2:"
        f"g={int(batch['group_id'])}:"
        f"sid={int(batch.get('session_id') or 0)}:"
        f"did={int(batch.get('discussion_id') or 0)}:"
        f"range={int(batch.get('candidate_start_sequence') or 0)}-"
        f"{int(batch.get('candidate_end_sequence') or 0)}:"
        f"trigger={batch.get('trigger_type') or 'state_assessment'}"
    )
    conn.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, pipeline_mode,
            group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority, trigger_message_id,
            assessment_batch_id,
            input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
            stage1_status, stage2_status, publish_status, final_status,
            parent_run_id,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_uuid,
            pipeline_mode,
            batch["group_id"],
            batch.get("session_id"),
            batch.get("session_no"),
            batch.get("discussion_id"),
            batch.get("task_id"),
            batch.get("trigger_type") or "state_assessment",
            batch.get("request_priority"),
            batch.get("replacement_trigger_message_id"),
            batch.get("id"),
            batch.get("candidate_start_sequence"),
            batch.get("candidate_end_sequence"),
            batch.get("candidate_end_sequence"),
            "NOT_RUN",
            "PENDING",
            "NOT_READY",
            "PENDING_STAGE2",
            batch.get("replacement_of_pipeline_run_id"),
            idempotency_key,
            timestamp,
            timestamp,
        ),
    )
    created = conn.execute(
        "SELECT * FROM strategy_pipeline_runs WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    record_latency_event(
        stage="pipeline",
        event="pipeline_created",
        pipeline_run_id=created["id"],
        assessment_batch_id=batch.get("id"),
        occurred_at=timestamp,
        conn=conn,
        pipeline_context=True,
    )
    if batch.get("replacement_of_pipeline_run_id"):
        link_pipeline_replacement(
            conn,
            int(batch["replacement_of_pipeline_run_id"]),
            int(created["id"]),
            reason=batch.get("replacement_reason"),
            trigger_message_id=batch.get("replacement_trigger_message_id"),
            cutoff_sequence=batch.get("replacement_cutoff_sequence"),
        )
    return created


def _latest_student_message(batch: dict):
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT m.id, m.sequence
            FROM messages AS m
            JOIN users AS u ON u.id=m.user_id
            WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
              AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
              AND m.sequence IS NOT NULL
            ORDER BY m.sequence DESC, m.id DESC
            LIMIT 1
            """,
            (batch["group_id"], batch.get("session_id"), batch.get("discussion_id")),
        ).fetchone()
        return row
    finally:
        conn.close()


def _latest_student_sequence(batch: dict) -> Optional[int]:
    row = _latest_student_message(batch)
    return int(row["sequence"]) if row and row["sequence"] is not None else None


def _optional_support_gate(
    batch: dict,
    *,
    candidate_strategy_ids: list[str],
    timestamp: str,
) -> dict:
    support_ids = tuple(
        strategy_id
        for strategy_id in candidate_strategy_ids
        if str(strategy_id or "").startswith(("SS-", "EA-"))
    )
    conn = db()
    try:
        latest_student = conn.execute(
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
            (batch["group_id"], batch.get("session_id"), batch.get("discussion_id")),
        ).fetchone()
        latest_student_sequence = (
            int(latest_student["latest_sequence"])
            if latest_student and latest_student["latest_sequence"] is not None
            else None
        )
        last_agent = conn.execute(
            """
            SELECT sequence, created_at
            FROM messages
            WHERE group_id=?
              AND COALESCE(session_id, 0)=COALESCE(?, 0)
              AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
              AND COALESCE(NULLIF(TRIM(role), ''), sender_type)='agent'
            ORDER BY sequence DESC, id DESC
            LIMIT 1
            """,
            (batch["group_id"], batch.get("session_id"), batch.get("discussion_id")),
        ).fetchone()
        seconds_since_agent = None
        if last_agent and last_agent["created_at"]:
            last_agent_time = _parse_dt(last_agent["created_at"])
            current_time = _parse_dt(timestamp) or datetime.now()
            if last_agent_time:
                seconds_since_agent = max(
                    0,
                    int((current_time - last_agent_time).total_seconds()),
                )
        support_count = 0
        if support_ids:
            placeholders = ",".join("?" for _ in support_ids)
            support_count_row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM strategy_pipeline_runs
                WHERE group_id=?
                  AND COALESCE(session_id, 0)=COALESCE(?, 0)
                  AND publish_status='PUBLISHED'
                  AND selected_strategy_id IN ({placeholders})
                """,
                (
                    batch["group_id"],
                    batch.get("session_id"),
                    *support_ids,
                ),
            ).fetchone()
            support_count = int(
                support_count_row["count"] if support_count_row else 0
            )
        if seconds_since_agent is not None and seconds_since_agent < 300:
            return {
                "allowed": False,
                "reason": "agent_spacing_lt_300s",
                "seconds_since_agent": seconds_since_agent,
                "positive_support_count": support_count,
                "latest_student_sequence": latest_student_sequence,
            }
        if support_count >= 2:
            return {
                "allowed": False,
                "reason": "positive_support_cap_reached",
                "seconds_since_agent": seconds_since_agent,
                "positive_support_count": support_count,
                "latest_student_sequence": latest_student_sequence,
            }
        if latest_student_sequence is None:
            return {
                "allowed": False,
                "reason": "no_recent_student_feedback",
                "seconds_since_agent": seconds_since_agent,
                "positive_support_count": support_count,
                "latest_student_sequence": latest_student_sequence,
            }
        if last_agent and last_agent["sequence"] is not None:
            try:
                if latest_student_sequence <= int(last_agent["sequence"]):
                    return {
                        "allowed": False,
                        "reason": "no_student_feedback_after_agent",
                        "seconds_since_agent": seconds_since_agent,
                        "positive_support_count": support_count,
                        "latest_student_sequence": latest_student_sequence,
                    }
            except (TypeError, ValueError):
                pass
        return {
            "allowed": True,
            "reason": "allowed",
            "seconds_since_agent": seconds_since_agent,
            "positive_support_count": support_count,
            "latest_student_sequence": latest_student_sequence,
        }
    finally:
        conn.close()


def _segments_with_db_ids(stage2_result: dict, saved_segments: list[dict]) -> list[dict]:
    ids_by_order = {
        int(row["segment_order"]): int(row["id"])
        for row in saved_segments or []
        if row.get("segment_order") is not None and row.get("id") is not None
    }
    payloads = []
    for index, segment in enumerate(stage2_result.get("segments") or []):
        item = {
            key: segment.get(key)
            for key in (
                "raw_sub_state",
                "canonical_sub_state",
                "secondary_tags",
                "start_sequence",
                "end_sequence",
                "confidence",
                "evidence_message_ids",
                "reason",
                "is_active_at_window_end",
                "detected_self_regulation",
            )
        }
        if index in ids_by_order:
            item["segment_id"] = ids_by_order[index]
        payloads.append(item)
    return payloads


def _active_segment_id(active: dict, saved_segments: list[dict]) -> Optional[int]:
    """Resolve the persisted segment that owns the current Stage 2 decision."""
    active_end = _int_or_none(active.get("end_sequence"))
    active_canonical = normalize_canonical_sub_state(
        active.get("canonical_sub_state")
    )
    for row in saved_segments:
        if not row.get("is_active_at_batch_end"):
            continue
        row_id = _int_or_none(row.get("id"))
        if row_id is not None:
            return row_id
    for row in saved_segments:
        row_id = _int_or_none(row.get("id"))
        row_end = _int_or_none(row.get("end_sequence"))
        row_canonical = normalize_canonical_sub_state(
            row.get("canonical_sub_state_code")
            or row.get("canonical_sub_state")
        )
        if (
            row_id is not None
            and row_end == active_end
            and row_canonical == active_canonical
        ):
            return row_id
    return None


def _audit_active_evidence(
    *,
    batch: dict,
    active: dict,
    canonical: str,
    evidence_sequences: list[int],
) -> dict:
    """Verify that suppression evidence belongs to the current student window."""
    if not evidence_sequences:
        return {"valid": False, "reason": "empty_evidence", "message_ids": []}

    candidate_start = _int_or_none(batch.get("candidate_start_sequence"))
    candidate_end = _int_or_none(batch.get("candidate_end_sequence"))
    cutoff = candidate_end
    active_start = _int_or_none(active.get("start_sequence"))
    active_end = _int_or_none(active.get("end_sequence"))
    active_canonical = normalize_canonical_sub_state(
        active.get("canonical_sub_state")
    )
    if None in (candidate_start, candidate_end, active_start, active_end):
        return {
            "valid": False,
            "reason": "missing_evidence_window",
            "message_ids": [],
        }
    if active_canonical != canonical or active_end != cutoff:
        return {
            "valid": False,
            "reason": "evidence_not_related_to_active_sub_state",
            "message_ids": [],
        }
    if any(
        sequence < candidate_start
        or sequence > candidate_end
        or sequence < active_start
        or sequence > active_end
        or sequence > cutoff
        for sequence in evidence_sequences
    ):
        return {
            "valid": False,
            "reason": "evidence_outside_current_candidate_window",
            "message_ids": [],
        }

    placeholders = ",".join("?" for _ in evidence_sequences)
    conn = db()
    try:
        rows = conn.execute(
            f"""
            SELECT m.id, m.sequence
            FROM messages AS m
            JOIN users AS u ON u.id=m.user_id
            WHERE m.group_id=?
              AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
              AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
              AND m.sequence IN ({placeholders})
              AND COALESCE(NULLIF(TRIM(m.role), ''), u.role)='student'
            ORDER BY m.sequence ASC
            """,
            (
                batch["group_id"],
                batch.get("session_id"),
                batch.get("discussion_id"),
                *evidence_sequences,
            ),
        ).fetchall()
    finally:
        conn.close()
    found_sequences = {int(row["sequence"]) for row in rows}
    if found_sequences != set(evidence_sequences):
        return {
            "valid": False,
            "reason": "evidence_missing_or_not_student_message",
            "message_ids": [],
        }
    return {
        "valid": True,
        "reason": "fresh_student_evidence",
        "message_ids": [int(row["id"]) for row in rows],
    }


def _update_segment_three_stage_fields(
    conn,
    *,
    pipeline_id: int,
    coarse_state_code: Optional[str],
    saved_segments: list[dict],
    strategy_library_version: Optional[str],
    active_segment_id: Optional[int],
    active_should_intervene: bool,
) -> None:
    for row in saved_segments:
        canonical = normalize_canonical_sub_state(row.get("canonical_sub_state_code"))
        secondary_tags = row.get("secondary_tags")
        if secondary_tags is None and row.get("secondary_tags_json"):
            try:
                secondary_tags = json.loads(row.get("secondary_tags_json") or "[]")
            except (TypeError, ValueError):
                secondary_tags = []
        route = StateStrategyRouter().route(
            canonical,
            secondary_tags=secondary_tags or [],
        ).to_legacy_route_payload()
        row_id = _int_or_none(row.get("id"))
        is_active = bool(row.get("is_active_at_batch_end")) or (
            active_segment_id is not None and row_id == active_segment_id
        )
        segment_should_intervene = (
            bool(active_should_intervene)
            if is_active
            else bool(route["should_intervene"])
        )
        conn.execute(
            """
            UPDATE collaboration_state_segments
            SET strategy_pipeline_run_id=?,
                coarse_state_code=COALESCE(coarse_state_code, ?),
                should_intervene=?,
                selected_strategy_id=?,
                strategy_library_version=?,
                source_stage='stage2',
                updated_at=?
            WHERE id=?
            """,
            (
                pipeline_id,
                coarse_state_code,
                1 if segment_should_intervene else 0,
                None,
                strategy_library_version,
                now_str(),
                row["id"],
            ),
        )


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _release_pipeline_lock(conn, pipeline, timestamp: str) -> bool:
    lock_token = pipeline["room_lock_token"] if "room_lock_token" in pipeline.keys() else None
    if not lock_token:
        return False
    owner_run_id = -int(pipeline["id"])
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
        (pipeline["group_id"], lock_token, owner_run_id),
    )
    released = cur.rowcount == 1
    if released:
        conn.execute(
            """
            UPDATE strategy_pipeline_runs
            SET room_lock_released_at=?, updated_at=?
            WHERE id=?
            """,
            (timestamp, timestamp, pipeline["id"]),
        )
        record_latency_event(
            stage="lock",
            event="room_lock_released",
            pipeline_run_id=pipeline["id"],
            assessment_batch_id=(
                pipeline["assessment_batch_id"]
                if "assessment_batch_id" in pipeline.keys()
                else None
            ),
            occurred_at=timestamp,
            lock_token=lock_token,
            details={
                "reason": "stage2_terminal",
                "lease_action": "release",
                "lease_released": True,
            },
            conn=conn,
            pipeline_context=True,
        )
    return released


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_list(values: Any) -> list[int]:
    result = []
    for value in values or []:
        parsed = _int_or_none(value)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


__all__ = ["Stage2PipelineService", "is_stage2_result"]
