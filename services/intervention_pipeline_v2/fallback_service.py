# -*- coding: utf-8 -*-
"""回退服务：LLM 失败或校验失败时使用回退模板。"""
from typing import Optional

from config import LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED
from db import db, execute, now_str
from services.intervention_pipeline_v2.strategy_service import StrategyService


class FallbackService:
    """回退服务。

    处理场景：
    - LLM 调用失败或超时；
    - 校验失败（内容违规、长度超限等）；
    - 确保房间不会一直保持锁定。
    """

    @staticmethod
    def get_fallback_message(strategy_id: str = None, strategy: dict = None, condition: str = None) -> str:
        """获取指定策略的回退模板消息。"""
        if strategy:
            if condition == "control" and strategy.get("fallback_template_control"):
                return strategy["fallback_template_control"]
            return strategy.get("fallback_template", "")
        if strategy_id:
            s = StrategyService.get_strategy(strategy_id)
            if s:
                if condition == "control" and s.get("fallback_template_control"):
                    return s["fallback_template_control"]
                return s.get("fallback_template", "")
        return ""
    
    @staticmethod
    def apply_fallback(
        intervention_run_id: int,
        group_id: int,
        strategy_id: str = None,
        strategy: dict = None,
        lock_token: str = None,
        reason: str = "llm_failure",
        condition: str = None,
    ) -> bool:
        """应用回退。

        1. 查找回退模板；
        2. 标记 intervention_run 为 FALLBACK；
        3. 释放房间锁（dry-run 时不锁房，所以也不用解锁）；
        4. 如果是正式模式，插入 AI 消息。

        Returns True 表示回退成功应用。
        """
        message = FallbackService.get_fallback_message(strategy_id=strategy_id, strategy=strategy, condition=condition)
        fallback_template = strategy.get("fallback_template") if strategy else None

        # Check if dry run
        from db import query_one
        run = query_one("SELECT dry_run FROM intervention_runs WHERE id=?", (intervention_run_id,))
        is_dry_run = bool(run and run["dry_run"])

        now = now_str()
        if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
            execute(
                """UPDATE intervention_runs
                   SET status='FAILED', fallback_used=0,
                       failure_reason=?, completed_at=?
                   WHERE id=?""",
                ("legacy_direct_fallback_publish_disabled", now, intervention_run_id),
            )
            if not is_dry_run and lock_token:
                from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
                RoomLeaseService.release(group_id, lock_token)
            return False

        if not is_dry_run and lock_token and message:
            if FallbackService._publish_fallback_message(
                intervention_run_id=intervention_run_id,
                group_id=group_id,
                message=message,
                lock_token=lock_token,
                fallback_template=fallback_template or strategy_id,
                reason=reason,
            ):
                return True

        execute(
            """UPDATE intervention_runs
               SET status='FALLBACK', generated_message=?, fallback_used=1,
                   fallback_template=?, failure_reason=?, completed_at=?
               WHERE id=?""",
            (message, fallback_template or strategy_id, reason[:500], now, intervention_run_id),
        )

        if not is_dry_run and lock_token:
            from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
            RoomLeaseService.release(group_id, lock_token)

        return True

    @staticmethod
    def _publish_fallback_message(
        *,
        intervention_run_id: int,
        group_id: int,
        message: str,
        lock_token: str,
        fallback_template: str = None,
        reason: str = None,
    ) -> bool:
        """Publish the fallback text students should see, while preserving FALLBACK status."""
        from auth import get_sera_user_id
        from db import get_runtime_message_context
        from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

        sera_user_id = get_sera_user_id()
        if not sera_user_id:
            return False

        now = now_str()
        runtime = get_runtime_message_context()
        client_message_id = f"agent-v2-fallback-{intervention_run_id}"

        conn = db()
        try:
            run = conn.execute(
                """
                SELECT session_id, session_no, task_id, discussion_id
                FROM intervention_runs
                WHERE id=? AND group_id=?
                """,
                (intervention_run_id, group_id),
            ).fetchone()
            from services.discussion_scope import resolve_discussion_scope

            scope = resolve_discussion_scope(
                conn,
                group_id=group_id,
                session_id=run["session_id"] if run else None,
                session_no=run["session_no"] if run else runtime.get("session_no"),
                task_id=run["task_id"] if run else runtime.get("task_id"),
                discussion_id=run["discussion_id"] if run else None,
                allow_legacy_fallback=False,
            )
            room = conn.execute(
                "SELECT lock_token FROM groups WHERE id=?",
                (group_id,),
            ).fetchone()
            if not room or room["lock_token"] != lock_token:
                return False

            existing = conn.execute(
                "SELECT id FROM messages WHERE group_id=? AND user_id=? AND client_message_id=? LIMIT 1",
                (group_id, sera_user_id, client_message_id),
            ).fetchone()
            if not existing:
                next_sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE group_id=?",
                    (group_id,),
                ).fetchone()[0]
                conn.execute(
                    """INSERT INTO messages(
                        group_id, user_id, content, role, client_message_id,
                        intervention_run_id, sequence, created_at,
                        session_no, task_id, session_id, discussion_id,
                        sender_type, agent_type, trigger_source,
                        scope_resolved_from, legacy_scope_fallback,
                        scope_fallback_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        group_id,
                        sera_user_id,
                        message,
                        "agent",
                        client_message_id,
                        intervention_run_id,
                        next_sequence,
                        now,
                        scope.session_no,
                        scope.task_id,
                        scope.session_id,
                        scope.discussion_id,
                        "agent",
                        "strategy",
                        "fallback",
                        scope.resolved_from,
                        1 if scope.is_legacy_fallback else 0,
                        scope.fallback_reason,
                    ),
                )

            conn.execute(
                "UPDATE groups SET last_message_sequence = MAX(COALESCE(last_message_sequence,0), (SELECT MAX(sequence) FROM messages WHERE group_id=?)) WHERE id=?",
                (group_id, group_id),
            )
            conn.execute(
                """UPDATE intervention_runs
                   SET status='FALLBACK', generated_message=?, fallback_used=1,
                       fallback_template=?, failure_reason=?, published_at=?, completed_at=?
                   WHERE id=?""",
                (message, fallback_template, (reason or "")[:500], now, now, intervention_run_id),
            )
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
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    @staticmethod
    def handle_intervention_error(
        intervention_run_id: int,
        group_id: int,
        error: Exception,
        strategy_id: str = None,
        strategy: dict = None,
        lock_token: str = None,
        condition: str = None,
    ) -> bool:
        """处理介入过程中的异常。"""
        reason = f"{error.__class__.__name__}: {str(error)[:300]}"
        return FallbackService.apply_fallback(
            intervention_run_id=intervention_run_id,
            group_id=group_id,
            strategy_id=strategy_id,
            strategy=strategy,
            lock_token=lock_token,
            reason=reason,
            condition=condition,
        )
