# -*- coding: utf-8 -*-
"""
Huey tasks for group-state monitoring and periodic analysis (V2 管线).

Each task pushes its own Flask application context and creates an
independent database session.  No request-scoped state is reused.
"""
import logging
import json
from config import SSRL_AGENT_DEBUG
from huey import crontab

from huey_instance import huey
from core import app

logger = logging.getLogger(__name__)


def _task_log(event: str, **fields):
    logger.info(
        "[monitoring_task] %s %s",
        event,
        json.dumps(
            {key: value for key, value in fields.items() if value is not None},
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ),
    )


@huey.task()
def monitoring_smoke():
    """Smoke test – verifies the monitoring_tasks module is importable
    and tasks can execute under the Flask application context."""
    with app.app_context():
        return "monitoring_smoke ok"


@huey.task()
def check_monitoring_db():
    """Quick connectivity check against the business database."""
    with app.app_context():
        from db import db
        conn = db()
        try:
            conn.execute("SELECT 1").fetchone()
            conn.commit()
        finally:
            conn.close()
        return {"module": "monitoring", "db_ok": True}


@huey.task(priority=50)
def process_new_message_task(room_id: int, sequence: int, trigger_type: str = "new_message"):
    """普通消息写入成功后提交的监测任务（低优先级）。

    流程（不直接锁房或发布助手消息）：
    1. 执行不调用 LLM 的规则前置判定；
    2. 持久化可信规则状态，并将负面状态交给独立介入 guard；
    3. 将消息数、规则强触发、求助或介入后观察统一提交给
       request_state_assessment；
    4. 仅由获胜的 immutable batch 入队并在 batch worker 中调用 LLM。
    """
    with app.app_context():
        try:
            _task_log(
                "process_new_message_start",
                group_id=room_id,
                cutoff_sequence=sequence,
                trigger_type=trigger_type,
                monitor_run_id=None,
                final_state=None,
                skip_reason=None,
            )
            if SSRL_AGENT_DEBUG:
                logger.info("[SSRL_AGENT] process_new_message_task ENTER group=%s seq=%s", room_id, sequence)
            from services.discussion_pipeline_v2.monitoring_service import MonitoringService
            result = MonitoringService.run_detection(
                group_id=room_id,
                trigger_type=trigger_type or "new_message",
                allow_state_llm=False,
                persist_state_segment=True,
                schedule_strategy=True,
            )
            from services.state_assessment_scheduler import (
                request_state_assessment_for_message,
                resolve_message_scope,
            )
            scheduler_trigger = "help_request" if trigger_type in {
                "student_help", "student_help_request", "help_request"
            } else "message_count_periodic"
            winning_state = result.get("rule_winning_state")
            gate = result.get("state_detector_gate") or {}
            if (
                scheduler_trigger != "help_request"
                and bool(gate.get("gate"))
                and winning_state not in (None, "unknown", "positive_collaboration")
            ):
                scheduler_trigger = "rule_high_risk"
            if scheduler_trigger == "message_count_periodic":
                scope = resolve_message_scope(group_id=room_id, sequence=sequence)
                if scope:
                    from services.state_assessment_batch_service import StateAssessmentBatchService

                    cursor = StateAssessmentBatchService.get_cursor(
                        group_id=scope["group_id"],
                        session_id=scope["session_id"],
                        discussion_id=scope["discussion_id"],
                    )
                    if cursor and cursor.get("observation_status") == "observing":
                        scheduler_trigger = "post_intervention_observation"
            assessment_request = request_state_assessment_for_message(
                group_id=room_id,
                sequence=sequence,
                trigger_type=scheduler_trigger,
            )
            result["state_assessment_request"] = assessment_request
            if SSRL_AGENT_DEBUG:
                summary = {k: v for k, v in result.items() if k in ("monitor_run_id", "fused_state", "fused_state_code", "fused_confidence", "no_intervention_reason", "llm_called", "skipped", "reason")}
                logger.info("[SSRL_AGENT] process_new_message_task DONE group=%s seq=%s result=%s", room_id, sequence, summary)
            if result.get("shadow"):
                logger.info(
                    "[V2 SHADOW] group=%s seq=%s run_id=%s state=%s shadow=true",
                    room_id, sequence,
                    result.get("monitor_run_id"),
                    result.get("fused_state"),
                )
            _task_log(
                "process_new_message_done",
                group_id=room_id,
                cutoff_sequence=sequence,
                trigger_type=trigger_type,
                monitor_run_id=result.get("monitor_run_id"),
                final_state=result.get("fused_state"),
                skip_reason=result.get("reason") if result.get("skipped") else result.get("no_intervention_reason"),
            )
            return result
        except Exception as exc:
            logger.exception("process_new_message_task(group=%s, seq=%s) error", room_id, sequence)
            _task_log(
                "process_new_message_error",
                group_id=room_id,
                cutoff_sequence=sequence,
                trigger_type=trigger_type,
                monitor_run_id=None,
                final_state=None,
                skip_reason="internal_error",
                error_type=exc.__class__.__name__,
            )
            return {"error": str(exc)}


@huey.task(priority=30)
def check_room_silence(
    room_id: int,
    expected_sequence: int,
    expected_last_student_message_at: str = None,
    expected_session_id: int = None,
    expected_task_id: int = None,
):
    """沉默延迟检查任务。

    达到沉默阈值后重新核对最后一条学生消息及其课次/任务作用域；
    任一快照字段变化都按过期任务跳过。Agent 消息不参与该校验。
    """
    with app.app_context():
        try:
            _task_log(
                "silence_check_start",
                group_id=room_id,
                cutoff_sequence=expected_sequence,
                trigger_type="silence_check",
                monitor_run_id=None,
                final_state=None,
                skip_reason=None,
            )
            from db import query_one
            from services.discussion_pipeline_v2.monitoring_service import MonitoringService
            from services.discussion_pipeline_v2.monitor_run_repo import MonitorRunRepo

            # 重新读取最后一条学生消息。Agent/情绪 Agent 消息不能终止学生沉默。
            group = query_one(
                "SELECT last_message_sequence, state FROM groups WHERE id=?",
                (room_id,),
            )
            if not group:
                return {"skipped": True, "reason": "room_not_found"}

            current_student = query_one(
                """
                SELECT sequence, created_at, session_id, task_id
                FROM messages
                WHERE group_id=?
                  AND sequence IS NOT NULL
                  AND COALESCE(role, '')='student'
                ORDER BY sequence DESC, id DESC
                LIMIT 1
                """,
                (room_id,),
            )
            stale_fields = {}
            current_student_seq = (
                int(current_student["sequence"]) if current_student else None
            )
            if current_student_seq != expected_sequence:
                stale_fields["actual_sequence"] = current_student_seq
            if (
                expected_last_student_message_at is not None
                and (
                    not current_student
                    or str(current_student["created_at"])
                    != str(expected_last_student_message_at)
                )
            ):
                stale_fields["actual_last_student_message_at"] = (
                    current_student["created_at"] if current_student else None
                )
            if (
                expected_session_id is not None
                and (
                    not current_student
                    or current_student["session_id"] is None
                    or int(current_student["session_id"]) != int(expected_session_id)
                )
            ):
                stale_fields["actual_session_id"] = (
                    current_student["session_id"] if current_student else None
                )
            if (
                expected_task_id is not None
                and (
                    not current_student
                    or current_student["task_id"] is None
                    or int(current_student["task_id"]) != int(expected_task_id)
                )
            ):
                stale_fields["actual_task_id"] = (
                    current_student["task_id"] if current_student else None
                )
            if stale_fields:
                stale_audit = {
                    "silence_task_revalidation": {
                        "result": "stale",
                        "reason": "stale_silence_task",
                        "expected_last_student_sequence": expected_sequence,
                        "expected_last_student_message_at": (
                            expected_last_student_message_at
                        ),
                        "expected_session_id": expected_session_id,
                        "expected_task_id": expected_task_id,
                        **stale_fields,
                    }
                }
                existing_run = MonitorRunRepo.find_by_unique_key(
                    room_id,
                    expected_sequence,
                    trigger_type="silence_check",
                )
                if existing_run:
                    run_id = int(existing_run["id"])
                    MonitorRunRepo.update_monitor_audit(run_id, stale_audit)
                else:
                    run_id = MonitorRunRepo.create(
                        room_id,
                        expected_sequence,
                        trigger_type="silence_check",
                    )
                    MonitorRunRepo.skip(
                        run_id,
                        (
                            "stale_silence_task "
                            f"expected_sequence={expected_sequence} "
                            f"actual_sequence={current_student_seq}"
                        ),
                        audit_json=stale_audit,
                    )
                return {
                    "skipped": True,
                    "reason": "stale_silence_task",
                    "monitor_run_id": run_id,
                    "expected_last_student_sequence": expected_sequence,
                    "expected_last_student_message_at": expected_last_student_message_at,
                    "expected_session_id": expected_session_id,
                    "expected_task_id": expected_task_id,
                    **stale_fields,
                }

            if group["state"] == "CLOSED":
                return {"skipped": True, "reason": "room_closed"}

            # 执行沉默检测（以 silence_check 为 trigger_type）
            result = MonitoringService.run_detection(
                group_id=room_id,
                trigger_type="silence_check",
                silence_expected_sequence=expected_sequence,
                silence_expected_message_at=expected_last_student_message_at,
                silence_expected_session_id=expected_session_id,
                silence_expected_task_id=expected_task_id,
                allow_state_llm=False,
                persist_state_segment=True,
                schedule_strategy=True,
            )
            if bool((result.get("state_detector_gate") or {}).get("gate")):
                from services.state_assessment_scheduler import request_state_assessment_for_message

                result["state_assessment_request"] = request_state_assessment_for_message(
                    group_id=room_id,
                    sequence=expected_sequence,
                    trigger_type="silence_check",
                )
            if result.get("shadow"):
                logger.info(
                    "[V2 SHADOW] silence group=%s seq=%s run_id=%s state=%s shadow=true",
                    room_id, expected_sequence,
                    result.get("monitor_run_id"),
                    result.get("fused_state"),
                )
            _task_log(
                "silence_check_done",
                group_id=room_id,
                cutoff_sequence=expected_sequence,
                trigger_type="silence_check",
                monitor_run_id=result.get("monitor_run_id"),
                final_state=result.get("fused_state"),
                skip_reason=result.get("reason") if result.get("skipped") else result.get("no_intervention_reason"),
            )
            return result
        except Exception as exc:
            logger.exception("check_room_silence(group=%s, seq=%s) error", room_id, expected_sequence)
            _task_log(
                "silence_check_error",
                group_id=room_id,
                cutoff_sequence=expected_sequence,
                trigger_type="silence_check",
                monitor_run_id=None,
                final_state=None,
                skip_reason="internal_error",
                error_type=exc.__class__.__name__,
            )
            return {"error": str(exc)}


@huey.task(priority=40)
def process_state_assessment_batch(batch_id: int):
    """Execute one immutable state-assessment batch."""
    with app.app_context():
        from services.state_assessment_scheduler import execute_state_assessment_batch

        return execute_state_assessment_batch(batch_id)


@huey.periodic_task(crontab(minute='*'))
def scan_due_state_assessments_task():
    """Single global periodic scanner for message/time assessment triggers."""
    with app.app_context():
        try:
            from services.state_assessment_scheduler import scan_due_state_assessments

            return scan_due_state_assessments()
        except Exception as exc:
            logger.exception("scan_due_state_assessments failed: %s", exc)
            return {"ok": False, "error": str(exc)}
