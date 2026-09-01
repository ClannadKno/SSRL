# -*- coding: utf-8 -*-
"""
Huey tasks for V2 automatic intervention.

Each task pushes its own Flask application context and creates an
independent database session.
"""
import logging
from config import SSRL_AGENT_DEBUG

from huey_instance import huey
from core import app

logger = logging.getLogger(__name__)


@huey.task()
def intervention_v2_smoke():
    """Smoke test for v2 intervention tasks."""
    with app.app_context():
        return "intervention_v2_smoke ok"


@huey.task(priority=10)
def release_expired_intervention(group_id: int, lock_token: str):
    """超时恢复：释放过期房间锁。

    使用 lock_token 做条件解锁，确保旧 token 不得解锁新的介入。
    由 InterventionService 在获取锁后安排，约 lock_seconds 秒后执行。
    """
    with app.app_context():
        try:
            from services.intervention_pipeline_v2.intervention_service import InterventionService
            ok = InterventionService.handle_release_expired(group_id, lock_token)
            if ok:
                logger.info("Released expired lock for group %s (token=%s...)", group_id, lock_token[:8])
            else:
                # lock_token 不匹配或房间已解锁（正常流程已释放），均为正常
                logger.info("Lock already released for group %s or token mismatch (token=%s...)", group_id, lock_token[:8])
            return {"group_id": group_id, "released": ok}
        except Exception as exc:
            logger.exception("release_expired_intervention failed for group %s", group_id)
            return {"error": str(exc)}


@huey.task(priority=30)
def execute_intervention_v2(
    monitor_run_id: int,
    state_assessment_id: int = None,
    group_id: int = None,
    session_id: int = None,
    task_id: int = None,
    cutoff_sequence: int = None,
    trigger_source: str = "auto_state",
):
    """从已完成的 monitor_run / state_assessment 执行 V2 自动介入。

    流程：
    1. 检查 Feature Flag；
    2. 读取并校验 state_assessment；
    3. 创建 intervention_run（PENDING/SKIPPED）；
    4. 获取房间锁；
    5. 构建上下文；
    6. 调用统一策略复核 LLM；
    8. PASS 只审计并解锁；
    9. INTERVENE 发布。
    """
    with app.app_context():
        try:
            if SSRL_AGENT_DEBUG:
                logger.info(
                    "[SSRL_AGENT] execute_intervention_v2 ENTER monitor_run_id=%s state_assessment_id=%s",
                    monitor_run_id,
                    state_assessment_id,
                )
            from services.intervention_pipeline_v2.intervention_service import InterventionService
            result = InterventionService.execute(
                monitor_run_id,
                state_assessment_id=state_assessment_id,
                group_id=group_id,
                session_id=session_id,
                task_id=task_id,
                cutoff_sequence=cutoff_sequence,
                trigger_source=trigger_source or "auto_state",
            )
            if SSRL_AGENT_DEBUG:
                skipped = result.get("skipped", False)
                skip_reason = result.get("reason", "N/A")
                steps = result.get("steps", {})
                intervention_run_id = result.get("intervention_run_id")
                published = result.get("published")
                logger.info("[SSRL_AGENT] execute_intervention_v2 DONE monitor_run_id=%s skipped=%s reason=%s steps=%s run_id=%s published=%s",
                    monitor_run_id, skipped, skip_reason, steps, intervention_run_id, published)
            if result.get("dry_run"):
                logger.info(
                    "[V2 DRY-RUN] monitor_run=%s group=%s run_id=%s strategies=%s",
                    monitor_run_id,
                    result.get("intervention_run_id"),
                    result.get("candidate_strategies"),
                )
            return result
        except Exception as exc:
            logger.exception("execute_intervention_v2 failed for monitor_run %s", monitor_run_id)
            return {"error": str(exc)}

@huey.task()
def intervention_smoke():
    """Smoke test – verifies the intervention_tasks module is importable
    and tasks can execute under the Flask application context."""
    with app.app_context():
         return "intervention_smoke ok"


@huey.task()
def check_intervention_db():
    """Quick connectivity check against the business database.
    Opens and closes its own connection – no request session is reused."""
    with app.app_context():
         from db import db
         conn = db()
         try:
             conn.execute("SELECT 1").fetchone()
             conn.commit()
         finally:
             conn.close()
         return {"module": "intervention", "db_ok": True}


