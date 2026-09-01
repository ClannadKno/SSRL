# -*- coding: utf-8 -*-
"""V2 自动介入编排服务。

完整流程：
1. 检查 Feature Flag；
2. 从已完成的 monitor_run 创建 intervention_run；
3. 介入前重新验证；
4. 获取房间乐观锁；
5. 提交事务（不持事务调 LLM）；
6. 构建上下文；
7. 选择策略；
8. 调用统一 LLM（strategy_review_and_generation profile）；
9. PASS 时只审计并解锁；
10. INTERVENE 时发布消息（短事务验证 lock_token）；
11. 超时恢复调度；
12. Dry-run 模式：不锁房、不写 AI 消息。
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from db import db, execute, now_str, query_one
from config import (
    AUTO_INTERVENTION_V2_ENABLED,
    AUTO_INTERVENTION_V2_DRY_RUN,
    HUEY_IMMEDIATE,
    SSRL_AGENT_DEBUG,
    INTERVENTION_V2_LOCK_SECONDS,
    INTERVENTION_V2_MAX_CANDIDATE_STRATEGIES,
)
from services.discussion_pipeline_v2.monitor_run_repo import MonitorRunRepo
from services.intervention_pipeline_v2.intervention_run_repo import InterventionRunRepo
from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
from services.intervention_pipeline_v2.intervention_validator import InterventionValidator
from services.intervention_pipeline_v2.context_builder import ContextBuilder
from services.intervention_pipeline_v2.strategy_service import StrategyService
from services.intervention_pipeline_v2.strategy_review_service import (
    STRATEGY_REVIEW_PROFILE,
    STRATEGY_REVIEW_PROMPT_VERSION,
    review_strategy_context,
)
from services.audit_log_service import safe_write_audit_log

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = {
    "PUBLISHED",
    "FALLBACK",
    "PASS",
    "SKIPPED",
    "FAILED",
    "EXPIRED",
    "CANCELLED",
    "STALE",
    "DRY_RUN",
}

RETRYABLE_STRATEGY_FAILURE_TYPES = {
    "connect_timeout",
    "read_timeout",
    "network_error",
    "rate_limited",
    "upstream_5xx",
    "truncated_response",
}

RETRYABLE_STRATEGY_REASONS = {
    "invalid_json",
    "finish_reason_length",
    "truncated_response",
    "empty_response",
}


def _debug_log(msg, *args):
    if SSRL_AGENT_DEBUG:
        logging.getLogger(__name__).info('[SSRL_AGENT] ' + msg, *args)


def _monitor_audit_skip_reason(reason: str = None) -> str:
    text = str(reason or "")
    if "pending_recent_help_requests" in text or "pending_help" in text:
        return "pending_help_request"
    if "cooldown_active" in text:
        return "cooldown_active"
    if text.startswith("rule_candidate_confidence") and "below" in text:
        return "all_rule_scores_below_threshold"
    if "strategy_agent_disabled" in text or "task_agent_intervention_disabled" in text:
        return "auto_intervention_disabled"
    if "session" in text and ("not" in text or "no_active" in text):
        return "session_not_active"
    if "lock" in text or "AI_INTERVENING" in text:
        return "document_locked"
    if "task" in text and ("not_available" in text or "missing" in text):
        return "task_not_available"
    if text.startswith("non_intervention_state_unknown"):
        return "final_state_unknown"
    if text.startswith("non_intervention_state_positive_collaboration"):
        return "positive_state_no_intervention"
    return text or None


def _load_json(value, default=None):
    if value in (None, ""):
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_log_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return value
    return str(value)


def _log_strategy_event(event: str, **fields):
    safe = {
        key: _json_log_value(value)
        for key, value in fields.items()
        if value is not None
    }
    safe["event"] = event
    logger.info(
        "%s %s",
        event,
        json.dumps(safe, ensure_ascii=False, default=str, sort_keys=True),
        extra={"sera_event": event, "sera_fields": safe},
    )


class InterventionService:
    """V2 自动介入编排器。"""

    @staticmethod
    def is_enabled() -> bool:
        import config
        return bool(config.AUTO_INTERVENTION_V2_ENABLED)

    @staticmethod
    def is_dry_run() -> bool:
        import config
        return bool(config.AUTO_INTERVENTION_V2_DRY_RUN)

    # ------------------------------------------------------------------
    # 入口：从完成的 monitor_run 创建并执行介入
    # ------------------------------------------------------------------
    @staticmethod
    def execute(
        monitor_run_id: int,
        state_assessment_id: int = None,
        group_id: int = None,
        session_id: int = None,
        task_id: int = None,
        cutoff_sequence: int = None,
        trigger_source: str = "auto_state",
    ) -> dict:
        """Execute automatic strategy review anchored to a finalized assessment."""
        _debug_log(
            "execute ENTER monitor_run_id=%s state_assessment_id=%s",
            monitor_run_id,
            state_assessment_id,
        )

        if not InterventionService.is_enabled():
            if monitor_run_id:
                MonitorRunRepo.update_monitor_audit(
                    monitor_run_id,
                    {
                        "strategy_review_enqueued": False,
                        "skip_reason": "auto_intervention_disabled",
                        "strategy_review_skip_reason": "feature_flag_disabled",
                    },
                )
            return {"skipped": True, "reason": "feature_flag_disabled"}

        monitor_run_row = query_one("SELECT * FROM monitor_runs WHERE id=?", (monitor_run_id,))
        monitor_run = dict(monitor_run_row) if monitor_run_row else None
        if not monitor_run:
            return {"ok": False, "reason": "monitor_run_not_found", "monitor_run_id": monitor_run_id}
        if monitor_run["status"] != "completed":
            reason = f"monitor_run status={monitor_run['status']}"
            MonitorRunRepo.update_monitor_audit(
                monitor_run_id,
                {
                    "strategy_review_enqueued": False,
                    "skip_reason": "internal_error",
                    "strategy_review_skip_reason": reason,
                },
            )
            return {"skipped": True, "reason": reason, "monitor_run_id": monitor_run_id}

        contract = InterventionService._build_execution_contract(
            monitor_run=monitor_run,
            state_assessment_id=state_assessment_id,
            group_id=group_id,
            session_id=session_id,
            task_id=task_id,
            cutoff_sequence=cutoff_sequence,
            trigger_source=trigger_source,
        )
        result = {
            "monitor_run_id": monitor_run_id,
            "state_assessment_id": contract.get("state_assessment_id"),
            "group_id": contract.get("group_id"),
            "session_id": contract.get("session_id"),
            "task_id": contract.get("task_id"),
            "cutoff_sequence": contract.get("cutoff_sequence"),
            "dry_run": InterventionService.is_dry_run(),
            "steps": {},
            "intervention_run_id": None,
        }

        if not contract["valid"]:
            return InterventionService._record_skipped_result(
                result,
                contract=contract,
                monitor_run=monitor_run,
                reason=contract["reason"],
            )

        group_id = contract["group_id"]
        session_id = contract["session_id"]
        task_id = contract["task_id"]
        session_no = contract.get("session_no")
        cutoff_sequence = contract["cutoff_sequence"]
        state_assessment = contract["state_assessment"]
        detected_state = contract["detected_state"]
        confidence = contract.get("confidence")
        trigger_source = contract["trigger_source"]
        is_dry_run = result["dry_run"]
        run_id = None
        lock_token = None
        execute_started_perf = time.perf_counter()

        existing = InterventionRunRepo.get_by_assessment(
            group_id,
            session_id,
            contract["state_assessment_id"],
            trigger_source,
        ) or InterventionRunRepo.get_by_group_and_cutoff(group_id, cutoff_sequence, trigger_source)
        if existing:
            _log_strategy_event(
                "strategy_review_cancelled",
                group_id=group_id,
                session_id=session_id,
                task_id=task_id,
                assessment_id=contract["state_assessment_id"],
                monitor_run_id=monitor_run_id,
                intervention_run_id=existing["id"],
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                final_status=existing["status"],
                failure_stage="idempotency_precheck",
                decision=existing.get("decision"),
            )
            return {
                **result,
                "skipped": True,
                "reason": "state_assessment_already_reviewed",
                "existing_id": existing["id"],
                "existing_status": existing["status"],
            }

        try:
            group_switch = query_one(
                """
                SELECT COALESCE(auto_intervention_enabled, 1) AS enabled
                FROM groups
                WHERE id=?
                """,
                (group_id,),
            )
            if not group_switch or not bool(group_switch["enabled"]):
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason="group_auto_intervention_disabled",
                )

            from services.intervention_pipeline_v2.agent_research_helper import check_strategy_agent_enabled
            if not check_strategy_agent_enabled(group_id):
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason="strategy_agent_disabled",
                )

            from services.session_lifecycle import check_agent_allowed
            allowed, gate_reason = check_agent_allowed(
                group_id,
                session_id=session_id,
                task_id=task_id,
                session_no=session_no,
                agent_type="strategy",
            )
            if not allowed:
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason=gate_reason,
                )

            from db import get_agent_intervention_enabled_for_task
            if task_id and not get_agent_intervention_enabled_for_task(task_id, group_id=group_id):
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason="task_agent_intervention_disabled",
                )

            validation_monitor = dict(monitor_run)
            validation_monitor.update(
                {
                    "final_state": detected_state,
                    "confidence": confidence,
                    "trigger_type": trigger_source,
                    "trigger_source": trigger_source,
                    "session_id": session_id,
                }
            )
            validation = InterventionValidator.validate(group_id, cutoff_sequence, validation_monitor)
            result["validation"] = validation
            result["steps"]["validated"] = True
            if not validation["valid"]:
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason=validation.get("reason") or "validation_rejected",
                    validation=validation,
                    status="STALE" if validation.get("action") == "STALE" else "SKIPPED",
                )
            if validation.get("action") == "QUICK_RECHECK":
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason="stale_assessment",
                    validation=validation,
                    status="STALE",
                )

            candidates = StrategyService.find_strategies_for_state(
                detected_state,
                max_results=INTERVENTION_V2_MAX_CANDIDATE_STRATEGIES,
            )
            result["candidate_strategies"] = [c["id"] for c in candidates]
            result["steps"]["strategies_selected"] = True
            if not candidates:
                return InterventionService._record_skipped_result(
                    result,
                    contract=contract,
                    monitor_run=monitor_run,
                    reason="state_not_auto_intervention_candidate",
                )

            run_id = InterventionRunRepo.create(
                group_id=group_id,
                monitor_run_id=monitor_run_id,
                cutoff_sequence=cutoff_sequence,
                detected_state=detected_state,
                confidence=confidence,
                dry_run=is_dry_run,
                trigger_type=trigger_source,
                state_assessment_id=contract["state_assessment_id"],
                session_id=session_id,
                task_id=task_id,
                target_segment_id=contract.get("target_segment_id"),
                metadata={
                    "trigger_source": trigger_source,
                    "target_segment_id": contract.get("target_segment_id"),
                    "state_assessment": contract["assessment_summary"],
                    "validation": {k: v for k, v in validation.items() if k != "state_check"},
                    "candidate_strategies": [c["id"] for c in candidates],
                },
            )
            result["intervention_run_id"] = run_id
            result["steps"]["created"] = True
            _log_strategy_event(
                "strategy_review_started",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=contract["state_assessment_id"],
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=detected_state,
                fused_state=detected_state,
                candidate_strategies=[c["id"] for c in candidates],
            )

            if is_dry_run:
                InterventionRunRepo.set_status(
                    run_id,
                    "DRY_RUN",
                    decision="DRY_RUN",
                    teacher_reason="dry_run_no_publish",
                    completed_at=now_str(),
                )
                result["steps"]["dry_run_completed"] = True
                return result

            lock_token = RoomLeaseService.acquire(group_id, run_id)
            if not lock_token:
                InterventionRunRepo.set_status(
                    run_id,
                    "SKIPPED",
                    decision="SKIPPED",
                    skip_reason="lock_not_acquired",
                    lock_acquired=0,
                    completed_at=now_str(),
                )
                result["steps"]["lock_failed"] = True
                result["reason"] = "lock_not_acquired"
                _log_strategy_event(
                    "strategy_review_cancelled",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=contract["state_assessment_id"],
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    candidate_state=detected_state,
                    fused_state=detected_state,
                    final_status="SKIPPED",
                    failure_stage="room_lease",
                    decision="SKIPPED",
                )
                return result

            lock_info = RoomLeaseService.get_lock_info(group_id)
            _log_strategy_event(
                "room_lease_acquired",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=contract["state_assessment_id"],
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                lock_expires_at=lock_info.get("lock_expires_at"),
            )
            InterventionRunRepo.set_status(
                run_id,
                "LOCKED",
                lock_token=lock_token,
                lock_expires_at=lock_info.get("lock_expires_at"),
                lock_acquired=1,
            )
            result["lock_token"] = lock_token
            result["steps"]["locked"] = True
            if not HUEY_IMMEDIATE:
                InterventionService._schedule_release_expired(group_id, lock_token)

            stale_check = InterventionValidator._check_cutoff_sequence(
                group_id,
                cutoff_sequence,
                validation_monitor,
            )
            if stale_check.get("delta") is not None and stale_check.get("delta") > 0:
                InterventionRunRepo.set_status(
                    run_id,
                    "STALE",
                    decision="SKIPPED",
                    skip_reason="stale_assessment",
                    failure_reason="stale_assessment",
                    completed_at=now_str(),
                )
                InterventionService._release_room_lock(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    final_status="STALE",
                )
                _log_strategy_event(
                    "strategy_review_cancelled",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=contract["state_assessment_id"],
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    candidate_state=detected_state,
                    fused_state=detected_state,
                    final_status="STALE",
                    failure_stage="cutoff_recheck",
                    decision="SKIPPED",
                )
                result["steps"]["stale_assessment"] = True
                result["reason"] = "stale_assessment"
                MonitorRunRepo.update_monitor_audit(
                    monitor_run_id,
                    {
                        "strategy_review_enqueued": False,
                        "skip_reason": "stale_assessment",
                        "strategy_review_skip_reason": "stale_assessment",
                    },
                )
                return result

            review_started_at = now_str()
            context = ContextBuilder.build_strategy_review_context(
                group_id=group_id,
                session_id=session_id,
                monitor_run_id=monitor_run_id,
                cutoff_sequence=cutoff_sequence,
                candidate_strategies=candidates,
                state_assessment_id=contract["state_assessment_id"],
                state_assessment=state_assessment,
                trigger_source=trigger_source,
            )
            InterventionService._record_run_review_artifacts(
                run_id,
                context=context,
                review_result=None,
                status="GENERATING",
                started_at=review_started_at,
            )
            result["steps"]["context_built"] = True

            _debug_log(
                "will_call_llm=true profile=%s group=%s run=%s state=%s assessment=%s",
                STRATEGY_REVIEW_PROFILE,
                group_id,
                run_id,
                detected_state,
                contract["state_assessment_id"],
            )
            review_outcome = InterventionService._run_strategy_review_with_safe_retry(
                context=context,
                run_id=run_id,
                group_id=group_id,
                session_id=session_id,
                task_id=task_id,
                session_no=session_no,
                monitor_run_id=monitor_run_id,
                assessment_id=contract["state_assessment_id"],
                cutoff_sequence=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=detected_state,
                fused_state=detected_state,
                lock_token=lock_token,
            )
            review_result = review_outcome["review_result"]
            review_completed_at = review_outcome["completed_at"]
            result["strategy_review_result"] = review_result
            result["steps"]["llm_called"] = True

            MonitorRunRepo.update_strategy_review(
                monitor_run_id,
                context=context,
                review_result=review_result,
                started_at=review_started_at,
                completed_at=review_completed_at,
                error=None if review_result.get("ok") else review_result.get("reason"),
            )
            InterventionService._record_run_review_artifacts(
                run_id,
                context=context,
                review_result=review_result,
                status="VALIDATING",
                started_at=review_started_at,
                completed_at=review_completed_at,
            )

            if review_outcome.get("cancelled"):
                reason = review_result.get("reason", "strategy_review_cancelled")
                InterventionService._skip_without_student_message(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    reason=reason,
                )
                result["steps"]["retry_cancelled"] = True
                result["published"] = False
                result["reason"] = reason
                MonitorRunRepo.update_monitor_audit(
                    monitor_run_id,
                    {
                        "strategy_review_skip_reason": reason,
                        "strategy_review_error": None,
                    },
                )
                return result

            if not review_result.get("ok"):
                reason = review_result.get("reason", "strategy_review_failed")
                InterventionService._fail_without_student_message(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    reason=reason,
                )
                result["steps"]["failed_without_student_message"] = True
                result["fallback_used"] = False
                result["reason"] = reason
                MonitorRunRepo.update_monitor_audit(
                    monitor_run_id,
                    {
                        "strategy_review_skip_reason": reason,
                        "strategy_review_error": reason,
                    },
                )
                return result

            result["state_segments"] = {
                "skipped": True,
                "reason": "state_segments_owned_by_monitoring",
            }

            if review_result.get("decision") == "PASS":
                _log_strategy_event(
                    "strategy_review_decision_pass",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=contract["state_assessment_id"],
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    candidate_state=detected_state,
                    fused_state=detected_state,
                    decision="PASS",
                    elapsed_ms=review_result.get("elapsed_ms"),
                )
                result["review_decision"] = "PASS"
                result["published"] = False
                InterventionService._complete_without_student_message(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    reason=review_result.get("teacher_reason") or review_result.get("reason"),
                )
                result["steps"]["pass_recorded"] = True
                return result

            generated_message = review_result.get("student_message") or review_result.get("message") or ""
            selected_strategy_id = review_result.get("strategy") or review_result.get("strategy_id")
            _log_strategy_event(
                "strategy_review_decision_intervene",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=contract["state_assessment_id"],
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=detected_state,
                fused_state=detected_state,
                decision="INTERVENE",
                strategy_id=selected_strategy_id,
                elapsed_ms=review_result.get("elapsed_ms"),
            )
            publish_allowed, publish_gate_reason = check_agent_allowed(
                group_id,
                session_id=session_id,
                task_id=task_id,
                session_no=session_no,
                agent_type="strategy",
            )
            result["steps"]["publish_gate_rechecked"] = True
            if not publish_allowed:
                InterventionService._skip_without_student_message(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    reason=publish_gate_reason,
                )
                result["published"] = False
                result["reason"] = publish_gate_reason
                result["steps"]["blocked_before_publish"] = True
                MonitorRunRepo.update_monitor_audit(
                    monitor_run_id,
                    {
                        "strategy_review_skip_reason": publish_gate_reason,
                        "strategy_review_publish_gate": publish_gate_reason,
                    },
                )
                return result

            _log_strategy_event(
                "intervention_publish_started",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=contract["state_assessment_id"],
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=detected_state,
                fused_state=detected_state,
                decision="INTERVENE",
                strategy_id=selected_strategy_id,
                message_chars=len(generated_message),
            )
            publish_result = InterventionService._publish(
                group_id=group_id,
                intervention_run_id=run_id,
                lock_token=lock_token,
                message=generated_message,
                strategy_id=selected_strategy_id,
                trigger_source=trigger_source,
                prompt_version=review_result.get("prompt_version") or STRATEGY_REVIEW_PROMPT_VERSION,
                session_id=session_id,
                task_id=task_id,
                session_no=session_no,
                teacher_reason=review_result.get("teacher_reason") or review_result.get("reason"),
            )
            publish_ok = bool(publish_result.get("ok"))
            result["steps"]["published"] = publish_ok
            result["published"] = publish_ok
            result["message_id"] = publish_result.get("message_id")

            if publish_ok:
                _log_strategy_event(
                    "intervention_published",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=contract["state_assessment_id"],
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    candidate_state=detected_state,
                    fused_state=detected_state,
                    decision="INTERVENE",
                    strategy_id=selected_strategy_id,
                    publish_message_id=publish_result.get("message_id"),
                    final_status="PUBLISHED",
                    elapsed_ms=int((time.perf_counter() - execute_started_perf) * 1000),
                    duplicate=publish_result.get("duplicate"),
                )
                _log_strategy_event(
                    "intervention_run_finished",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=contract["state_assessment_id"],
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    decision="INTERVENE",
                    strategy_id=selected_strategy_id,
                    publish_message_id=publish_result.get("message_id"),
                    final_status="PUBLISHED",
                    elapsed_ms=int((time.perf_counter() - execute_started_perf) * 1000),
                )
                _debug_log(
                    "[intervention] DONE: assistant_message_id=%s group=%s run=%s content_length=%s",
                    publish_result.get("message_id"),
                    group_id,
                    run_id,
                    len(generated_message),
                )
                return result

            _log_strategy_event(
                "intervention_publish_failed",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=contract["state_assessment_id"],
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=detected_state,
                fused_state=detected_state,
                decision="INTERVENE",
                strategy_id=selected_strategy_id,
                final_status="FAILED",
                failure_stage="publish",
                exception_type=publish_result.get("reason"),
            )
            InterventionService._fail_without_student_message(
                run_id=run_id,
                group_id=group_id,
                lock_token=lock_token,
                reason=publish_result.get("reason") or "publish_failed",
                failure_stage="publish",
            )
            result["steps"]["failed_without_student_message"] = True
            result["reason"] = publish_result.get("reason") or "publish_failed"
            return result

        except Exception as exc:
            _debug_log("EXCEPTION monitor_run_id=%s error=%s", monitor_run_id, str(exc))
            if run_id is not None:
                logger.exception("InterventionService.execute failed for run %s", run_id)
                InterventionService._fail_without_student_message(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    reason=f"{exc.__class__.__name__}: {str(exc)[:300]}",
                    failure_stage="execute_exception",
                )
            else:
                logger.exception(
                    "InterventionService.execute failed before run creation: group=%s error=%s",
                    group_id,
                    exc,
                )
            result["error"] = str(exc)
            result["steps"]["failed"] = True
            result["reason"] = "intervention_execute_exception"
            MonitorRunRepo.update_monitor_audit(
                monitor_run_id,
                {
                    "strategy_review_enqueued": False,
                    "skip_reason": "internal_error",
                    "strategy_review_skip_reason": "intervention_execute_exception",
                    "strategy_review_error": str(exc),
                },
            )
            return result

    # ------------------------------------------------------------------

    @staticmethod
    def _state_assessment_id_from_monitor(monitor_run: dict) -> int:
        payload = _load_json((monitor_run or {}).get("rule_result_json"), {}) or {}
        audit = payload.get("monitor_audit") if isinstance(payload, dict) else {}
        return _as_int((audit or {}).get("state_assessment_id"))

    @staticmethod
    def _target_segment_id_from_monitor(monitor_run: dict) -> int:
        payload = _load_json((monitor_run or {}).get("rule_result_json"), {}) or {}
        audit = payload.get("monitor_audit") if isinstance(payload, dict) else {}
        return _as_int((audit or {}).get("segment_id"))

    @staticmethod
    def _build_execution_contract(
        *,
        monitor_run: dict,
        state_assessment_id: int = None,
        group_id: int = None,
        session_id: int = None,
        task_id: int = None,
        cutoff_sequence: int = None,
        trigger_source: str = "auto_state",
    ) -> dict:
        from db import get_current_running_session_context
        from services.intervention_pipeline_v2.strategy_service import FORMAL_INTERVENTION_STATES

        monitor_group_id = _as_int((monitor_run or {}).get("group_id"))
        state_assessment_id = _as_int(state_assessment_id) or InterventionService._state_assessment_id_from_monitor(monitor_run)
        resolved_group_id = _as_int(group_id) or monitor_group_id
        resolved_cutoff = _as_int(cutoff_sequence) or _as_int((monitor_run or {}).get("cutoff_sequence")) or 0
        target_segment_id = InterventionService._target_segment_id_from_monitor(
            monitor_run
        )
        monitor_trigger_type = (monitor_run or {}).get("trigger_type")
        if (
            monitor_trigger_type == "student_help_request"
            and trigger_source in (None, "", "auto_state")
        ):
            trigger_source = monitor_trigger_type
        elif (
            monitor_trigger_type == "silence_check"
            and trigger_source in (None, "", "auto_state")
        ):
            trigger_source = "silence_rule"
        else:
            trigger_source = trigger_source or monitor_trigger_type or "auto_state"

        if not state_assessment_id:
            return {
                "valid": False,
                "reason": "state_assessment_missing",
                "group_id": resolved_group_id,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": None,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }

        assessment_row = query_one("SELECT * FROM state_assessments WHERE id=?", (state_assessment_id,))
        if not assessment_row:
            return {
                "valid": False,
                "reason": "state_assessment_not_found",
                "group_id": resolved_group_id,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        assessment = dict(assessment_row)
        assessment_group_id = _as_int(assessment.get("group_id"))
        if monitor_group_id and assessment_group_id and monitor_group_id != assessment_group_id:
            return {
                "valid": False,
                "reason": "state_assessment_group_mismatch",
                "group_id": monitor_group_id,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        if resolved_group_id and assessment_group_id and resolved_group_id != assessment_group_id:
            return {
                "valid": False,
                "reason": "state_assessment_group_mismatch",
                "group_id": resolved_group_id,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        resolved_group_id = assessment_group_id or resolved_group_id

        current_ctx = get_current_running_session_context() or {}
        resolved_session_id = _as_int(session_id) or _as_int(assessment.get("session_id")) or _as_int(current_ctx.get("session_id"))
        resolved_task_id = _as_int(task_id) or _as_int(assessment.get("task_id")) or _as_int(current_ctx.get("task_id"))
        resolved_session_no = _as_int(assessment.get("session_no")) or _as_int(current_ctx.get("session_no"))

        assessment_session_id = _as_int(assessment.get("session_id"))
        if _as_int(session_id) and assessment_session_id and _as_int(session_id) != assessment_session_id:
            return {
                "valid": False,
                "reason": "state_assessment_session_mismatch",
                "group_id": resolved_group_id,
                "session_id": _as_int(session_id),
                "task_id": resolved_task_id,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        assessment_task_id = _as_int(assessment.get("task_id"))
        if _as_int(task_id) and assessment_task_id and _as_int(task_id) != assessment_task_id:
            return {
                "valid": False,
                "reason": "state_assessment_task_mismatch",
                "group_id": resolved_group_id,
                "session_id": resolved_session_id,
                "task_id": _as_int(task_id),
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }

        summary = ContextBuilder._summarize_state_assessment(assessment, monitor_run=monitor_run)
        detected_state = summary.get("detected_state") or "unknown"
        assessment_status = summary.get("assessment_status")
        if assessment_status in (None, "", "insufficient_evidence", "pending", "running"):
            return {
                "valid": False,
                "reason": "state_assessment_not_completed",
                "group_id": resolved_group_id,
                "session_id": resolved_session_id,
                "task_id": resolved_task_id,
                "session_no": resolved_session_no,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "state_assessment": assessment,
                "assessment_summary": summary,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        if detected_state == "unknown":
            return {
                "valid": False,
                "reason": "final_state_unknown",
                "group_id": resolved_group_id,
                "session_id": resolved_session_id,
                "task_id": resolved_task_id,
                "session_no": resolved_session_no,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "state_assessment": assessment,
                "assessment_summary": summary,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        if detected_state not in FORMAL_INTERVENTION_STATES:
            return {
                "valid": False,
                "reason": f"non_intervention_state_{detected_state}",
                "group_id": resolved_group_id,
                "session_id": resolved_session_id,
                "task_id": resolved_task_id,
                "session_no": resolved_session_no,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "state_assessment": assessment,
                "assessment_summary": summary,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }
        if not bool(assessment.get("should_intervene")):
            return {
                "valid": False,
                "reason": "state_assessment_should_intervene_false",
                "group_id": resolved_group_id,
                "session_id": resolved_session_id,
                "task_id": resolved_task_id,
                "session_no": resolved_session_no,
                "cutoff_sequence": resolved_cutoff,
                "state_assessment_id": state_assessment_id,
                "state_assessment": assessment,
                "assessment_summary": summary,
                "trigger_source": trigger_source,
                "target_segment_id": target_segment_id,
            }

        return {
            "valid": True,
            "group_id": resolved_group_id,
            "session_id": resolved_session_id,
            "task_id": resolved_task_id,
            "session_no": resolved_session_no,
            "cutoff_sequence": resolved_cutoff,
            "state_assessment_id": state_assessment_id,
            "state_assessment": assessment,
            "assessment_summary": summary,
            "detected_state": detected_state,
            "confidence": summary.get("confidence"),
            "trigger_source": trigger_source,
            "target_segment_id": target_segment_id,
        }

    @staticmethod
    def _record_skipped_result(
        result: dict,
        *,
        contract: dict,
        monitor_run: dict,
        reason: str,
        validation: dict = None,
        status: str = "SKIPPED",
    ) -> dict:
        group_id = contract.get("group_id") or (monitor_run or {}).get("group_id")
        cutoff_sequence = contract.get("cutoff_sequence") or (monitor_run or {}).get("cutoff_sequence") or 0
        state_assessment_id = contract.get("state_assessment_id")
        session_id = contract.get("session_id")
        task_id = contract.get("task_id")
        trigger_source = contract.get("trigger_source") or "auto_state"
        detected_state = (
            (contract.get("assessment_summary") or {}).get("detected_state")
            or (monitor_run or {}).get("final_state")
        )
        confidence = (
            (contract.get("assessment_summary") or {}).get("confidence")
            if contract.get("assessment_summary")
            else (monitor_run or {}).get("confidence")
        )

        if group_id:
            existing = (
                InterventionRunRepo.get_by_assessment(
                    group_id,
                    session_id,
                    state_assessment_id,
                    trigger_source,
                )
                if state_assessment_id
                else InterventionRunRepo.get_by_group_and_cutoff(group_id, cutoff_sequence, trigger_source)
            )
            if existing:
                result.update(
                    {
                        "skipped": True,
                        "reason": "state_assessment_already_reviewed",
                        "existing_id": existing["id"],
                        "existing_status": existing["status"],
                    }
                )
                return result
            try:
                run_id = InterventionRunRepo.create(
                    group_id=group_id,
                    monitor_run_id=(monitor_run or {}).get("id"),
                    cutoff_sequence=cutoff_sequence,
                    detected_state=detected_state,
                    confidence=confidence,
                    dry_run=InterventionService.is_dry_run(),
                    trigger_type=trigger_source,
                    state_assessment_id=state_assessment_id,
                    session_id=session_id,
                    task_id=task_id,
                    target_segment_id=contract.get("target_segment_id"),
                    metadata={
                        "trigger_source": trigger_source,
                        "target_segment_id": contract.get("target_segment_id"),
                        "skip_reason": reason,
                        "state_assessment": contract.get("assessment_summary"),
                        "validation": validation,
                    },
                )
                result["intervention_run_id"] = run_id
                extra = {
                    "decision": "SKIPPED",
                    "reason_code": reason,
                    "guard_reason": reason,
                    "skip_reason": reason,
                    "completed_at": now_str(),
                    "lock_acquired": 0,
                }
                if validation:
                    extra["cooldown_result"] = InterventionService._json_for_db(validation.get("cooldown_check"))
                    extra["validation_json"] = InterventionService._json_for_db(validation)
                if status == "STALE":
                    extra["failure_reason"] = reason
                InterventionRunRepo.set_status(run_id, status, **extra)
            except Exception:
                logger.exception("Failed to record skipped intervention_run for monitor_run %s", (monitor_run or {}).get("id"))

        if (monitor_run or {}).get("id"):
            MonitorRunRepo.update_monitor_audit(
                monitor_run["id"],
                {
                    "strategy_review_enqueued": False,
                    "skip_reason": _monitor_audit_skip_reason(reason),
                    "strategy_review_skip_reason": reason,
                },
            )
        result["skipped"] = True
        result["reason"] = reason
        result["steps"]["rejected"] = True
        return result

    @staticmethod
    def record_skipped_for_monitor(
        monitor_run_id: int,
        *,
        state_assessment_id: int = None,
        group_id: int = None,
        session_id: int = None,
        task_id: int = None,
        cutoff_sequence: int = None,
        trigger_source: str = "auto_state",
        reason: str = "pending_help_request",
    ) -> dict:
        monitor_run_row = query_one("SELECT * FROM monitor_runs WHERE id=?", (monitor_run_id,))
        monitor_run = dict(monitor_run_row) if monitor_run_row else None
        result = {
            "monitor_run_id": monitor_run_id,
            "intervention_run_id": None,
            "skipped": True,
            "reason": reason,
            "steps": {},
        }
        if not monitor_run:
            return {**result, "ok": False, "reason": "monitor_run_not_found"}
        contract = InterventionService._build_execution_contract(
            monitor_run=monitor_run,
            state_assessment_id=state_assessment_id,
            group_id=group_id,
            session_id=session_id,
            task_id=task_id,
            cutoff_sequence=cutoff_sequence,
            trigger_source=trigger_source or "auto_state",
        )
        if not contract.get("valid"):
            contract["reason"] = contract.get("reason") or reason
        return InterventionService._record_skipped_result(
            result,
            contract=contract,
            monitor_run=monitor_run,
            reason=reason,
        )

    @staticmethod
    def _strategy_review_retry_delay_seconds() -> float:
        raw = os.environ.get("SERA_STRATEGY_REVIEW_RETRY_DELAY_SECONDS", "0.5")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid SERA_STRATEGY_REVIEW_RETRY_DELAY_SECONDS=%r; using 0.5", raw)
            return 0.5
        if value < 0:
            logger.warning("Invalid negative SERA_STRATEGY_REVIEW_RETRY_DELAY_SECONDS=%r; using 0.5", raw)
            return 0.5
        return min(value, 1.5)

    @staticmethod
    def _strategy_review_max_attempts() -> int:
        try:
            from services.llm_gateway import BUILTIN_PROFILES, LlmProfile

            cfg = BUILTIN_PROFILES[STRATEGY_REVIEW_PROFILE]
            profile = LlmProfile.from_env(
                STRATEGY_REVIEW_PROFILE,
                env_prefix=cfg["env_prefix"],
                defaults=cfg,
            )
            retries = int(profile.retries)
        except Exception:
            retries = 1
        return max(1, min(retries + 1, 3))

    @staticmethod
    def _review_strategy_context_once(context: dict, gateway):
        try:
            kwargs = {"max_attempts_override": 1}
            if gateway is not None:
                kwargs["gateway"] = gateway
            return review_strategy_context(context, **kwargs)
        except TypeError:
            return review_strategy_context(context)

    @staticmethod
    def _strategy_review_failure_type(review_result: dict) -> Optional[str]:
        llm_meta = (review_result or {}).get("llm_result") or {}
        return llm_meta.get("failure_type") or (review_result or {}).get("failure_type")

    @staticmethod
    def _strategy_review_timeout_type(review_result: dict) -> Optional[str]:
        failure_type = InterventionService._strategy_review_failure_type(review_result)
        if failure_type in {"connect_timeout", "read_timeout"}:
            return failure_type
        return None

    @staticmethod
    def _is_retryable_strategy_review_failure(review_result: dict) -> bool:
        if not review_result or review_result.get("ok"):
            return False
        llm_meta = review_result.get("llm_result") or {}
        if llm_meta.get("retryable") is True:
            return True
        if llm_meta.get("retryable") is False:
            return False
        failure_type = InterventionService._strategy_review_failure_type(review_result)
        if failure_type in RETRYABLE_STRATEGY_FAILURE_TYPES:
            return True
        reason = str(review_result.get("reason") or "")
        return reason in RETRYABLE_STRATEGY_REASONS

    @staticmethod
    def _check_strategy_retry_business_state(
        *,
        run_id: int,
        group_id: int,
        session_id: int,
        task_id: int,
        session_no: int,
        cutoff_sequence: int,
        trigger_source: str,
        lock_token: str,
    ) -> Optional[str]:
        run = InterventionRunRepo.get(run_id)
        if not run:
            return "intervention_run_not_found"
        status = str(run.get("status") or "").upper()
        if status in TERMINAL_RUN_STATUSES:
            return f"intervention_run_terminal_{status.lower()}"
        if run.get("message_id"):
            return "intervention_run_already_published"

        group_switch = query_one(
            """
            SELECT COALESCE(auto_intervention_enabled, 1) AS enabled
            FROM groups
            WHERE id=?
            """,
            (group_id,),
        )
        if not group_switch or not bool(group_switch["enabled"]):
            return "group_auto_intervention_disabled"

        lock_info = RoomLeaseService.get_lock_info(group_id)
        if not lock_info.get("exists"):
            return "group_not_found"
        if lock_info.get("state") != RoomLeaseService.LOCK_STATE:
            return "room_lock_lost"
        if lock_token and lock_info.get("lock_token") != lock_token:
            return "room_lock_token_mismatch"
        active_run_id = _as_int(lock_info.get("active_intervention_run_id"))
        if active_run_id is not None and active_run_id != run_id:
            return "room_lock_owner_mismatch"

        from services.intervention_pipeline_v2.agent_research_helper import check_strategy_agent_enabled
        if not check_strategy_agent_enabled(group_id):
            return "strategy_agent_disabled"

        from services.session_lifecycle import check_agent_allowed
        allowed, gate_reason = check_agent_allowed(
            group_id,
            session_id=session_id,
            task_id=task_id,
            session_no=session_no,
            agent_type="strategy",
        )
        if not allowed:
            return gate_reason or "agent_not_allowed"

        from db import get_agent_intervention_enabled_for_task
        if task_id and not get_agent_intervention_enabled_for_task(task_id, group_id=group_id):
            return "task_agent_intervention_disabled"

        stale_check = InterventionValidator._check_cutoff_sequence(
            group_id,
            cutoff_sequence,
            run,
        )
        if not stale_check.get("ok"):
            return stale_check.get("reason") or "cutoff_recheck_failed"
        if stale_check.get("delta") is not None and stale_check.get("delta") > 0:
            return "stale_assessment"

        covered = query_one(
            """
            SELECT id, status FROM intervention_runs
            WHERE id<>?
              AND group_id=?
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND COALESCE(trigger_type, 'auto_state')=?
              AND status IN ('PUBLISHED','FALLBACK')
              AND (
                    (state_assessment_id IS NOT NULL AND state_assessment_id=?)
                    OR COALESCE(cutoff_sequence, 0)=COALESCE(?, 0)
                  )
            ORDER BY id DESC LIMIT 1
            """,
            (
                run_id,
                group_id,
                trigger_source or "auto_state",
                run.get("state_assessment_id"),
                cutoff_sequence,
            ),
        )
        if covered:
            return "successful_intervention_already_exists"
        return None

    @staticmethod
    def _run_strategy_review_with_safe_retry(
        *,
        context: dict,
        run_id: int,
        group_id: int,
        session_id: int,
        task_id: int,
        session_no: int,
        monitor_run_id: int,
        assessment_id: int,
        cutoff_sequence: int,
        trigger_source: str,
        candidate_state: str,
        fused_state: str,
        lock_token: str,
    ) -> dict:
        gateway = None
        max_attempts = InterventionService._strategy_review_max_attempts()
        retry_delay = InterventionService._strategy_review_retry_delay_seconds()
        attempts = []
        started_perf = time.perf_counter()
        prompt_message_count = len((context or {}).get("messages") or [])
        context_chars = len(json.dumps(context or {}, ensure_ascii=False, default=str))
        final_result = None

        for attempt in range(1, max_attempts + 1):
            _log_strategy_event(
                "strategy_review_attempt_started",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=assessment_id,
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=candidate_state,
                fused_state=fused_state,
                attempt=attempt,
                max_attempts=max_attempts,
                prompt_message_count=prompt_message_count,
                context_chars=context_chars,
            )
            attempt_started = time.perf_counter()
            InterventionRunRepo.set_status(
                run_id,
                "GENERATING",
                generator_params_json=InterventionService._json_for_db(
                    {
                        "profile": STRATEGY_REVIEW_PROFILE,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "retry_delay_seconds": retry_delay,
                    }
                ),
            )
            review_result = InterventionService._review_strategy_context_once(context, gateway)
            attempt_elapsed_ms = int((time.perf_counter() - attempt_started) * 1000)
            llm_meta = (review_result or {}).get("llm_result") or {}
            attempt_record = {
                "attempt": attempt,
                "ok": bool((review_result or {}).get("ok")),
                "elapsed_ms": attempt_elapsed_ms,
                "reason": (review_result or {}).get("reason"),
                "failure_type": llm_meta.get("failure_type"),
                "timeout_type": InterventionService._strategy_review_timeout_type(review_result or {}),
                "retryable": InterventionService._is_retryable_strategy_review_failure(review_result or {}),
                "llm_attempt_count": llm_meta.get("attempt_count"),
                "status_code": llm_meta.get("status_code"),
                "finish_reason": llm_meta.get("finish_reason"),
            }
            attempts.append(attempt_record)
            final_result = review_result

            if review_result.get("ok"):
                elapsed_ms = int((time.perf_counter() - started_perf) * 1000)
                review_result.update(
                    {
                        "attempts": attempts,
                        "attempt_count": attempt,
                        "max_attempts": max_attempts,
                        "elapsed_ms": elapsed_ms,
                        "final_status": "PASS" if review_result.get("decision") == "PASS" else "PUBLISHED_PENDING",
                    }
                )
                _log_strategy_event(
                    "strategy_review_succeeded",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=assessment_id,
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    candidate_state=candidate_state,
                    fused_state=fused_state,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    elapsed_ms=elapsed_ms,
                    decision=review_result.get("decision"),
                    strategy_id=review_result.get("strategy_id") or review_result.get("strategy"),
                )
                return {
                    "review_result": review_result,
                    "completed_at": now_str(),
                    "cancelled": False,
                }

            retryable = InterventionService._is_retryable_strategy_review_failure(review_result)
            _log_strategy_event(
                "strategy_review_attempt_failed",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=assessment_id,
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=candidate_state,
                fused_state=fused_state,
                attempt=attempt,
                max_attempts=max_attempts,
                elapsed_ms=attempt_elapsed_ms,
                failure_stage="strategy_review",
                exception_type=attempt_record.get("failure_type") or review_result.get("reason"),
                timeout_type=attempt_record.get("timeout_type"),
            )

            if not retryable or attempt >= max_attempts:
                break

            cancellation_reason = InterventionService._check_strategy_retry_business_state(
                run_id=run_id,
                group_id=group_id,
                session_id=session_id,
                task_id=task_id,
                session_no=session_no,
                cutoff_sequence=cutoff_sequence,
                trigger_source=trigger_source,
                lock_token=lock_token,
            )
            if cancellation_reason:
                elapsed_ms = int((time.perf_counter() - started_perf) * 1000)
                cancelled = dict(review_result)
                cancelled.update(
                    {
                        "ok": False,
                        "cancelled": True,
                        "reason": cancellation_reason,
                        "attempts": attempts,
                        "attempt_count": attempt,
                        "max_attempts": max_attempts,
                        "elapsed_ms": elapsed_ms,
                        "final_status": "SKIPPED",
                        "failure_stage": "retry_business_recheck",
                    }
                )
                _log_strategy_event(
                    "strategy_review_cancelled",
                    group_id=group_id,
                    session_id=session_id,
                    session_no=session_no,
                    task_id=task_id,
                    assessment_id=assessment_id,
                    monitor_run_id=monitor_run_id,
                    intervention_run_id=run_id,
                    cutoff_message_id=cutoff_sequence,
                    trigger_source=trigger_source,
                    candidate_state=candidate_state,
                    fused_state=fused_state,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    elapsed_ms=elapsed_ms,
                    final_status="SKIPPED",
                    failure_stage="retry_business_recheck",
                    exception_type=cancellation_reason,
                )
                return {
                    "review_result": cancelled,
                    "completed_at": now_str(),
                    "cancelled": True,
                }

            _log_strategy_event(
                "strategy_review_retry_scheduled",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                assessment_id=assessment_id,
                monitor_run_id=monitor_run_id,
                intervention_run_id=run_id,
                cutoff_message_id=cutoff_sequence,
                trigger_source=trigger_source,
                candidate_state=candidate_state,
                fused_state=fused_state,
                attempt=attempt,
                max_attempts=max_attempts,
                elapsed_ms=attempt_elapsed_ms,
                retry_delay_seconds=retry_delay,
                failure_stage="strategy_review",
                exception_type=attempt_record.get("failure_type") or review_result.get("reason"),
                timeout_type=attempt_record.get("timeout_type"),
            )
            if retry_delay > 0:
                time.sleep(retry_delay)

        elapsed_ms = int((time.perf_counter() - started_perf) * 1000)
        final_result = dict(final_result or {})
        final_result.update(
            {
                "ok": False,
                "attempts": attempts,
                "attempt_count": len(attempts),
                "max_attempts": max_attempts,
                "elapsed_ms": elapsed_ms,
                "final_status": "FAILED",
                "failure_stage": "strategy_review",
                "timeout_type": InterventionService._strategy_review_timeout_type(final_result),
            }
        )
        return {
            "review_result": final_result,
            "completed_at": now_str(),
            "cancelled": False,
        }

    @staticmethod
    def _release_room_lock(
        *,
        run_id: int,
        group_id: int,
        lock_token: Optional[str],
        final_status: str,
    ) -> bool:
        if not lock_token:
            return False
        run = InterventionRunRepo.get(run_id) or {}
        try:
            released = RoomLeaseService.release(group_id, lock_token)
            if released:
                _log_strategy_event(
                    "room_lease_released",
                    group_id=group_id,
                    session_id=run.get("session_id"),
                    task_id=run.get("task_id"),
                    assessment_id=run.get("state_assessment_id"),
                    monitor_run_id=run.get("monitor_run_id"),
                    intervention_run_id=run_id,
                    cutoff_message_id=run.get("cutoff_sequence"),
                    trigger_source=run.get("trigger_type"),
                    final_status=final_status,
                )
            else:
                _log_strategy_event(
                    "room_lease_release_failed",
                    group_id=group_id,
                    session_id=run.get("session_id"),
                    task_id=run.get("task_id"),
                    assessment_id=run.get("state_assessment_id"),
                    monitor_run_id=run.get("monitor_run_id"),
                    intervention_run_id=run_id,
                    cutoff_message_id=run.get("cutoff_sequence"),
                    trigger_source=run.get("trigger_type"),
                    final_status=final_status,
                    failure_stage="room_lease_release",
                )
            return released
        except Exception as exc:
            _log_strategy_event(
                "room_lease_release_failed",
                group_id=group_id,
                session_id=run.get("session_id"),
                task_id=run.get("task_id"),
                assessment_id=run.get("state_assessment_id"),
                monitor_run_id=run.get("monitor_run_id"),
                intervention_run_id=run_id,
                cutoff_message_id=run.get("cutoff_sequence"),
                trigger_source=run.get("trigger_type"),
                final_status=final_status,
                failure_stage="room_lease_release",
                exception_type=exc.__class__.__name__,
            )
            logger.exception("Error releasing room lock for intervention_run %s", run_id)
            return False

    @staticmethod
    def _fail_without_student_message(
        run_id: int,
        group_id: int,
        lock_token: Optional[str] = None,
        reason: str = "intervention_failed",
        failure_stage: str = "strategy_review",
    ) -> bool:
        """Fail an automatic intervention without publishing a template fallback."""
        safe_reason = (reason or "intervention_failed")[:500]
        marked_failed = False
        try:
            InterventionRunRepo.mark_failed(run_id, safe_reason)
            marked_failed = True
        except Exception:
            logger.exception("Failed to mark intervention_run %s as FAILED", run_id)
        finally:
            if lock_token:
                InterventionService._release_room_lock(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    final_status="FAILED",
                )
            run = InterventionRunRepo.get(run_id) or {}
            _log_strategy_event(
                "intervention_run_finished",
                group_id=group_id,
                session_id=run.get("session_id"),
                task_id=run.get("task_id"),
                assessment_id=run.get("state_assessment_id"),
                monitor_run_id=run.get("monitor_run_id"),
                intervention_run_id=run_id,
                cutoff_message_id=run.get("cutoff_sequence"),
                trigger_source=run.get("trigger_type"),
                decision="FAILED",
                final_status="FAILED",
                failure_stage=failure_stage,
                exception_type=safe_reason,
            )
        return marked_failed

    @staticmethod
    def _complete_without_student_message(
        run_id: int,
        group_id: int,
        lock_token: Optional[str] = None,
        reason: str = "pass_no_intervention",
    ) -> bool:
        """Finish a review without publishing or starting strategy cooldown."""
        completed = False
        try:
            InterventionRunRepo.mark_pass(run_id, teacher_reason=reason or "pass_no_intervention")
            completed = True
        except Exception:
            logger.exception("Failed to complete intervention_run %s without publish", run_id)
        finally:
            if lock_token:
                InterventionService._release_room_lock(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    final_status="PASS",
                )
            run = InterventionRunRepo.get(run_id) or {}
            _log_strategy_event(
                "intervention_run_finished",
                group_id=group_id,
                session_id=run.get("session_id"),
                task_id=run.get("task_id"),
                assessment_id=run.get("state_assessment_id"),
                monitor_run_id=run.get("monitor_run_id"),
                intervention_run_id=run_id,
                cutoff_message_id=run.get("cutoff_sequence"),
                trigger_source=run.get("trigger_type"),
                decision="PASS",
                final_status="PASS",
            )
        return completed

    @staticmethod
    def _skip_without_student_message(
        run_id: int,
        group_id: int,
        lock_token: Optional[str] = None,
        reason: str = "intervention_skipped",
    ) -> bool:
        """Skip a locked review without publishing and always release the room."""
        safe_reason = (reason or "intervention_skipped")[:500]
        skipped = False
        try:
            InterventionRunRepo.mark_skipped(run_id, safe_reason)
            skipped = True
        except Exception:
            logger.exception("Failed to mark intervention_run %s as SKIPPED", run_id)
        finally:
            if lock_token:
                InterventionService._release_room_lock(
                    run_id=run_id,
                    group_id=group_id,
                    lock_token=lock_token,
                    final_status="SKIPPED",
                )
            run = InterventionRunRepo.get(run_id) or {}
            _log_strategy_event(
                "intervention_run_finished",
                group_id=group_id,
                session_id=run.get("session_id"),
                task_id=run.get("task_id"),
                assessment_id=run.get("state_assessment_id"),
                monitor_run_id=run.get("monitor_run_id"),
                intervention_run_id=run_id,
                cutoff_message_id=run.get("cutoff_sequence"),
                trigger_source=run.get("trigger_type"),
                decision="SKIPPED",
                final_status="SKIPPED",
                failure_stage="business_recheck",
                exception_type=safe_reason,
            )
        return skipped

    @staticmethod
    def _json_for_db(value) -> str:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _record_run_review_artifacts(
        run_id: int,
        *,
        context: dict,
        review_result: dict = None,
        status: str = None,
        started_at: str = None,
        completed_at: str = None,
    ):
        """Persist context and single-call review artifacts on intervention_runs."""
        review_result = review_result or {}
        boundary = (context or {}).get("context_boundary") or {}
        llm_meta = review_result.get("llm_result") or {}
        extra = {
            "context_from_sequence": boundary.get("from_sequence") or (context or {}).get("context_from_sequence"),
            "context_to_sequence": boundary.get("to_sequence") or (context or {}).get("context_to_sequence"),
            "input_message_sequences_json": InterventionService._json_for_db((context or {}).get("input_message_sequences") or []),
            "evidence_sequences_json": InterventionService._json_for_db(
                review_result.get("evidence_sequences")
                or ((context or {}).get("state_assessment") or {}).get("evidence_message_ids")
                or []
            ),
            "selected_strategy_id": review_result.get("strategy") or review_result.get("strategy_id"),
            "generated_message": review_result.get("student_message") or review_result.get("message"),
            "decision": review_result.get("decision"),
            "teacher_reason": review_result.get("teacher_reason") or review_result.get("reason"),
            "prompt_version": review_result.get("prompt_version") or STRATEGY_REVIEW_PROMPT_VERSION,
            "model_profile": review_result.get("profile") or STRATEGY_REVIEW_PROFILE,
            "llm_context_json": InterventionService._json_for_db(context),
            "llm_prompt_json": InterventionService._json_for_db(review_result.get("payload")),
            "llm_response_json": InterventionService._json_for_db(review_result),
            "validation_json": InterventionService._json_for_db(review_result.get("validation")),
            "actual_started_at": started_at,
            "generator_params_json": InterventionService._json_for_db(
                {
                    "profile": review_result.get("profile") or STRATEGY_REVIEW_PROFILE,
                    "attempt_count": review_result.get("attempt_count"),
                    "max_attempts": review_result.get("max_attempts"),
                    "attempts": review_result.get("attempts") or [],
                    "final_status": review_result.get("final_status"),
                    "failure_stage": review_result.get("failure_stage"),
                    "timeout_type": review_result.get("timeout_type"),
                }
            ),
        }
        if completed_at:
            extra["validated_at"] = completed_at
        if llm_meta.get("latency_ms") is not None:
            extra["latency_ms"] = llm_meta.get("latency_ms")
        elif review_result.get("elapsed_ms") is not None:
            extra["latency_ms"] = review_result.get("elapsed_ms")
        if review_result.get("reason") and not review_result.get("ok", True):
            extra["validation_error"] = review_result.get("reason")
            extra["failure_reason"] = review_result.get("reason")

        if status:
            InterventionRunRepo.set_status(run_id, status, **extra)
        else:
            InterventionRunRepo.set_status(
                run_id,
                InterventionRunRepo.get(run_id)["status"],
                **extra,
            )

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    @staticmethod
    def _publish(
        group_id: int,
        intervention_run_id: int,
        lock_token: str,
        message: str,
        strategy_id: str = None,
        trigger_source: str = "auto_v2",
        prompt_version: str = STRATEGY_REVIEW_PROMPT_VERSION,
        session_id: int = None,
        task_id: int = None,
        session_no: int = None,
        teacher_reason: str = None,
    ) -> dict:
        """在短事务中发布介入消息。

        必须：
        1. 验证 lock_token；
        2. 插入唯一 AI 消息；
        3. 更新 intervention_run；
        4. 清空 active_intervention_run_id；
        5. 清空 lock_token 和 lock_expires_at；
        6. 将房间改回 OPEN；
        7. version + 1；
        8. 提交。
        """
        from auth import get_sera_user_id
        from services.agent_intervention_publisher import publish_agent_intervention

        publish_result = publish_agent_intervention(
            group_id=group_id,
            intervention_run_id=intervention_run_id,
            lock_token=lock_token,
            message=message,
            strategy_id=strategy_id,
            trigger_source=trigger_source or "auto_state",
            prompt_version=prompt_version,
            session_id=session_id,
            task_id=task_id,
            session_no=session_no,
            teacher_reason=teacher_reason,
            agent_type="strategy",
            push_mode="sera_auto_v2",
            expected_lock_owner_run_id=intervention_run_id,
        )
        if not publish_result.get("ok"):
            return publish_result

        if lock_token:
            _log_strategy_event(
                "room_lease_released",
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                intervention_run_id=intervention_run_id,
                trigger_source=trigger_source or "auto_state",
                publish_message_id=publish_result.get("message_id"),
                final_status=publish_result.get("status") or "PUBLISHED",
            )

        strategy_type_info = None
        if strategy_id:
            try:
                from services.intervention_pipeline_v2.strategy_service import StrategyService

                strategy = StrategyService.get_strategy(strategy_id)
                if strategy:
                    strategy_type_info = strategy.get("strategy_type")
            except Exception:
                pass

        created_at = now_str()
        trigger_source_for_audit = publish_result.get("trigger_source") or trigger_source or "auto_state"
        sera_user_id = get_sera_user_id()
        InterventionService._safe_record_post_publish_events(
            group_id=group_id,
            intervention_run_id=intervention_run_id,
            strategy_id=strategy_id,
            strategy_type_info=strategy_type_info,
            session_id=session_id,
            task_id=task_id,
            session_no=session_no,
            sera_user_id=sera_user_id,
            created_at=created_at,
            trigger_source=trigger_source_for_audit,
        )
        safe_write_audit_log(
            action_type="intervention.published",
            actor_type="system",
            actor_id="intervention_v2",
            target_type="intervention_run",
            target_id=intervention_run_id,
            metadata={
                "intervention_id": intervention_run_id,
                "group_id": group_id,
                "strategy_id": strategy_id,
                "strategy_type": strategy_type_info,
                "trigger_source": trigger_source_for_audit,
                "published_to": str(sera_user_id),
                "created_at": created_at,
            },
        )
        return {
            "ok": True,
            "message_id": publish_result.get("message_id"),
            "intervention_run_id": publish_result.get("intervention_run_id"),
            "intervention_log_id": publish_result.get("intervention_log_id"),
            "duplicate": publish_result.get("duplicate"),
        }

        sera_user_id = get_sera_user_id()
        if not sera_user_id:
            logger.error("SERA user not found, cannot publish message")
            return {"ok": False, "reason": "sera_user_not_found"}

        conn = db()
        try:
            # 1. 验证 lock_token
            room = conn.execute(
                "SELECT lock_token, version FROM groups WHERE id=?",
                (group_id,),
            ).fetchone()
            if not room or room["lock_token"] != lock_token:
                logger.warning("lock_token mismatch for group %s, aborting publish", group_id)
                conn.close()
                return {"ok": False, "reason": "lock_token_mismatch"}

            current_version = int(room["version"])

            # 2. 插入 AI 消息（使用 client_message_id 去重）
            from db import now_str as ns
            now = ns()
            from uuid import uuid4
            client_msg_id = f"agent-v2-{intervention_run_id}-{uuid4().hex[:8]}"
            from db import get_active_session_id, get_runtime_message_context
            _session_id = session_id or get_active_session_id()
            _rt_ctx = get_runtime_message_context()
            _session_no = session_no if session_no is not None else _rt_ctx.get("session_no", 0)
            _task_id = task_id if task_id is not None else _rt_ctx.get("task_id")

            msg_cur = conn.execute(
                """INSERT INTO messages(group_id, user_id, content, role, client_message_id, intervention_run_id, sequence, created_at, session_no, task_id, session_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    sera_user_id,
                    message,
                    "agent",
                    client_msg_id,
                    intervention_run_id,
                    # Get next sequence
                    (conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE group_id=?", (group_id,)).fetchone()[0]),
                    now,
                    _session_no,
                    _task_id,
                    _session_id,
                ),
            )
            message_id = msg_cur.lastrowid
            conn.execute(
                "UPDATE messages SET agent_type='strategy', strategy_id=? WHERE intervention_run_id=?",
                (strategy_id, intervention_run_id),
            )
            # Sync groups.last_message_sequence to match actual max sequence
            conn.execute(
                "UPDATE groups SET last_message_sequence = MAX(COALESCE(last_message_sequence,0), (SELECT MAX(sequence) FROM messages WHERE group_id=?)) WHERE id = ?",
                (group_id, group_id),
            )

            # 3. 更新 intervention_run
            conn.execute(
                """UPDATE intervention_runs
                   SET status='PUBLISHED',
                       decision='INTERVENE',
                       generated_message=?,
                       selected_strategy_id=?,
                       strategy_id=?,
                       teacher_reason=?,
                       message_id=?,
                       prompt_version=?,
                       generated_at=?,
                       published_at=?,
                       actual_published_at=?,
                       completed_at=?,
                       lock_token=?
                   WHERE id=?""",
                (
                    message,
                    strategy_id,
                    strategy_id,
                    teacher_reason,
                    message_id,
                    prompt_version,
                    now,
                    now,
                    now,
                    now,
                    lock_token,
                    intervention_run_id,
                ),
            )
            linked_segment = conn.execute(
                "SELECT target_segment_id FROM intervention_runs WHERE id=?",
                (intervention_run_id,),
            ).fetchone()
            if linked_segment and linked_segment["target_segment_id"]:
                conn.execute(
                    """
                    UPDATE collaboration_state_segments
                    SET intervention_run_id=?,
                        intervention_published_at=?,
                        intervention_disposition='PUBLISHED',
                        updated_at=?
                    WHERE id=? AND state_code='negative_silence'
                      AND source='silence_rule'
                    """,
                    (
                        intervention_run_id,
                        now,
                        now,
                        linked_segment["target_segment_id"],
                    ),
                )

            # ---- 3.5 Insert intervention_logs (for export/audit chain) ----
            try:
                run_row = conn.execute(
                    "SELECT detected_state, model_profile FROM intervention_runs WHERE id=?",
                    (intervention_run_id,),
                ).fetchone()

                strategy = None
                if strategy_id:
                    from services.intervention_pipeline_v2.strategy_service import StrategyService
                    strategy = StrategyService.get_strategy(strategy_id)

                group_row = conn.execute(
                    "SELECT condition FROM groups WHERE id=?", (group_id,)
                ).fetchone()
                _condition_2 = group_row["condition"] if group_row else None

                prev_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM intervention_logs WHERE group_id=?", (group_id,)
                ).fetchone()
                intervention_index = (prev_count["c"] or 0) + 1

                _title = (strategy.get("goal") or strategy.get("id") or strategy_id or "auto")
                _sub_category = strategy.get("sub_category") if strategy else None
                _strategy_type = strategy.get("strategy_type") if strategy else None
                _strategy_version = strategy.get("version") if strategy else None
                _model_name = run_row["model_profile"] if run_row else None

                conn.execute(
                    """INSERT INTO intervention_logs(
                        group_id, intervention_id, pushed_by_user_id, push_mode,
                        title, message, condition, trigger_source,
                        strategy_id, template_id, sub_category, strategy_type,
                        strategy_version, model_name, prompt_version,
                        session_id, task_id, intervention_index, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        group_id,
                        intervention_run_id,
                        sera_user_id,
                        "sera_auto_v2",
                        _title,
                        message,
                        _condition_2,
                        trigger_source or "auto_v2",
                        strategy_id,
                        None,
                        _sub_category,
                        _strategy_type,
                        _strategy_version,
                        _model_name,
                        prompt_version,
                        _session_id,
                        _task_id,
                        intervention_index,
                        now,
                    ),
                )
            except Exception as log_exc:
                logger.warning(
                    "Failed to write intervention_logs for run %s: %s",
                    intervention_run_id, log_exc,
                )

                        # 4. 更新房间：清空锁状态，回到 OPEN，增加 version
            conn.execute(
                """UPDATE groups
                   SET state=?, version=version+1,
                       lock_token=NULL, lock_expires_at=NULL,
                       active_intervention_run_id=NULL,
                       last_intervention_at=?
                   WHERE id=? AND lock_token=?""",
                (RoomLeaseService.OPEN_STATE, now, group_id, lock_token),
            )

            conn.commit()

            # ---- Compute strategy_type_info for downstream use ----
            strategy_type_info = None
            if strategy_id:
                try:
                    from services.intervention_pipeline_v2.strategy_service import StrategyService
                    _s = StrategyService.get_strategy(strategy_id)
                    if _s:
                        strategy_type_info = _s.get("strategy_type")
                except Exception:
                    pass

            # ---- Record process_events and initial intervention_uptake (safe, non-blocking) ----
            InterventionService._safe_record_post_publish_events(
                group_id=group_id,
                intervention_run_id=intervention_run_id,
                strategy_id=strategy_id,
                strategy_type_info=strategy_type_info,
                session_id=_session_id,
                task_id=_task_id,
                session_no=_session_no,
                sera_user_id=sera_user_id,
                created_at=now,
                trigger_source=trigger_source or "auto_v2",
            )

            # Audit: intervention published
            safe_write_audit_log(
                action_type="intervention.published",
                actor_type="system",
                actor_id="intervention_v2",
                target_type="intervention_run",
                target_id=intervention_run_id,
                metadata={
                    "intervention_id": intervention_run_id,
                    "group_id": group_id,
                    "strategy_id": strategy_id,
                    "strategy_type": strategy_type_info,
                    "trigger_source": trigger_source or "auto_v2",
                    "published_to": str(sera_user_id),
                    "created_at": now,
                },
            )
            return {"ok": True, "message_id": message_id}

        except Exception as exc:
            conn.rollback()
            logger.exception("Publish intervention failed for run %s", intervention_run_id)
            return {"ok": False, "reason": "publish_failed", "error": str(exc)}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 超时恢复
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Post-publish recording helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_record_post_publish_events(
        group_id, intervention_run_id, strategy_id, strategy_type_info,
        session_id, task_id, session_no, sera_user_id, created_at,
        trigger_source="auto_v2",
    ):
        """Record process_events and intervention_uptake after successful publish.

        Both writes are independently wrapped in try/except so that failures
        in event or uptake recording never break the main publish flow.
        """
        # Process event
        try:
            from db import record_process_event
            record_process_event(
                "intervention_published",
                source="system",
                group_id=group_id,
                user_id=sera_user_id,
                session_no=session_no,
                task_id=task_id,
                related_table="intervention_runs",
                related_id=intervention_run_id,
                event_key=f"intervention_published:{intervention_run_id}",
                payload={
                    "intervention_id": intervention_run_id,
                    "strategy_id": strategy_id,
                    "strategy_type": strategy_type_info,
                    "trigger_source": trigger_source,
                    "session_id": session_id,
                },
                created_at=created_at,
            )
        except Exception as exc:
            logger.warning(
                "Failed to record process_event intervention_published for run %s: %s",
                intervention_run_id, exc,
            )

        # Initial intervention_uptake record
        try:
            from db import execute as _db_execute
            import json as _json
            _db_execute(
                """INSERT INTO intervention_uptake(
                    intervention_id, group_id, session_id, auto_uptake_type,
                    target_ssrl_behavior, state_before
                ) VALUES(?,?,?,?,?,?)""",
                (
                    intervention_run_id,
                    group_id,
                    session_id,
                    "not_evaluated",
                    strategy_id,
                    _json.dumps({
                        "evaluation_window_seconds": 120,
                        "intervention_id": intervention_run_id,
                        "group_id": group_id,
                        "strategy_id": strategy_id,
                        "strategy_type": strategy_type_info,
                    }, ensure_ascii=False),
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to record initial intervention_uptake for run %s: %s",
                intervention_run_id, exc,
            )

    @staticmethod
    def _schedule_release_expired(
        group_id: int,
        lock_token: str,
        *,
        delay_seconds: int = None,
    ):
        """安排高优先级延迟任务：约 lock_seconds 秒后执行释放。"""
        try:
            from huey_instance import huey
            from agent.intervention_tasks import release_expired_intervention

            release_expired_intervention.schedule(
                args=(group_id, lock_token),
                delay=int(delay_seconds or INTERVENTION_V2_LOCK_SECONDS),
                priority=10,  # 高优先级
            )
        except Exception as exc:
            logger.warning("Failed to schedule release_expired_intervention: %s", exc)

    # ------------------------------------------------------------------
    # 超时释放处理器
    # ------------------------------------------------------------------
    @staticmethod
    def handle_release_expired(group_id: int, lock_token: str) -> bool:
        """处理超时释放请求。"""
        from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
        return RoomLeaseService.release_expired(group_id, lock_token)
