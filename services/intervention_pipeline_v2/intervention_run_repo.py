# -*- coding: utf-8 -*-
"""intervention_runs 表数据访问层 — V2 管线专用。"""
import json
from datetime import datetime
from typing import Optional

from db import db, execute, now_str, parse_dt, query_one, query_all


class InterventionRunRepo:
    """intervention_runs 表的 CRUD 与状态转换。

    V2 状态流:
      PENDING → REVALIDATING → LOCKED → GENERATING → VALIDATING → PUBLISHED
                                                              ↘ FALLBACK
      PENDING → PASS (状态存在但本次不发言)
      PENDING → SKIPPED (门禁/冷却/主动求助/幂等跳过)
      PENDING → STALE (过期不介入)
      PENDING → CANCELLED
      PUBLISHED → (terminal)
      FALLBACK → (terminal)
      PASS → (terminal)
      SKIPPED → (terminal)
      FAILED → (terminal)
      EXPIRED → (terminal)
    """

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @staticmethod
    def get(run_id: int) -> Optional[dict]:
        row = query_one("SELECT * FROM intervention_runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    @staticmethod
    def get_by_group_and_cutoff(
        group_id: int,
        cutoff_sequence: int,
        trigger_type: str = "auto_state",
    ) -> Optional[dict]:
        row = query_one(
            """
            SELECT * FROM intervention_runs
            WHERE group_id=?
              AND COALESCE(cutoff_sequence, 0)=COALESCE(?, 0)
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND COALESCE(trigger_type, 'auto_state')=?
            """,
            (group_id, cutoff_sequence, trigger_type or "auto_state"),
        )
        return dict(row) if row else None

    @staticmethod
    def get_by_assessment(
        group_id: int,
        session_id: int,
        state_assessment_id: int,
        trigger_type: str = "auto_state",
    ) -> Optional[dict]:
        if not state_assessment_id:
            return None
        row = query_one(
            """
            SELECT * FROM intervention_runs
            WHERE group_id=?
              AND (? IS NULL OR session_id=?)
              AND state_assessment_id=?
              AND COALESCE(trigger_type, 'auto_state')=?
              AND COALESCE(agent_type, 'strategy')='strategy'
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_id, session_id, session_id, state_assessment_id, trigger_type or "auto_state"),
        )
        return dict(row) if row else None

    @staticmethod
    def get_active_run_for_room(group_id: int) -> Optional[dict]:
        """查询 room 上当前 active 的 intervention_run（未终止的状态）。"""
        row = query_one(
            """
            SELECT * FROM intervention_runs
            WHERE group_id=?
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND status NOT IN ('PUBLISHED','FALLBACK','PASS','SKIPPED','FAILED','EXPIRED','CANCELLED','STALE')
            ORDER BY id DESC LIMIT 1
            """,
            (group_id,),
        )
        return dict(row) if row else None

    @staticmethod
    def get_valid_monitor_runs_for_room(group_id: int) -> list:
        """查询 room 上已完成的 monitor_run，按 id 降序。"""
        return query_all(
            "SELECT * FROM monitor_runs WHERE group_id=? AND status='completed' ORDER BY id DESC",
            (group_id,),
        )

    @staticmethod
    def count_recent_by_strategy(group_id: int, strategy_id: str, since: str) -> int:
        row = query_one(
            """
            SELECT COUNT(*) AS c FROM intervention_runs
            WHERE group_id=? AND strategy_id=? AND created_at>=?
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND status IN ('PUBLISHED','FALLBACK')
            """,
            (group_id, strategy_id, since),
        )
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------
    @staticmethod
    def create(
        group_id: int,
        monitor_run_id: int,
        cutoff_sequence: int,
        detected_state: str = None,
        confidence: float = None,
        dry_run: bool = True,
        trigger_type: str = None,
        metadata: dict = None,
        state_assessment_id: int = None,
        session_id: int = None,
        session_no: int = None,
        discussion_id: int = None,
        task_id: int = None,
        target_segment_id: int = None,
    ) -> int:
        conn = db()
        try:
            if target_segment_id is not None:
                target = conn.execute(
                    """
                    SELECT session_id, session_no, task_id, discussion_id,
                           start_sequence
                    FROM collaboration_state_segments
                    WHERE id=? AND group_id=?
                    """,
                    (target_segment_id, group_id),
                ).fetchone()
                if target:
                    session_id = target["session_id"] or session_id
                    session_no = target["session_no"] or session_no
                    task_id = target["task_id"] or task_id
                    discussion_id = target["discussion_id"] or discussion_id
            from services.discussion_scope import resolve_discussion_scope

            scope = resolve_discussion_scope(
                conn,
                group_id=group_id,
                sequence=cutoff_sequence,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
                allow_legacy_fallback=False,
            )
            cur = conn.execute(
                """INSERT INTO intervention_runs(
                    group_id, monitor_run_id, cutoff_sequence, detected_state, confidence,
                    status, dry_run, agent_type, trigger_type, metadata_json,
                    state_assessment_id, session_id, session_no, discussion_id,
                    task_id, target_segment_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    monitor_run_id,
                    cutoff_sequence,
                    detected_state,
                    confidence,
                    "PENDING",
                    1 if dry_run else 0,
                    "strategy",
                    trigger_type,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                    state_assessment_id,
                    scope.session_id,
                    scope.session_no,
                    scope.discussion_id,
                    scope.task_id,
                    target_segment_id,
                    now_str(),
                ),
            )
            conn.commit()
            run_id = cur.lastrowid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        InterventionRunRepo._sync_silence_segment(
            run_id,
            status="PENDING",
        )
        return run_id

    # ------------------------------------------------------------------
    # 状态转换
    # ------------------------------------------------------------------
    @staticmethod
    def transition(run_id: int, from_status: str, to_status: str) -> bool:
        """原子状态转换。返回 True 表示转换成功。"""
        conn = db()
        cur = conn.execute(
            "UPDATE intervention_runs SET status=? WHERE id=? AND status=?",
            (to_status, run_id, from_status),
        )
        affected = cur.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    @staticmethod
    def set_status(run_id: int, status: str, **extra_fields):
        """无条件设置状态并附加字段。"""
        fields = ["status=?"]
        params = [status]
        for key, value in extra_fields.items():
            fields.append(f"{key}=?")
            params.append(value)
        params.append(run_id)
        execute(
            f"UPDATE intervention_runs SET {', '.join(fields)} WHERE id=?",
            tuple(params),
        )
        InterventionRunRepo._sync_silence_segment(run_id, status=status)

    @staticmethod
    def _sync_silence_segment(run_id: int, *, status: str = None, published_at: str = None):
        """Mirror strategy-run disposition onto a linked negative-silence event."""
        run = query_one(
            """
            SELECT target_segment_id, status, published_at, actual_published_at
            FROM intervention_runs
            WHERE id=?
            """,
            (run_id,),
        )
        if not run or not run["target_segment_id"]:
            return
        effective_status = status or run["status"] or "UNKNOWN"
        effective_published_at = (
            published_at
            or run["actual_published_at"]
            or run["published_at"]
        )
        execute(
            """
            UPDATE collaboration_state_segments
            SET intervention_run_id=?,
                intervention_disposition=?,
                intervention_published_at=CASE
                    WHEN ? IN ('PUBLISHED','FALLBACK')
                    THEN COALESCE(?, intervention_published_at)
                    ELSE intervention_published_at
                END,
                updated_at=?
            WHERE id=? AND state_code='negative_silence'
              AND source='silence_rule'
            """,
            (
                run_id,
                effective_status,
                effective_status,
                effective_published_at,
                now_str(),
                run["target_segment_id"],
            ),
        )

    @staticmethod
    def transition_to_locked(run_id: int, lock_token: str, lock_expires_at: str) -> bool:
        """PENDING → LOCKED。两步：先原子转换，再设置锁字段。"""
        if not InterventionRunRepo.transition(run_id, "PENDING", "LOCKED"):
            return False
        InterventionRunRepo.set_status(
            run_id, "LOCKED", lock_token=lock_token, lock_expires_at=lock_expires_at,
        )
        return True

    @staticmethod
    def mark_stale(run_id: int, reason: str = None):
        InterventionRunRepo.set_status(
            run_id,
            "STALE",
            decision="SKIPPED",
            skip_reason=reason,
            failure_reason=reason,
            completed_at=now_str(),
        )

    @staticmethod
    def mark_failed(run_id: int, reason: str = None):
        InterventionRunRepo.set_status(
            run_id,
            "FAILED",
            decision="FAILED",
            failure_reason=reason,
            completed_at=now_str(),
        )

    @staticmethod
    def mark_skipped(run_id: int, reason: str = None):
        InterventionRunRepo.set_status(
            run_id,
            "SKIPPED",
            decision="SKIPPED",
            skip_reason=reason,
            completed_at=now_str(),
        )

    @staticmethod
    def mark_pass(run_id: int, teacher_reason: str = None):
        InterventionRunRepo.set_status(
            run_id,
            "PASS",
            decision="PASS",
            teacher_reason=teacher_reason,
            skip_reason="pass_no_intervention",
            generated_message=None,
            message_id=None,
            completed_at=now_str(),
        )

    @staticmethod
    def mark_fallback(run_id: int, message: str = None, fallback_template: str = None):
        InterventionRunRepo.set_status(
            run_id, "FALLBACK",
            generated_message=message,
            fallback_used=1,
            fallback_template=fallback_template,
            completed_at=now_str(),
        )

    @staticmethod
    def mark_published(run_id: int, message: str, lock_token: str):
        now = now_str()
        execute(
            """UPDATE intervention_runs
               SET status='PUBLISHED', generated_message=?, published_at=?, completed_at=?
               WHERE id=? AND lock_token=?""",
            (message, now, now, run_id, lock_token),
        )
        InterventionRunRepo._sync_silence_segment(
            run_id,
            status="PUBLISHED",
            published_at=now,
        )
