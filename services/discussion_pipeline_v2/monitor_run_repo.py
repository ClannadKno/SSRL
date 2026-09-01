# -*- coding: utf-8 -*-
"""monitor_runs 表数据访问层。"""
import json
from datetime import datetime, timedelta
from typing import Optional

from db import db, execute, now_str, parse_dt, query_one, query_all
from config import PIPELINE_V2_ANALYZER_VERSION


class MonitorRunRepo:
    """monitor_runs 表的 CRUD 与幂等检测。"""

    @staticmethod
    def _load_json(value, default=None):
        if not value:
            return {} if default is None else default
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else ({} if default is None else default)
        except Exception:
            return {} if default is None else default

    @staticmethod
    def _merge_monitor_audit(rule_result_json: dict = None, audit_json: dict = None) -> dict:
        payload = dict(rule_result_json or {})
        if audit_json:
            current = payload.get("monitor_audit")
            if not isinstance(current, dict):
                current = {}
            current.update(audit_json)
            payload["monitor_audit"] = current
        return payload

    @staticmethod
    def find_by_unique_key(group_id: int, cutoff_sequence: int, analyzer_version: str = None, trigger_type: str = None):
        """根据 room_id + cutoff_sequence + analyzer_version + trigger_type 查找已有记录。"""
        ver = analyzer_version or PIPELINE_V2_ANALYZER_VERSION
        trigger = trigger_type or "new_message"
        return query_one(
            """
            SELECT * FROM monitor_runs
            WHERE group_id=? AND cutoff_sequence=? AND analyzer_version=?
              AND COALESCE(trigger_type, 'new_message')=?
            """,
            (group_id, cutoff_sequence, ver, trigger),
        )

    @staticmethod
    def create(
        group_id: int,
        cutoff_sequence: int,
        trigger_type: str = "new_message",
        shadow: bool = True,
        scope: dict = None,
    ):
        """创建一条新的 monitor_run 记录。"""
        conn = db()
        try:
            if scope is None:
                from services.discussion_scope import resolve_discussion_scope

                resolved = resolve_discussion_scope(
                    conn,
                    group_id=group_id,
                    sequence=cutoff_sequence,
                    allow_legacy_fallback=False,
                )
                scope = resolved.as_dict()
            else:
                scope = dict(scope)
            cur = conn.execute(
                """
                INSERT INTO monitor_runs (
                    group_id, cutoff_sequence, trigger_type, status,
                    analyzer_version, shadow,
                    session_id, session_no, task_id, discussion_id,
                    scope_resolved_from, legacy_scope_fallback,
                    scope_fallback_reason, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id,
                    cutoff_sequence,
                    trigger_type,
                    "pending",
                    PIPELINE_V2_ANALYZER_VERSION,
                    1 if shadow else 0,
                    scope.get("session_id"),
                    scope.get("session_no"),
                    scope.get("task_id"),
                    scope.get("discussion_id"),
                    scope.get("resolved_from") or scope.get("source"),
                    1 if scope.get("is_legacy_fallback") else 0,
                    scope.get("fallback_reason"),
                    now_str(),
                ),
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def claim(run_id: int):
        """原子地将 PENDING 状态更新为 RUNNING。返回 True 表示认领成功。"""
        conn = db()
        cur = conn.execute(
            "UPDATE monitor_runs SET status='running' WHERE id=? AND status='pending'",
            (run_id,),
        )
        affected = cur.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    @staticmethod
    def complete(
        run_id: int,
        *,
        final_state: str = None,
        confidence: float = None,
        rule_result_json: dict = None,
        llm_result_json: dict = None,
        audit_json: dict = None,
        context_from_sequence: int = None,
        context_to_sequence: int = None,
        input_message_sequences: list = None,
        evidence_sequences: list = None,
    ):
        """标记 monitor_run 为 COMPLETED 并写入结果。"""
        rule_payload = MonitorRunRepo._merge_monitor_audit(rule_result_json, audit_json)
        execute(
            """
            UPDATE monitor_runs
            SET status='completed',
                final_state=?,
                confidence=?,
                rule_result_json=?,
                llm_result_json=?,
                context_from_sequence=?,
                context_to_sequence=?,
                input_message_sequences_json=?,
                evidence_sequences_json=?,
                completed_at=?
            WHERE id=?
            """,
            (
                final_state,
                confidence,
                json.dumps(rule_payload, ensure_ascii=False) if rule_payload else None,
                json.dumps(llm_result_json, ensure_ascii=False) if llm_result_json else None,
                context_from_sequence,
                context_to_sequence,
                json.dumps(input_message_sequences or [], ensure_ascii=False),
                json.dumps(evidence_sequences or [], ensure_ascii=False),
                now_str(),
                run_id,
            ),
        )

    @staticmethod
    def update_monitor_audit(run_id: int, audit_updates: dict):
        """Merge structured monitoring audit fields into rule_result_json."""
        if not audit_updates:
            return False
        row = query_one("SELECT rule_result_json FROM monitor_runs WHERE id=?", (run_id,))
        if not row:
            return False
        payload = MonitorRunRepo._load_json(row["rule_result_json"])
        payload = MonitorRunRepo._merge_monitor_audit(payload, audit_updates)
        execute(
            "UPDATE monitor_runs SET rule_result_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), run_id),
        )
        return True

    @staticmethod
    def update_strategy_review(
        run_id: int,
        *,
        context: dict = None,
        review_result: dict = None,
        started_at: str = None,
        completed_at: str = None,
        error: str = None,
    ):
        """Persist the single-call strategy review audit fields."""
        context = context or {}
        review_result = review_result or {}
        boundary = context.get("context_boundary") or {}
        decision = review_result.get("decision")
        if not decision and review_result.get("action") in {"PASS", "INTERVENE"}:
            decision = review_result.get("action")
        state_assessment = (
            review_result.get("state_assessment")
            or context.get("state_assessment")
            or context.get("confirmed_state")
            or {}
        )
        evidence_sequences = (
            review_result.get("evidence_sequences")
            or state_assessment.get("evidence_message_ids")
            or []
        )
        execute(
            """
            UPDATE monitor_runs
            SET context_from_sequence=?,
                context_to_sequence=?,
                input_message_sequences_json=?,
                evidence_sequences_json=?,
                state_assessment_id=?,
                session_id=?,
                task_id=?,
                decision=?,
                teacher_reason=?,
                review_decision=?,
                review_final_state=?,
                review_confidence=?,
                review_reason=?,
                selected_strategy_id=?,
                generated_message=?,
                prompt_version=?,
                review_started_at=?,
                review_completed_at=?,
                review_error=?
            WHERE id=?
            """,
            (
                boundary.get("from_sequence") or context.get("context_from_sequence"),
                boundary.get("to_sequence") or context.get("context_to_sequence"),
                json.dumps(context.get("input_message_sequences") or [], ensure_ascii=False),
                json.dumps(evidence_sequences, ensure_ascii=False),
                review_result.get("state_assessment_id")
                or state_assessment.get("state_assessment_id")
                or state_assessment.get("assessment_id")
                or context.get("state_assessment_id"),
                context.get("session_id"),
                context.get("task_id"),
                decision,
                review_result.get("teacher_reason") or review_result.get("reason"),
                decision,
                state_assessment.get("detected_state") or review_result.get("confirmed_state"),
                state_assessment.get("confidence") or review_result.get("confirmed_confidence"),
                review_result.get("teacher_reason") or review_result.get("reason"),
                review_result.get("strategy") or review_result.get("strategy_id"),
                review_result.get("student_message") or review_result.get("message"),
                review_result.get("prompt_version"),
                started_at,
                completed_at or now_str(),
                (error or review_result.get("reason"))[:500] if (error or not review_result.get("ok", True)) else None,
                run_id,
            ),
        )

    @staticmethod
    def fail(run_id: int, reason: str, audit_json: dict = None):
        execute(
            "UPDATE monitor_runs SET status='failed', failure_reason=?, completed_at=? WHERE id=?",
            (reason[:500], now_str(), run_id),
        )
        if audit_json:
            MonitorRunRepo.update_monitor_audit(run_id, audit_json)

    @staticmethod
    def skip(run_id: int, reason: str = None, audit_json: dict = None):
        execute(
            "UPDATE monitor_runs SET status='skipped', failure_reason=?, completed_at=? WHERE id=?",
            (reason[:500] if reason else "skipped", now_str(), run_id),
        )
        if audit_json:
            MonitorRunRepo.update_monitor_audit(run_id, audit_json)

    @staticmethod
    def stale_stuck_runs(group_id: int = None):
        """将超过 5 分钟仍 RUNNING 的记录标记为 STALE。"""
        cutoff = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        if group_id:
            execute(
                "UPDATE monitor_runs SET status='stale' WHERE status='running' AND created_at<? AND group_id=?",
                (cutoff, group_id),
            )
        else:
            execute(
                "UPDATE monitor_runs SET status='stale' WHERE status='running' AND created_at<?",
                (cutoff,),
            )

    @staticmethod
    def get_last_completed(group_id: int):
        """获取最近一次 COMPLETED 的 monitor_run。"""
        return query_one(
            """
            SELECT * FROM monitor_runs
            WHERE group_id=? AND status='completed'
            ORDER BY id DESC LIMIT 1
            """,
            (group_id,),
        )

    @staticmethod
    def get_llm_run_count_since(group_id: int, since: str):
        """统计指定时间之后调用了 LLM 的 monitor_run 次数。"""
        row = query_one(
            """
            SELECT COUNT(*) AS c FROM monitor_runs
            WHERE group_id=? AND created_at>=? AND llm_result_json IS NOT NULL
            """,
            (group_id, since),
        )
        return int(row["c"]) if row else 0
