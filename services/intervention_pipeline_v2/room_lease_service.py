# -*- coding: utf-8 -*-
"""房间租约服务：通过 SQLite 条件 UPDATE 实现乐观锁。

核心原则：
- 不得通过"先查询再无条件更新"实现锁。
- 使用 UPDATE room SET state='AI_INTERVENING', version=version+1, ... WHERE id=? AND state='OPEN' AND version=?
- 根据 affected rows 判断是否成功。
- 获取锁后立即提交事务，再调用 LLM。
- 严禁持有数据库事务等待 LLM。
"""
import logging
import math
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

from db import db, now_str, parse_dt, query_one
from config import (
    EMOTION_STRATEGY_SPACING_SECONDS,
    INTERVENTION_V2_COOLDOWN_SECONDS,
    INTERVENTION_V2_LOCK_SECONDS,
    THREE_STAGE_LOCK_HEARTBEAT_SECONDS,
    THREE_STAGE_LOCK_INITIAL_TTL_SECONDS,
    THREE_STAGE_LOCK_MAX_TOTAL_SECONDS,
)
from services.three_stage_latency import latency_timestamp, record_latency_event


logger = logging.getLogger(__name__)


class StrategyPipelineLeaseHeartbeat:
    """Renew one authoritative strategy lease while a model call is active."""

    def __init__(
        self,
        pipeline_run_id: int,
        lock_token: str,
        *,
        interval_seconds: int = None,
    ):
        self.pipeline_run_id = int(pipeline_run_id)
        self.lock_token = str(lock_token or "")
        self.interval_seconds = int(
            interval_seconds or THREE_STAGE_LOCK_HEARTBEAT_SECONDS
        )
        self.last_result = None
        self._stop_event = threading.Event()
        self._thread = None

    def pulse(self, *, now: datetime = None) -> dict:
        """Run one synchronous renewal; exposed for final gates and tests."""
        result = RoomLeaseService.renew_strategy_pipeline(
            self.pipeline_run_id,
            self.lock_token,
            now=now,
        )
        self.last_result = result
        if not result.get("renewed"):
            self._stop_event.set()
        return result

    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name=f"strategy-lease-heartbeat-{self.pipeline_run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                result = self.pulse()
            except Exception as exc:
                # A transient SQLite delay must not kill the heartbeat loop;
                # the next pulse may still renew before the current expiry.
                logger.warning(
                    "Strategy lease heartbeat failed pipeline=%s error=%s",
                    self.pipeline_run_id,
                    exc,
                )
                continue
            if not result.get("renewed"):
                logger.warning(
                    "Strategy lease heartbeat stopped pipeline=%s reason=%s",
                    self.pipeline_run_id,
                    result.get("reason"),
                )
                return

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False


class RoomLeaseService:
    """房间乐观锁租赁服务。"""

    LOCK_STATE = "AI_INTERVENING"
    OPEN_STATE = "OPEN"
    ACTIVE_RUN_STATUSES = (
        "PENDING",
        "RUNNING",
        "REVALIDATING",
        "LOCKED",
        "GENERATING",
        "VALIDATING",
    )
    TERMINAL_PIPELINE_STATUSES = (
        "PUBLISHED",
        "SUPPRESSED",
        "STALE",
        "FAILED",
        "CANCELLED",
        "SUPERSEDED",
    )
    ACTIVE_PIPELINE_STATUSES = (
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
    )

    # ------------------------------------------------------------------
    # 锁操作
    # ------------------------------------------------------------------
    @staticmethod
    def acquire(
        group_id: int,
        intervention_run_id: int,
        lock_seconds: int = None,
    ) -> Optional[str]:
        """尝试获取房间锁。

        使用条件 UPDATE 原子获取锁：
        UPDATE room SET state='AI_INTERVENING', version=version+1,
            lock_token=?, lock_expires_at=?, active_intervention_run_id=?
        WHERE id=? AND state='OPEN' AND version=?

        返回 lock_token（成功）或 None（失败）。

        Args:
            group_id: 房间 ID
            intervention_run_id: 关联的 intervention_run
            lock_seconds: 锁定时长（秒），默认 INTERVENTION_V2_LOCK_SECONDS
        """
        if lock_seconds is None:
            lock_seconds = INTERVENTION_V2_LOCK_SECONDS

        # The delayed Huey release is a safety net, not the only recovery path.
        # Reclaim an already-expired orphan before attempting a new lease.
        RoomLeaseService.recover_expired(group_id)

        lock_token = str(uuid.uuid4())
        lock_expires_at = (datetime.now() + timedelta(seconds=lock_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        # 读取当前 version
        room = query_one(
            "SELECT id, state, version FROM groups WHERE id=?",
            (group_id,),
        )
        if not room:
            return None
        if room["state"] != RoomLeaseService.OPEN_STATE:
            return None

        current_version = int(room["version"])

        # 条件 UPDATE（乐观锁）
        conn = db()
        try:
            cur = conn.execute(
                """UPDATE groups
                   SET state=?, version=version+1, lock_token=?, lock_expires_at=?,
                       active_intervention_run_id=?
                   WHERE id=? AND state=? AND version=?""",
                (
                    RoomLeaseService.LOCK_STATE,
                    lock_token,
                    lock_expires_at,
                    intervention_run_id,
                    group_id,
                    RoomLeaseService.OPEN_STATE,
                    current_version,
                ),
            )
            affected = cur.rowcount
            if affected > 0 and int(intervention_run_id) < 0:
                record_latency_event(
                    stage="lock",
                    event="room_lock_acquired",
                    pipeline_run_id=abs(int(intervention_run_id)),
                    lock_owner=int(intervention_run_id),
                    lock_token=lock_token,
                    occurred_at=latency_timestamp(),
                    details={
                        "reason": "stage1_preliminary_acquire",
                        "lease_action": "acquire",
                        "lease_acquired": True,
                        "lock_ttl_seconds": lock_seconds,
                        "strategy_cooldown_seconds": INTERVENTION_V2_COOLDOWN_SECONDS,
                    },
                    conn=conn,
                    pipeline_context=True,
                )
            conn.commit()

            if affected > 0:
                return lock_token
            return None
        finally:
            conn.close()

    @staticmethod
    def claim_strategy_pipeline(
        pipeline_run_id: int,
        *,
        lock_seconds: int = None,
    ) -> dict:
        """Acquire, renew, or transfer a lease to an authoritative pipeline.

        New preliminary Stage 1 work is lock-free.  The transfer branch remains
        only so an in-flight legacy preliminary lease can be adopted safely
        during a rolling deployment; the target must still be tied to a live
        assessment batch and pass all scope/switch checks.
        """
        pipeline_id = int(pipeline_run_id)
        lease_seconds = int(lock_seconds or THREE_STAGE_LOCK_INITIAL_TTL_SECONDS)
        now_dt = datetime.now()
        timestamp = _format_timestamp(now_dt, milliseconds=True)
        expires_at = (
            now_dt + timedelta(seconds=lease_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = db()
        token = None
        transferred_from = None
        renewed = False

        def _claim_failed(reason: str, **fields) -> dict:
            result = {
                "acquired": False,
                "reason": str(reason),
                "pipeline_run_id": pipeline_id,
                **fields,
            }
            record_latency_event(
                stage="lock",
                event="room_lock_acquire_failed",
                pipeline_run_id=pipeline_id,
                details={
                    "reason": str(reason),
                    "failure_category": "invalid_room_lease",
                    "lease_action": "acquire",
                },
                pipeline_context=True,
            )
            result["failure_category"] = "invalid_room_lease"
            return result

        pipeline_hint = query_one(
            "SELECT group_id FROM strategy_pipeline_runs WHERE id=?",
            (pipeline_id,),
        )
        if pipeline_hint:
            RoomLeaseService.recover_expired(int(pipeline_hint["group_id"]))
            owned_hint = query_one(
                """
                SELECT p.room_lock_token, g.state, g.lock_token,
                       g.active_intervention_run_id
                  FROM strategy_pipeline_runs AS p
                  JOIN groups AS g ON g.id=p.group_id
                 WHERE p.id=?
                """,
                (pipeline_id,),
            )
            if (
                owned_hint
                and owned_hint["state"] == RoomLeaseService.LOCK_STATE
                and owned_hint["lock_token"]
                and owned_hint["lock_token"] == owned_hint["room_lock_token"]
                and int(owned_hint["active_intervention_run_id"] or 0)
                == -pipeline_id
            ):
                return RoomLeaseService.renew_strategy_pipeline(
                    pipeline_id,
                    str(owned_hint["lock_token"]),
                    lock_seconds=lease_seconds,
                    now=now_dt,
                )
        try:
            conn.execute("BEGIN IMMEDIATE")
            pipeline = conn.execute(
                "SELECT * FROM strategy_pipeline_runs WHERE id=?",
                (pipeline_id,),
            ).fetchone()
            if not pipeline:
                conn.rollback()
                return _claim_failed("pipeline_not_found")
            if str(pipeline["publish_status"] or "").upper() == "PUBLISHED":
                conn.rollback()
                return _claim_failed("pipeline_already_published")

            stage2_ready = (
                str(pipeline["stage2_status"] or "").upper() == "SUCCEEDED"
                and int(pipeline["should_intervene"] or 0) == 1
            )
            coarse_ready = int(pipeline["coarse_should_escalate"] or 0) == 1
            if not (stage2_ready or coarse_ready):
                conn.rollback()
                return _claim_failed("pipeline_does_not_require_room_lock")

            lifecycle_guard = RoomLeaseService._authoritative_pipeline_guard(
                conn,
                pipeline,
                stage2_ready=stage2_ready,
                coarse_ready=coarse_ready,
            )
            if not lifecycle_guard["allowed"]:
                conn.rollback()
                return _claim_failed(lifecycle_guard["reason"])

            group = conn.execute(
                """
                SELECT id, state, version, lock_token, lock_expires_at,
                       active_intervention_run_id
                  FROM groups
                 WHERE id=?
                """,
                (pipeline["group_id"],),
            ).fetchone()
            if not group:
                conn.rollback()
                return _claim_failed("group_not_found")

            expected_owner = -pipeline_id
            owner_id = group["active_intervention_run_id"]
            owner_id = int(owner_id) if owner_id is not None else None
            state = str(group["state"] or "").upper()
            current_token = group["lock_token"]

            if (
                state == RoomLeaseService.LOCK_STATE
                and owner_id == expected_owner
                and current_token
            ):
                token = str(current_token)
                acquired_at = _parse_timestamp(pipeline["room_lock_acquired_at"])
                acquired_at = acquired_at or now_dt
                max_deadline = acquired_at + timedelta(
                    seconds=THREE_STAGE_LOCK_MAX_TOTAL_SECONDS
                )
                if (max_deadline - now_dt).total_seconds() < 1:
                    conn.rollback()
                    return _claim_failed(
                        "room_lease_max_total_exceeded", renewed=False
                    )
                current_expiry = _parse_timestamp(group["lock_expires_at"])
                if current_expiry is None or current_expiry <= now_dt:
                    conn.rollback()
                    return _claim_failed("room_lease_expired", renewed=False)
                expires_at = min(
                    now_dt + timedelta(seconds=lease_seconds),
                    max_deadline,
                ).strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    """
                    UPDATE groups
                       SET version=version+1, lock_expires_at=?
                     WHERE id=? AND state=? AND lock_token=?
                       AND active_intervention_run_id=? AND lock_expires_at=?
                    """,
                    (
                        expires_at,
                        pipeline["group_id"],
                        RoomLeaseService.LOCK_STATE,
                        token,
                        expected_owner,
                        group["lock_expires_at"],
                    ),
                )
                renewed = cur.rowcount == 1
            elif state == RoomLeaseService.OPEN_STATE:
                token = str(uuid.uuid4())
                cur = conn.execute(
                    """
                    UPDATE groups
                       SET state=?, version=version+1, lock_token=?,
                           lock_expires_at=?, active_intervention_run_id=?
                     WHERE id=? AND state=? AND version=?
                    """,
                    (
                        RoomLeaseService.LOCK_STATE,
                        token,
                        expires_at,
                        expected_owner,
                        pipeline["group_id"],
                        RoomLeaseService.OPEN_STATE,
                        int(group["version"] or 0),
                    ),
                )
            elif (
                state == RoomLeaseService.LOCK_STATE
                and owner_id is not None
                and owner_id < 0
                and current_token
            ):
                owner_pipeline_id = abs(owner_id)
                owner = conn.execute(
                    "SELECT * FROM strategy_pipeline_runs WHERE id=?",
                    (owner_pipeline_id,),
                ).fetchone()
                if not RoomLeaseService._can_transfer_stage1_pipeline_lease(
                    owner,
                    pipeline,
                ):
                    conn.rollback()
                    return _claim_failed(
                        "room_locked_by_other_pipeline",
                        lock_owner_run_id=owner_pipeline_id,
                    )
                token = str(uuid.uuid4())
                cur = conn.execute(
                    """
                    UPDATE groups
                       SET version=version+1, lock_token=?, lock_expires_at=?,
                           active_intervention_run_id=?
                     WHERE id=? AND state=? AND lock_token=?
                       AND active_intervention_run_id=?
                    """,
                    (
                        token,
                        expires_at,
                        expected_owner,
                        pipeline["group_id"],
                        RoomLeaseService.LOCK_STATE,
                        current_token,
                        owner_id,
                    ),
                )
                transferred_from = owner_pipeline_id if cur.rowcount == 1 else None
            else:
                conn.rollback()
                return _claim_failed(
                    "room_lock_unavailable",
                    lock_owner_run_id=abs(owner_id) if owner_id else None,
                )

            if cur.rowcount != 1 or not token:
                conn.rollback()
                return _claim_failed("room_lease_claim_race")

            if transferred_from is not None:
                active_placeholders = ",".join(
                    "?" for _ in RoomLeaseService.ACTIVE_PIPELINE_STATUSES
                )
                conn.execute(
                    f"""
                    UPDATE strategy_pipeline_runs
                       SET publish_status='SKIPPED',
                           final_status='SUPERSEDED',
                           skip_reason='SUPERSEDED_BY_STATE_BATCH',
                           superseded_by_run_id=?,
                           room_lock_released_at=CASE
                               WHEN id=? THEN COALESCE(room_lock_released_at, ?)
                               ELSE room_lock_released_at
                           END,
                           updated_at=?
                     WHERE group_id=?
                       AND COALESCE(session_id, 0)=COALESCE(?, 0)
                       AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
                       AND id<>?
                       AND COALESCE(input_cutoff_student_sequence, 0)
                           BETWEEN ? AND ?
                       AND UPPER(COALESCE(stage2_status, ''))<>'SUCCEEDED'
                       AND UPPER(COALESCE(publish_status, ''))<>'PUBLISHED'
                       AND UPPER(COALESCE(final_status, 'PENDING'))
                           IN ({active_placeholders})
                    """,
                    (
                        pipeline_id,
                        transferred_from,
                        timestamp,
                        timestamp,
                        pipeline["group_id"],
                        pipeline["session_id"],
                        pipeline["discussion_id"],
                        pipeline_id,
                        int(pipeline["input_start_sequence"] or 0),
                        int(pipeline["input_cutoff_student_sequence"] or 0),
                        *RoomLeaseService.ACTIVE_PIPELINE_STATUSES,
                    ),
                )

            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET room_lock_token=?,
                       room_lock_acquired_at=CASE
                           WHEN ?=1 THEN COALESCE(room_lock_acquired_at, ?)
                           ELSE ?
                       END,
                       room_lock_released_at=NULL,
                       final_status=CASE
                           WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                               THEN final_status
                           ELSE 'LOCKED'
                       END,
                       skip_reason=NULL,
                       failure_code=NULL,
                       failure_detail=NULL,
                       updated_at=?
                 WHERE id=?
                """,
                (
                    token,
                    1 if renewed else 0,
                    timestamp,
                    timestamp,
                    timestamp,
                    pipeline_id,
                ),
            )
            if transferred_from is not None:
                record_latency_event(
                    stage="lock",
                    event="room_lock_released",
                    pipeline_run_id=transferred_from,
                    occurred_at=timestamp,
                    lock_token=current_token,
                    details={
                        "reason": "room_lease_transferred",
                        "lease_action": "release",
                        "lease_released": True,
                    },
                    conn=conn,
                    pipeline_context=True,
                )
            record_latency_event(
                stage="lock",
                event="room_lock_renewed" if renewed else "room_lock_acquired",
                pipeline_run_id=pipeline_id,
                occurred_at=timestamp,
                lock_owner=expected_owner,
                lock_token=token,
                details={
                    "reason": (
                        "room_lease_transferred"
                        if transferred_from is not None
                        else "room_lease_renewed"
                        if renewed
                        else "room_lease_acquired"
                    ),
                    "lease_action": "renew" if renewed else "acquire",
                    "lease_acquired": True,
                    "transferred_from_pipeline_id": transferred_from,
                    "renewed": renewed,
                    "lock_ttl_seconds": lease_seconds,
                    "lock_heartbeat_seconds": THREE_STAGE_LOCK_HEARTBEAT_SECONDS,
                    "lock_max_total_seconds": THREE_STAGE_LOCK_MAX_TOTAL_SECONDS,
                    "strategy_cooldown_seconds": INTERVENTION_V2_COOLDOWN_SECONDS,
                    # Deprecated telemetry field retained for old audit readers;
                    # it is not consulted by lease or publish decisions.
                    "emotion_strategy_spacing_seconds": EMOTION_STRATEGY_SPACING_SECONDS,
                },
                conn=conn,
                pipeline_context=True,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        RoomLeaseService._schedule_strategy_pipeline_expiry(
            int(pipeline["group_id"]),
            token,
            _expiry_delay_seconds(expires_at, now_dt),
        )
        return {
            "acquired": True,
            "reason": (
                "room_lease_transferred"
                if transferred_from is not None
                else "room_lease_renewed"
                if renewed
                else "room_lease_acquired"
            ),
            "pipeline_run_id": pipeline_id,
            "lock_token": token,
            "lock_expires_at": expires_at,
            "lock_owner_run_id": expected_owner,
            "transferred_from_pipeline_id": transferred_from,
            "renewed": renewed,
        }

    @staticmethod
    def renew_strategy_pipeline(
        pipeline_run_id: int,
        lock_token: str,
        *,
        lock_seconds: int = None,
        max_total_seconds: int = None,
        now: datetime = None,
    ) -> dict:
        """Atomically renew an active authoritative lease within its hard cap."""
        pipeline_id = int(pipeline_run_id)
        token = str(lock_token or "")
        lease_seconds = int(lock_seconds or THREE_STAGE_LOCK_INITIAL_TTL_SECONDS)
        max_seconds = int(max_total_seconds or THREE_STAGE_LOCK_MAX_TOTAL_SECONDS)
        now_dt = now or datetime.now()
        timestamp = _format_timestamp(now_dt, milliseconds=True)
        expected_owner = -pipeline_id
        expires_at = None
        group_id = None

        def rejected(reason: str) -> dict:
            logger.warning(
                "[room_lease] renew_rejected pipeline=%s owner=%s reason=%s "
                "lock_ttl_seconds=%s lock_heartbeat_seconds=%s "
                "lock_max_total_seconds=%s strategy_cooldown_seconds=%s "
                "emotion_strategy_spacing_seconds=%s",
                pipeline_id,
                expected_owner,
                reason,
                lease_seconds,
                THREE_STAGE_LOCK_HEARTBEAT_SECONDS,
                max_seconds,
                INTERVENTION_V2_COOLDOWN_SECONDS,
                EMOTION_STRATEGY_SPACING_SECONDS,
            )
            return {
                "acquired": False,
                "renewed": False,
                "reason": reason,
                "pipeline_run_id": pipeline_id,
            }

        if not token:
            return rejected("room_lease_token_missing")

        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pipeline = conn.execute(
                "SELECT * FROM strategy_pipeline_runs WHERE id=?",
                (pipeline_id,),
            ).fetchone()
            if not pipeline:
                conn.rollback()
                return rejected("pipeline_not_found")
            final_status = str(pipeline["final_status"] or "PENDING").upper()
            if (
                final_status not in RoomLeaseService.ACTIVE_PIPELINE_STATUSES
                or str(pipeline["publish_status"] or "").upper() == "PUBLISHED"
            ):
                conn.rollback()
                return rejected("pipeline_terminal")
            if str(pipeline["room_lock_token"] or "") != token:
                conn.rollback()
                return rejected("room_lease_token_mismatch")

            group_id = int(pipeline["group_id"])
            group = conn.execute(
                """
                SELECT state, lock_token, lock_expires_at,
                       active_intervention_run_id
                  FROM groups
                 WHERE id=?
                """,
                (group_id,),
            ).fetchone()
            if not group:
                conn.rollback()
                return rejected("group_not_found")
            if str(group["state"] or "").upper() != RoomLeaseService.LOCK_STATE:
                conn.rollback()
                return rejected("room_lease_not_active")
            if str(group["lock_token"] or "") != token:
                conn.rollback()
                return rejected("room_lease_token_mismatch")
            if int(group["active_intervention_run_id"] or 0) != expected_owner:
                conn.rollback()
                return rejected("room_lease_owner_mismatch")

            acquired_at = _parse_timestamp(pipeline["room_lock_acquired_at"])
            acquired_at = acquired_at or now_dt
            max_deadline = acquired_at + timedelta(seconds=max_seconds)
            remaining_seconds = (max_deadline - now_dt).total_seconds()
            if remaining_seconds < 1:
                conn.rollback()
                return rejected("room_lease_max_total_exceeded")

            current_expiry = _parse_timestamp(group["lock_expires_at"])
            if current_expiry is None or current_expiry <= now_dt:
                conn.rollback()
                return rejected("room_lease_expired")

            scope = conn.execute(
                """
                SELECT gsd.status AS discussion_status,
                       es.status AS session_status,
                       CASE WHEN es.agent_mode='strategy' THEN 1 ELSE 0 END
                           AS strategy_enabled,
                       COALESCE(g.auto_intervention_enabled, 1)
                           AS group_enabled
                  FROM group_session_discussions AS gsd
                  JOIN experiment_sessions AS es ON es.id=gsd.session_id
                  JOIN groups AS g ON g.id=gsd.group_id
                 WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
                """,
                (
                    pipeline["discussion_id"],
                    pipeline["group_id"],
                    pipeline["session_id"],
                ),
            ).fetchone()
            if not scope or str(scope["discussion_status"] or "").lower() != "running":
                conn.rollback()
                return rejected("discussion_not_running")
            if str(scope["session_status"] or "").lower() != "running":
                conn.rollback()
                return rejected("session_not_running")
            if not bool(scope["strategy_enabled"]):
                conn.rollback()
                return rejected("strategy_agent_disabled")
            if not bool(scope["group_enabled"]):
                conn.rollback()
                return rejected("group_auto_intervention_disabled")

            expires_dt = min(
                now_dt + timedelta(seconds=lease_seconds),
                max_deadline,
            )
            expires_at = _format_timestamp(expires_dt)
            cur = conn.execute(
                """
                UPDATE groups
                   SET version=version+1, lock_expires_at=?
                 WHERE id=? AND state=? AND lock_token=?
                   AND active_intervention_run_id=? AND lock_expires_at=?
                """,
                (
                    expires_at,
                    group_id,
                    RoomLeaseService.LOCK_STATE,
                    token,
                    expected_owner,
                    group["lock_expires_at"],
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return rejected("room_lease_renew_race")
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET updated_at=?
                 WHERE id=? AND room_lock_token=?
                """,
                (timestamp, pipeline_id, token),
            )
            record_latency_event(
                stage="lock",
                event="room_lock_renewed",
                pipeline_run_id=pipeline_id,
                occurred_at=timestamp,
                lock_owner=expected_owner,
                lock_token=token,
                details={
                    "reason": "room_lease_heartbeat",
                    "lease_action": "renew",
                    "renewed": True,
                    "lock_ttl_seconds": lease_seconds,
                    "lock_heartbeat_seconds": THREE_STAGE_LOCK_HEARTBEAT_SECONDS,
                    "lock_max_total_seconds": max_seconds,
                    "strategy_cooldown_seconds": INTERVENTION_V2_COOLDOWN_SECONDS,
                    # Deprecated telemetry field retained for old audit readers;
                    # it is not consulted by lease or publish decisions.
                    "emotion_strategy_spacing_seconds": EMOTION_STRATEGY_SPACING_SECONDS,
                },
                conn=conn,
                pipeline_context=True,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        RoomLeaseService._schedule_strategy_pipeline_expiry(
            group_id,
            token,
            _expiry_delay_seconds(expires_at, now_dt),
        )
        return {
            "acquired": True,
            "renewed": True,
            "reason": "room_lease_renewed",
            "pipeline_run_id": pipeline_id,
            "lock_token": token,
            "lock_expires_at": expires_at,
            "lock_owner_run_id": expected_owner,
        }

    @staticmethod
    def strategy_pipeline_heartbeat(
        pipeline_run_id: int,
        lock_token: str,
        *,
        interval_seconds: int = None,
    ) -> StrategyPipelineLeaseHeartbeat:
        return StrategyPipelineLeaseHeartbeat(
            pipeline_run_id,
            lock_token,
            interval_seconds=interval_seconds,
        )

    @staticmethod
    def _authoritative_pipeline_guard(
        conn,
        pipeline,
        *,
        stage2_ready: bool,
        coarse_ready: bool,
    ) -> dict:
        """Validate scope, switches, batch ownership, and lifecycle in one txn."""
        final_status = str(pipeline["final_status"] or "PENDING").upper()
        stage2_status = str(pipeline["stage2_status"] or "PENDING").upper()
        if final_status in RoomLeaseService.TERMINAL_PIPELINE_STATUSES:
            return {"allowed": False, "reason": "pipeline_terminal"}
        if coarse_ready and not stage2_ready:
            if stage2_status != "PENDING" or final_status not in {
                "PENDING_STAGE2",
                "WAITING_FOR_LOCK",
                "LOCKED",
            }:
                return {"allowed": False, "reason": "pipeline_not_stage2_startable"}
        elif stage2_ready and final_status not in {
            "PENDING_STAGE3",
            "LOCKED",
            "GENERATING",
            "VALIDATING",
            "PENDING_DECISION_GATE",
            "READY_TO_PUBLISH",
        }:
            return {"allowed": False, "reason": "pipeline_not_stage3_startable"}

        batch_id = (
            pipeline["assessment_batch_id"]
            if "assessment_batch_id" in pipeline.keys()
            else None
        )
        if not batch_id and not stage2_ready:
            return {"allowed": False, "reason": "authoritative_batch_missing"}
        if batch_id:
            batch = conn.execute(
                """
                SELECT id, group_id, session_id, discussion_id, status,
                       terminal_status
                  FROM state_assessment_batches
                 WHERE id=?
                """,
                (int(batch_id),),
            ).fetchone()
            if not batch:
                return {"allowed": False, "reason": "authoritative_batch_missing"}
            if (
                int(batch["group_id"]) != int(pipeline["group_id"])
                or int(batch["session_id"] or 0) != int(pipeline["session_id"] or 0)
                or int(batch["discussion_id"] or 0)
                != int(pipeline["discussion_id"] or 0)
            ):
                return {"allowed": False, "reason": "authoritative_batch_scope_mismatch"}
            allowed_batch_statuses = {"running", "succeeded"} if stage2_ready else {"running"}
            if str(batch["status"] or "").lower() not in allowed_batch_statuses:
                return {"allowed": False, "reason": "authoritative_batch_not_active"}
            if batch["terminal_status"]:
                return {"allowed": False, "reason": "authoritative_batch_terminal"}

        scope = conn.execute(
            """
            SELECT gsd.status AS discussion_status,
                   es.status AS session_status,
                   CASE WHEN es.agent_mode='strategy' THEN 1 ELSE 0 END
                       AS strategy_enabled,
                   COALESCE(g.auto_intervention_enabled, 1) AS group_enabled
              FROM group_session_discussions AS gsd
              JOIN experiment_sessions AS es ON es.id=gsd.session_id
              JOIN groups AS g ON g.id=gsd.group_id
             WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
            """,
            (
                pipeline["discussion_id"],
                pipeline["group_id"],
                pipeline["session_id"],
            ),
        ).fetchone()
        if not scope or str(scope["discussion_status"] or "").lower() != "running":
            return {"allowed": False, "reason": "discussion_not_running"}
        if str(scope["session_status"] or "").lower() != "running":
            return {"allowed": False, "reason": "session_not_running"}
        if not bool(scope["strategy_enabled"]):
            return {"allowed": False, "reason": "strategy_agent_disabled"}
        if not bool(scope["group_enabled"]):
            return {"allowed": False, "reason": "group_auto_intervention_disabled"}

        if pipeline["task_id"]:
            task = conn.execute(
                "SELECT agent_intervention_enabled FROM learning_tasks WHERE id=?",
                (pipeline["task_id"],),
            ).fetchone()
            if task and not bool(task["agent_intervention_enabled"]):
                return {"allowed": False, "reason": "task_agent_intervention_disabled"}
        control = conn.execute(
            """
            SELECT agent_paused
              FROM group_session_controls
             WHERE group_id=? AND session_id=?
            """,
            (pipeline["group_id"], pipeline["session_id"]),
        ).fetchone()
        if control and bool(control["agent_paused"]):
            return {"allowed": False, "reason": "group_agent_paused"}
        return {"allowed": True, "reason": "allowed"}

    @staticmethod
    def _can_transfer_stage1_pipeline_lease(owner, target) -> bool:
        if not owner:
            return False
        if (
            "assessment_batch_id" in owner.keys()
            and owner["assessment_batch_id"] is not None
        ):
            # An authoritative owner is a real consumer, not a legacy
            # preliminary lease.  Competing pipelines must never preempt it.
            return False
        if (
            int(owner["group_id"]) != int(target["group_id"])
            or int(owner["session_id"] or 0) != int(target["session_id"] or 0)
            or int(owner["discussion_id"] or 0)
            != int(target["discussion_id"] or 0)
        ):
            return False
        if str(owner["publish_status"] or "").upper() == "PUBLISHED":
            return False
        if str(owner["stage2_status"] or "").upper() == "SUCCEEDED":
            return False
        if str(owner["final_status"] or "PENDING").upper() not in (
            RoomLeaseService.ACTIVE_PIPELINE_STATUSES
        ):
            return False
        owner_cutoff = int(owner["input_cutoff_student_sequence"] or 0)
        target_start = int(target["input_start_sequence"] or 0)
        target_cutoff = int(target["input_cutoff_student_sequence"] or 0)
        return target_start <= owner_cutoff <= target_cutoff

    @staticmethod
    def _schedule_strategy_pipeline_expiry(
        group_id: int,
        lock_token: str,
        delay_seconds: int,
    ) -> None:
        try:
            from config import HUEY_IMMEDIATE

            if HUEY_IMMEDIATE:
                return
            from services.intervention_pipeline_v2.intervention_service import (
                InterventionService,
            )

            InterventionService._schedule_release_expired(
                group_id,
                lock_token,
                delay_seconds=delay_seconds,
            )
        except Exception as exc:
            logger.warning(
                "Failed to schedule strategy pipeline lease expiry "
                "group=%s token=%s: %s",
                group_id,
                lock_token[:8],
                exc,
            )

    @staticmethod
    def release(group_id: int, lock_token: str) -> bool:
        """释放房间锁（发布/取消后调用）。

        条件 UPDATE 确保只有持有有效 lock_token 才能释放锁。
        UPDATE room SET state='OPEN', version=version+1,
            lock_token=NULL, lock_expires_at=NULL, active_intervention_run_id=NULL
        WHERE id=? AND lock_token=?

        返回 True 表示释放成功。
        """
        conn = db()
        try:
            owner = conn.execute(
                "SELECT active_intervention_run_id FROM groups WHERE id=? AND lock_token=?",
                (group_id, lock_token),
            ).fetchone()
            cur = conn.execute(
                """UPDATE groups
                   SET state=?, version=version+1,
                       lock_token=NULL, lock_expires_at=NULL,
                       active_intervention_run_id=NULL
                   WHERE id=? AND state=? AND lock_token=?""",
                (
                    RoomLeaseService.OPEN_STATE,
                    group_id,
                    RoomLeaseService.LOCK_STATE,
                    lock_token,
                ),
            )
            affected = cur.rowcount
            if (
                affected > 0
                and owner
                and owner["active_intervention_run_id"] is not None
                and int(owner["active_intervention_run_id"]) < 0
            ):
                record_latency_event(
                    stage="lock",
                    event="room_lock_released",
                    pipeline_run_id=abs(int(owner["active_intervention_run_id"])),
                    occurred_at=latency_timestamp(),
                    lock_token=lock_token,
                    details={
                        "reason": "explicit_release",
                        "lease_action": "release",
                        "lease_released": True,
                    },
                    conn=conn,
                    pipeline_context=True,
                )
            conn.commit()
            return affected > 0
        finally:
            conn.close()

    @staticmethod
    def _expire_locked_room(group_id: int, lock_token: str = None) -> bool:
        """Atomically reclaim one expired lease and terminalize its owner."""
        current_time = now_str()
        conn = db()
        try:
            row = conn.execute(
                """
                SELECT state, lock_token, lock_expires_at,
                       active_intervention_run_id
                  FROM groups
                 WHERE id=?
                """,
                (group_id,),
            ).fetchone()
            if (
                not row
                or row["state"] != RoomLeaseService.LOCK_STATE
                or not row["lock_token"]
                or not row["lock_expires_at"]
                or row["lock_expires_at"] > current_time
                or (lock_token is not None and row["lock_token"] != lock_token)
            ):
                return False

            current_token = row["lock_token"]
            owner_id = row["active_intervention_run_id"]
            cur = conn.execute(
                """UPDATE groups
                   SET state=?, version=version+1,
                       lock_token=NULL, lock_expires_at=NULL,
                       active_intervention_run_id=NULL
                   WHERE id=? AND state=? AND lock_token=?
                     AND lock_expires_at IS NOT NULL
                     AND lock_expires_at <= ?""",
                (
                    RoomLeaseService.OPEN_STATE,
                    group_id,
                    RoomLeaseService.LOCK_STATE,
                    current_token,
                    current_time,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False

            if owner_id is not None and int(owner_id) > 0:
                placeholders = ",".join("?" for _ in RoomLeaseService.ACTIVE_RUN_STATUSES)
                conn.execute(
                    f"""
                    UPDATE intervention_runs
                       SET status='EXPIRED',
                           decision=COALESCE(decision, 'EXPIRED'),
                           failure_reason=COALESCE(failure_reason, 'room_lease_expired'),
                           skip_reason=COALESCE(skip_reason, 'room_lease_expired'),
                           completed_at=COALESCE(completed_at, ?)
                     WHERE id=?
                       AND UPPER(COALESCE(status, '')) IN ({placeholders})
                    """,
                    (
                        current_time,
                        int(owner_id),
                        *RoomLeaseService.ACTIVE_RUN_STATUSES,
                    ),
                )
            elif owner_id is not None and int(owner_id) < 0:
                negative_owner_id = abs(int(owner_id))
                pipeline = conn.execute(
                    """
                    SELECT id, final_status
                      FROM strategy_pipeline_runs
                     WHERE id=? AND room_lock_token=?
                    """,
                    (negative_owner_id, current_token),
                ).fetchone()
                if pipeline:
                    final_status = (pipeline["final_status"] or "").upper()
                    if final_status not in RoomLeaseService.TERMINAL_PIPELINE_STATUSES:
                        conn.execute(
                            """
                            UPDATE strategy_pipeline_runs
                               SET final_status='FAILED',
                                   publish_status=CASE
                                       WHEN publish_status='PUBLISHED' THEN publish_status
                                       ELSE 'FAILED'
                                   END,
                                   skip_reason=COALESCE(
                                       skip_reason, 'ROOM_LEASE_EXPIRED'
                                   ),
                                   failure_code=COALESCE(
                                       failure_code, 'ROOM_LEASE_EXPIRED'
                                   ),
                                   failure_detail=COALESCE(
                                       failure_detail,
                                       'room lease expired before pipeline completion'
                                   ),
                                   room_lock_released_at=COALESCE(
                                       room_lock_released_at, ?
                                   ),
                                   updated_at=?
                             WHERE id=? AND room_lock_token=?
                            """,
                            (
                                current_time,
                                current_time,
                                negative_owner_id,
                                current_token,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE strategy_pipeline_runs
                               SET room_lock_released_at=COALESCE(
                                       room_lock_released_at, ?
                                   ),
                                   updated_at=?
                             WHERE id=? AND room_lock_token=?
                            """,
                            (
                                current_time,
                                current_time,
                                negative_owner_id,
                                current_token,
                            ),
                        )
                    placeholders = ",".join(
                        "?" for _ in RoomLeaseService.ACTIVE_RUN_STATUSES
                    )
                    conn.execute(
                        f"""
                        UPDATE intervention_runs
                           SET status='EXPIRED',
                               decision=COALESCE(decision, 'EXPIRED'),
                               failure_reason=COALESCE(
                                   failure_reason, 'room_lease_expired'
                               ),
                               skip_reason=COALESCE(
                                   skip_reason, 'room_lease_expired'
                               ),
                               completed_at=COALESCE(completed_at, ?)
                         WHERE strategy_pipeline_run_id=?
                           AND UPPER(COALESCE(status, '')) IN ({placeholders})
                        """,
                        (
                            current_time,
                            negative_owner_id,
                            *RoomLeaseService.ACTIVE_RUN_STATUSES,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE help_requests
                           SET status='FAILED',
                               handling_status='failed',
                               failure_reason=COALESCE(
                                   failure_reason, 'room_lease_expired'
                               ),
                               handled_at=COALESCE(handled_at, ?),
                               completed_at=COALESCE(completed_at, ?)
                         WHERE id=?
                           AND UPPER(COALESCE(status, '')) IN (
                               'QUEUED', 'PENDING', 'RUNNING'
                           )
                        """,
                        (current_time, current_time, negative_owner_id),
                    )

            if owner_id is not None and int(owner_id) < 0:
                record_latency_event(
                    stage="lock",
                    event="room_lock_expired",
                    pipeline_run_id=abs(int(owner_id)),
                    occurred_at=latency_timestamp(),
                    lock_token=current_token,
                    details={
                        "reason": "ttl_recovery",
                        "terminal_status": "FAILED",
                        "lease_action": "release",
                        "lease_released": True,
                    },
                    conn=conn,
                    pipeline_context=True,
                )
                record_latency_event(
                    stage="lock",
                    event="room_lock_released",
                    pipeline_run_id=abs(int(owner_id)),
                    occurred_at=latency_timestamp(),
                    lock_token=current_token,
                    details={
                        "reason": "ttl_recovery",
                        "terminal_status": "FAILED",
                        "lease_action": "release",
                        "lease_released": True,
                    },
                    conn=conn,
                    pipeline_context=True,
                )
            conn.commit()
            logger.warning(
                "Recovered expired room lease group=%s owner=%s "
                "expired_at=%s release_reason=room_lease_expired",
                group_id,
                owner_id,
                row["lock_expires_at"],
            )
            return True
        finally:
            conn.close()

    @staticmethod
    def recover_expired(group_id: int) -> bool:
        """Best-effort request/acquire-path recovery for an orphaned lease."""
        return RoomLeaseService._expire_locked_room(group_id)

    @staticmethod
    def release_expired(group_id: int, lock_token: str) -> bool:
        """释放过期锁（超时恢复任务调用）。

        只有 lock_token 匹配且锁确实过期时才解锁，确保迟到的延迟
        任务不能释放仍然有效的当前介入锁。
        """
        return RoomLeaseService._expire_locked_room(group_id, lock_token=lock_token)

    @staticmethod
    def verify_lock(group_id: int, lock_token: str) -> bool:
        """验证房间当前是否仍持有指定 lock_token。"""
        row = query_one(
            "SELECT lock_token, state FROM groups WHERE id=?",
            (group_id,),
        )
        if not row:
            return False
        if row["state"] != RoomLeaseService.LOCK_STATE:
            return False
        return row["lock_token"] == lock_token

    @staticmethod
    def get_lock_info(group_id: int) -> dict:
        """获取房间锁信息（用于调试/监控）。"""
        row = query_one(
            "SELECT state, version, lock_token, lock_expires_at, active_intervention_run_id FROM groups WHERE id=?",
            (group_id,),
        )
        if not row:
            return {"exists": False}
        owner_id = row["active_intervention_run_id"]
        owner_type = None
        owner_run_id = None
        owner_status = None
        lock_reason = None
        if owner_id is not None and int(owner_id) < 0:
            owner_run_id = abs(int(owner_id))
            strategy_pipeline = query_one(
                """
                SELECT trigger_source, final_status
                  FROM strategy_pipeline_runs
                 WHERE id=? AND room_lock_token=?
                """,
                (owner_run_id, row["lock_token"]),
            )
            if strategy_pipeline:
                owner_type = "strategy_pipeline"
                lock_reason = (
                    strategy_pipeline["trigger_source"]
                    or "strategy_intervention"
                )
                owner_status = strategy_pipeline["final_status"] or "unknown"
            else:
                owner_type = "student_help"
                lock_reason = "student_help_request"
                help_request = query_one(
                    "SELECT status FROM help_requests WHERE id=?",
                    (owner_run_id,),
                )
                owner_status = help_request["status"] if help_request else "orphaned"
        elif owner_id is not None and int(owner_id) > 0:
            owner_run_id = int(owner_id)
            run = query_one(
                """
                SELECT agent_type, trigger_type, status
                  FROM intervention_runs
                 WHERE id=?
                """,
                (owner_run_id,),
            )
            owner_type = (
                (run["agent_type"] or "strategy")
                if run
                else "strategy"
            )
            lock_reason = (
                (run["trigger_type"] or "strategy_intervention")
                if run
                else "strategy_intervention"
            )
            owner_status = run["status"] if run else "orphaned"
        expires_at = parse_dt(row["lock_expires_at"]) if row["lock_expires_at"] else None
        return {
            "exists": True,
            "state": row["state"],
            "version": row["version"],
            "lock_token": row["lock_token"],
            "lock_expires_at": row["lock_expires_at"],
            "active_intervention_run_id": row["active_intervention_run_id"],
            "lock_owner_type": owner_type,
            "lock_owner_run_id": owner_run_id,
            "lock_owner_status": owner_status,
            "lock_reason": lock_reason,
            "is_locked": row["state"] == RoomLeaseService.LOCK_STATE,
            "is_expired": bool(expires_at and expires_at <= datetime.now()),
        }


def _parse_timestamp(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _format_timestamp(value: datetime, *, milliseconds: bool = False) -> str:
    if milliseconds:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _expiry_delay_seconds(expires_at: str, now: datetime) -> int:
    expiry = _parse_timestamp(expires_at)
    if expiry is None:
        return 1
    return max(1, int(math.ceil((expiry - now).total_seconds())))
