# -*- coding: utf-8 -*-
"""Discussion-scoped, single-chain scheduling for emotion reflections.

The schedule origin is ``group_session_discussions.started_at``.  That value is
written once, in the same transaction that observes every snapshotted group
member as ready.  A unique database row is therefore the final concurrency
boundary for each ``group + session + discussion + slot``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from config import (
    EMOTION_INTERVAL_SECONDS,
    EMOTION_SCAN_MAX_DISCUSSIONS,
    EMOTION_SLOT_DEFER_INITIAL_SECONDS,
    EMOTION_SLOT_DEFER_MAX_ATTEMPTS,
    EMOTION_SLOT_DEFER_MAX_SECONDS,
    EMOTION_SLOT_MAX_ATTEMPTS,
    EMOTION_SLOT_MAX_COMPENSATION_SECONDS,
    EMOTION_SLOT_PENDING_TIMEOUT_SECONDS,
    EMOTION_SLOT_RETRY_DELAY_SECONDS,
    EMOTION_SLOT_RUNNING_TIMEOUT_SECONDS,
)
from db import db, now_str, parse_dt, query_all, query_one
from services.emotion_window_service import (
    EMOTION_SLOT_PROMPT_VERSION,
    EmotionWindowService,
)

logger = logging.getLogger(__name__)


def _now_dt(value=None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    parsed = parse_dt(str(value))
    if parsed is None:
        raise ValueError("invalid datetime")
    return parsed


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _row_dict(row) -> Optional[dict]:
    return dict(row) if row else None


class EmotionSlotService:
    """Create, claim and execute fixed emotion-reflection slots."""

    prompt_version = EMOTION_SLOT_PROMPT_VERSION

    @staticmethod
    def interval_seconds() -> int:
        return max(1, int(EMOTION_INTERVAL_SECONDS or 300))

    @classmethod
    def due_slot_index(cls, all_members_entered_at, *, now=None) -> int:
        started = parse_dt(all_members_entered_at) if isinstance(all_members_entered_at, str) else all_members_entered_at
        if not started:
            return 0
        elapsed = int((_now_dt(now) - started).total_seconds())
        if elapsed < cls.interval_seconds():
            return 0
        return max(0, elapsed // cls.interval_seconds())

    @classmethod
    def slot_scheduled_at(cls, all_members_entered_at, slot_index: int) -> str:
        started = parse_dt(all_members_entered_at) if isinstance(all_members_entered_at, str) else all_members_entered_at
        if not started:
            raise ValueError("all_members_entered_at is required")
        if int(slot_index) < 1:
            raise ValueError("slot_index must be positive")
        return _fmt(started + timedelta(seconds=cls.interval_seconds() * int(slot_index)))

    @classmethod
    def scan_due(cls, *, now=None, limit=None, enqueue=True) -> dict:
        """Scan active discussions and enqueue only the latest due slot."""
        now_dt = _now_dt(now)
        now_text = _fmt(now_dt)
        expired_inactive = cls.expire_inactive_slots(now=now_dt)
        rows = query_all(
            """
            SELECT gsd.id AS discussion_id, gsd.group_id, gsd.session_id,
                   gsd.started_at AS all_members_entered_at,
                   gsd.deadline, gsd.status AS discussion_status,
                   gsd.expected_student_count, gsd.ready_student_count,
                   gsd.submitted_at, gsd.auto_submitted_at,
                   es.session_no, es.task_id, es.status AS session_status,
                   es.agent_mode
            FROM group_session_discussions AS gsd
            JOIN experiment_sessions AS es ON es.id=gsd.session_id
            WHERE gsd.status='running'
              AND es.status='running'
              AND gsd.started_at IS NOT NULL
              AND gsd.expected_student_count > 0
              AND gsd.ready_student_count >= gsd.expected_student_count
              AND gsd.submitted_at IS NULL
              AND gsd.auto_submitted_at IS NULL
              AND es.agent_mode='emotion'
              AND (gsd.deadline IS NULL OR gsd.deadline > ?)
            ORDER BY gsd.id ASC
            LIMIT ?
            """,
            (now_text, max(1, int(limit or EMOTION_SCAN_MAX_DISCUSSIONS or 100))),
        )
        summary = {
            "scanned": len(rows),
            "due": 0,
            "enqueued": 0,
            "skipped": 0,
            "expired": expired_inactive,
            "results": [],
        }
        for row in rows:
            result = cls.ensure_latest_due_slot(dict(row), now=now_dt)
            summary["results"].append(result)
            if result.get("slot_index"):
                summary["due"] += 1
            if result.get("skipped"):
                summary["skipped"] += 1
            slot_id = result.get("enqueue_slot_id")
            if slot_id and enqueue:
                try:
                    cls.enqueue_slot(slot_id)
                    summary["enqueued"] += 1
                    result["enqueued"] = True
                except Exception as exc:
                    cls.mark_failed(slot_id, "enqueue_failed", str(exc), now=now_dt)
                    result.update({"enqueued": False, "reason": "enqueue_failed", "error": str(exc)})
        return summary

    @staticmethod
    def expire_inactive_slots(*, now=None) -> int:
        """Terminalize queued compensation when its discussion can no longer publish."""
        now_text = _fmt(_now_dt(now))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='expired', completed_at=?, enqueued_at=NULL,
                       next_retry_at=NULL, skip_reason='discussion_closed',
                       updated_at=?
                 WHERE status IN ('pending','deferred','failed')
                   AND EXISTS(
                       SELECT 1
                       FROM group_session_discussions AS gsd
                       JOIN experiment_sessions AS es ON es.id=gsd.session_id
                       WHERE gsd.id=emotion_reflection_slots.discussion_id
                         AND (
                             gsd.status<>'running'
                             OR es.status<>'running'
                             OR gsd.submitted_at IS NOT NULL
                             OR gsd.auto_submitted_at IS NOT NULL
                             OR (gsd.deadline IS NOT NULL AND gsd.deadline<=?)
                             OR COALESCE(es.agent_mode, '')<>'emotion'
                         )
                   )
                """,
                (now_text, now_text, now_text),
            )
            conn.commit()
            updated = int(cur.rowcount or 0)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if updated:
            logger.info("emotion inactive slots expired count=%s at=%s", updated, now_text)
        return updated

    @classmethod
    def ensure_latest_due_slot(cls, scope: dict, *, now=None) -> dict:
        """Create missed ledgers and atomically reserve the latest due slot."""
        now_dt = _now_dt(now)
        now_text = _fmt(now_dt)
        session = query_one(
            "SELECT status, agent_mode FROM experiment_sessions WHERE id=?",
            (int(scope["session_id"]),),
        )
        if not session or session["status"] != "running":
            return {
                "group_id": int(scope["group_id"]),
                "session_id": int(scope["session_id"]),
                "discussion_id": int(scope["discussion_id"]),
                "slot_index": 0,
                "enqueued": False,
                "skipped": True,
                "reason": "session_not_running",
            }
        if session["agent_mode"] != "emotion":
            return {
                "group_id": int(scope["group_id"]),
                "session_id": int(scope["session_id"]),
                "discussion_id": int(scope["discussion_id"]),
                "slot_index": 0,
                "enqueued": False,
                "skipped": True,
                "reason": "emotion_agent_disabled",
            }
        started_at = scope.get("all_members_entered_at") or scope.get("started_at")
        current_slot = cls.due_slot_index(started_at, now=now_dt)
        result = {
            "group_id": int(scope["group_id"]),
            "session_id": int(scope["session_id"]),
            "discussion_id": int(scope["discussion_id"]),
            "slot_index": current_slot,
            "enqueued": False,
        }
        if current_slot < 1:
            result.update({"skipped": True, "reason": "slot_not_due"})
            return result

        pending_stale_before = _fmt(
            now_dt - timedelta(seconds=max(1, int(EMOTION_SLOT_PENDING_TIMEOUT_SECONDS or 180)))
        )
        running_stale_before = _fmt(
            now_dt - timedelta(seconds=max(1, int(EMOTION_SLOT_RUNNING_TIMEOUT_SECONDS or 600)))
        )
        max_attempts = max(1, int(EMOTION_SLOT_MAX_ATTEMPTS or 2))
        prompt_version = str(cls.prompt_version)
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # A worker crash must not leave a permanent running slot.  Recovery
            # remains finite because retry_count advances only before generation.
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='failed', last_error='running_timeout',
                       next_retry_at=CASE WHEN retry_count < max_attempts THEN ? ELSE NULL END,
                       completed_at=?, updated_at=?
                 WHERE discussion_id=? AND status='running'
                   AND started_at IS NOT NULL AND started_at<=?
                """,
                (now_text, now_text, now_text, int(scope["discussion_id"]), running_stale_before),
            )
            # A cadence deferral must remain bounded so it cannot overlap the
            # following fixed slot or survive the discussion lifecycle.
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='expired', completed_at=?, enqueued_at=NULL,
                       next_retry_at=NULL,
                       skip_reason='fixed_interval_retry_window_expired',
                       updated_at=?
                 WHERE discussion_id=? AND status='deferred'
                   AND slot_index>=?
                   AND defer_deadline_at IS NOT NULL
                   AND defer_deadline_at<=?
                """,
                (
                    now_text,
                    now_text,
                    int(scope["discussion_id"]),
                    current_slot,
                    now_text,
                ),
            )
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='superseded', completed_at=?, enqueued_at=NULL,
                       next_retry_at=NULL,
                       skip_reason='superseded_by_newer_slot', updated_at=?
                 WHERE discussion_id=? AND slot_index<? AND status='deferred'
                """,
                (now_text, now_text, int(scope["discussion_id"]), current_slot),
            )
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='skipped', completed_at=?,
                       skip_reason='missed_due_to_downtime',
                       enqueued_at=NULL, next_retry_at=NULL, updated_at=?
                 WHERE discussion_id=? AND slot_index<?
                   AND status IN ('pending','failed')
                """,
                (now_text, now_text, int(scope["discussion_id"]), current_slot),
            )
            blocking_running = conn.execute(
                """
                SELECT * FROM emotion_reflection_slots
                WHERE discussion_id=? AND slot_index<? AND status='running'
                ORDER BY slot_index DESC LIMIT 1
                """,
                (int(scope["discussion_id"]), current_slot),
            ).fetchone()
            if blocking_running:
                conn.commit()
                result.update(
                    {
                        "reason": "previous_slot_running",
                        "slot": _row_dict(blocking_running),
                    }
                )
                return result

            for slot_index in range(1, current_slot):
                scheduled_at = cls.slot_scheduled_at(started_at, slot_index)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO emotion_reflection_slots(
                        group_id, session_id, discussion_id, slot_index,
                        scheduled_at, prompt_version,
                        status, completed_at, skip_reason,
                        retry_count, max_attempts, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,'skipped',?,'missed_due_to_downtime',0,?,?,?)
                    """,
                    (
                        int(scope["group_id"]),
                        int(scope["session_id"]),
                        int(scope["discussion_id"]),
                        slot_index,
                        scheduled_at,
                        prompt_version,
                        now_text,
                        max_attempts,
                        now_text,
                        now_text,
                    ),
                )
                missed_row = conn.execute(
                    """
                    SELECT id FROM emotion_reflection_slots
                    WHERE group_id=? AND session_id=? AND discussion_id=?
                      AND slot_index=? AND prompt_version=?
                    """,
                    (
                        int(scope["group_id"]),
                        int(scope["session_id"]),
                        int(scope["discussion_id"]),
                        slot_index,
                        prompt_version,
                    ),
                ).fetchone()
                if missed_row:
                    EmotionWindowService.freeze_slot(
                        conn,
                        slot_id=int(missed_row["id"]),
                        group_id=int(scope["group_id"]),
                        session_id=int(scope["session_id"]),
                        discussion_id=int(scope["discussion_id"]),
                        slot_index=slot_index,
                        started_at=started_at,
                        interval_seconds=cls.interval_seconds(),
                        prompt_version=prompt_version,
                        frozen_at=now_text,
                    )

            scheduled_at = cls.slot_scheduled_at(started_at, current_slot)
            conn.execute(
                """
                INSERT OR IGNORE INTO emotion_reflection_slots(
                    group_id, session_id, discussion_id, slot_index,
                    scheduled_at, prompt_version, status, retry_count, max_attempts,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,'pending',0,?,?,?)
                """,
                (
                    int(scope["group_id"]),
                    int(scope["session_id"]),
                    int(scope["discussion_id"]),
                    current_slot,
                    scheduled_at,
                    prompt_version,
                    max_attempts,
                    now_text,
                    now_text,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM emotion_reflection_slots
                WHERE group_id=? AND session_id=? AND discussion_id=?
                  AND slot_index=? AND prompt_version=?
                """,
                (
                    int(scope["group_id"]),
                    int(scope["session_id"]),
                    int(scope["discussion_id"]),
                    current_slot,
                    prompt_version,
                ),
            ).fetchone()

            if row:
                EmotionWindowService.freeze_slot(
                    conn,
                    slot_id=int(row["id"]),
                    group_id=int(scope["group_id"]),
                    session_id=int(scope["session_id"]),
                    discussion_id=int(scope["discussion_id"]),
                    slot_index=current_slot,
                    started_at=started_at,
                    interval_seconds=cls.interval_seconds(),
                    prompt_version=prompt_version,
                    frozen_at=now_text,
                )
                row = conn.execute(
                    "SELECT * FROM emotion_reflection_slots WHERE id=?",
                    (int(row["id"]),),
                ).fetchone()

            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET superseded_by_slot_id=?
                 WHERE discussion_id=? AND slot_index<?
                   AND status='superseded'
                   AND skip_reason='superseded_by_newer_slot'
                   AND superseded_by_slot_id IS NULL
                """,
                (
                    int(row["id"]) if row else None,
                    int(scope["discussion_id"]),
                    current_slot,
                ),
            )

            if row and row["status"] == "deferred":
                deadline = row["defer_deadline_at"]
                exhausted = int(row["defer_count"] or 0) >= max(
                    1, int(EMOTION_SLOT_DEFER_MAX_ATTEMPTS or 6)
                )
                if (deadline and deadline <= now_text) or exhausted:
                    reason = (
                        "fixed_interval_retry_exhausted"
                        if exhausted
                        else "fixed_interval_retry_window_expired"
                    )
                    conn.execute(
                        """
                        UPDATE emotion_reflection_slots
                           SET status='expired', completed_at=?, enqueued_at=NULL,
                               next_retry_at=NULL, skip_reason=?, updated_at=?
                         WHERE id=? AND status='deferred'
                        """,
                        (now_text, reason, now_text, int(row["id"])),
                    )
                elif row["next_retry_at"] and row["next_retry_at"] <= now_text:
                    conn.execute(
                        """
                        UPDATE emotion_reflection_slots
                           SET status='pending', enqueued_at=NULL,
                               completed_at=NULL, updated_at=?
                         WHERE id=? AND status='deferred'
                        """,
                        (now_text, int(row["id"])),
                    )
            elif row and row["status"] == "failed" and int(row["retry_count"] or 0) < int(row["max_attempts"] or max_attempts):
                if row["next_retry_at"] and row["next_retry_at"] <= now_text:
                    conn.execute(
                        """
                        UPDATE emotion_reflection_slots
                           SET status='pending', enqueued_at=NULL,
                               completed_at=NULL, updated_at=?
                         WHERE id=? AND status='failed'
                        """,
                        (now_text, int(row["id"])),
                    )
            elif row and row["status"] == "pending" and row["enqueued_at"] and row["enqueued_at"] <= pending_stale_before:
                conn.execute(
                    "UPDATE emotion_reflection_slots SET enqueued_at=NULL, updated_at=? WHERE id=? AND status='pending'",
                    (now_text, int(row["id"])),
                )

            claim = conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET enqueued_at=?, updated_at=?
                 WHERE id=(
                       SELECT id FROM emotion_reflection_slots
                        WHERE group_id=? AND session_id=? AND discussion_id=? AND slot_index=?
                          AND prompt_version=?
                   )
                   AND status='pending' AND enqueued_at IS NULL
                   AND retry_count < max_attempts
                """,
                (
                    now_text,
                    now_text,
                    int(scope["group_id"]),
                    int(scope["session_id"]),
                    int(scope["discussion_id"]),
                    current_slot,
                    prompt_version,
                ),
            )
            final_row = conn.execute(
                """
                SELECT * FROM emotion_reflection_slots
                WHERE group_id=? AND session_id=? AND discussion_id=?
                  AND slot_index=? AND prompt_version=?
                """,
                (
                    int(scope["group_id"]),
                    int(scope["session_id"]),
                    int(scope["discussion_id"]),
                    current_slot,
                    prompt_version,
                ),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        result["slot"] = _row_dict(final_row)
        if claim.rowcount == 1:
            result["enqueue_slot_id"] = int(final_row["id"])
        elif final_row and final_row["status"] == "deferred":
            result["reason"] = "slot_deferred"
        elif final_row and final_row["status"] in {"expired", "superseded", "suppressed"}:
            result["reason"] = final_row["skip_reason"] or final_row["status"]
        else:
            result["reason"] = "slot_already_recorded"
        logger.info(
            "emotion slot scan group_id=%s session_id=%s discussion_id=%s slot_index=%s slot_id=%s status=%s reason=%s",
            result["group_id"],
            result["session_id"],
            result["discussion_id"],
            current_slot,
            int(final_row["id"]) if final_row else None,
            final_row["status"] if final_row else None,
            result.get("reason"),
        )
        return result

    @staticmethod
    def enqueue_slot(slot_id: int) -> None:
        from agent.emotion_tasks import execute_emotion_reflection_slot

        execute_emotion_reflection_slot.schedule(args=(int(slot_id),), delay=0, priority=20)

    @staticmethod
    def get_slot(slot_id: int) -> Optional[dict]:
        return _row_dict(query_one("SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)))

    @classmethod
    def claim_slot(cls, slot_id: int, *, now=None) -> dict:
        now_text = _fmt(_now_dt(now))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='running', started_at=?, completed_at=NULL,
                       enqueued_at=NULL, next_retry_at=NULL, updated_at=?
                 WHERE id=? AND status='pending'
                   AND retry_count < max_attempts
                """,
                (now_text, now_text, int(slot_id)),
            )
            row = conn.execute("SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)).fetchone()
            conn.commit()
            return {"claimed": cur.rowcount == 1, "slot": _row_dict(row)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _begin_generation(slot_id: int, latest_student_sequence: int, *, now=None) -> bool:
        now_text = _fmt(_now_dt(now))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET retry_count=retry_count+1,
                       generation_student_sequence=?, updated_at=?
                 WHERE id=? AND status='running'
                   AND retry_count < max_attempts
                """,
                (int(latest_student_sequence), now_text, int(slot_id)),
            )
            conn.commit()
            return cur.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @classmethod
    def execute_slot(cls, slot_id: int, *, now=None) -> dict:
        now_dt = _now_dt(now)
        claim = cls.claim_slot(slot_id, now=now_dt)
        slot = claim.get("slot")
        if not claim.get("claimed"):
            return {"claimed": False, "slot": slot, "reason": "slot_not_claimable"}

        try:
            precheck = cls.precheck(slot, now=now_dt)
            if not precheck.get("allowed"):
                reason = precheck.get("reason") or "precheck_failed"
                if reason in {
                    "discussion_closed",
                    "agent_disabled",
                    "agent_paused",
                    "session_not_found",
                    "group_not_found",
                }:
                    return cls.mark_expired(slot_id, reason, now=now_dt)
                if reason == "newer_slot_exists":
                    return cls.mark_superseded(
                        slot_id,
                        reason,
                        superseded_by_slot_id=precheck.get("newer_slot_id"),
                        now=now_dt,
                    )
                return cls.mark_skipped(slot_id, reason, now=now_dt)

            if not cls._begin_generation(
                slot_id,
                int(precheck["latest_student_sequence"]),
                now=now_dt,
            ):
                return cls.mark_failed(
                    slot_id,
                    "generation_not_claimable",
                    "emotion generation attempt limit reached",
                    retryable=False,
                    now=now_dt,
                )

            scope = precheck["scope"]
            from services.emotion_agent.emotion_reflection_service import EmotionReflectionService

            result = EmotionReflectionService.execute_once(
                group_id=int(slot["group_id"]),
                session_id=int(slot["session_id"]),
                discussion_id=int(slot["discussion_id"]),
                task_id=scope.get("task_id"),
                scheduled_at=slot["scheduled_at"],
                tick_index=int(slot["slot_index"]),
                slot_id=int(slot["id"]),
                window_start=precheck["snapshot"]["current_window_start"],
                window_end=precheck["snapshot"]["current_window_end"],
                frozen_context=precheck["snapshot"],
            )
            if result.get("status") in {"published", "fallback"}:
                if not result.get("message_id") or not result.get("run_id"):
                    return cls.mark_failed(slot_id, "publish_link_missing", "message/run link missing", now=now_dt)
                return cls.mark_sent(
                    slot_id,
                    message_id=int(result["message_id"]),
                    intervention_run_id=int(result["run_id"]),
                    now=now_dt,
                )
            if result.get("status") == "skipped":
                return cls.mark_skipped(slot_id, result.get("reason") or "insufficient_emotional_context", now=now_dt)
            if result.get("status") == "deferred":
                return cls.mark_deferred(
                    slot_id,
                    result.get("reason") or "fixed_interval_spacing",
                    intervention_run_id=result.get("run_id"),
                    now=now_dt,
                )
            if result.get("status") == "suppressed":
                return cls.mark_suppressed(
                    slot_id,
                    result.get("reason") or "suppressed_after_generation",
                    intervention_run_id=result.get("run_id"),
                    now=now_dt,
                )
            if result.get("status") == "expired":
                return cls.mark_expired(
                    slot_id,
                    result.get("reason") or "discussion_closed_after_generation",
                    intervention_run_id=result.get("run_id"),
                    now=now_dt,
                )
            if result.get("status") == "superseded":
                return cls.mark_superseded(
                    slot_id,
                    result.get("reason") or "newer_slot_after_generation",
                    superseded_by_slot_id=result.get("superseded_by_slot_id"),
                    intervention_run_id=result.get("run_id"),
                    now=now_dt,
                )
            return cls.mark_failed(
                slot_id,
                result.get("reason") or "emotion_execution_failed",
                result.get("error") or result.get("skip_reason") or "emotion service failed",
                retryable=result.get("retryable"),
                now=now_dt,
            )
        except sqlite3.OperationalError as exc:
            logger.exception("emotion slot database operation failed slot_id=%s", slot_id)
            retryable = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            return cls.mark_failed(
                slot_id,
                "temporary_database_error" if retryable else "structural_database_error",
                str(exc),
                retryable=retryable,
                now=now_dt,
            )
        except sqlite3.DatabaseError as exc:
            logger.exception("emotion slot structural database failure slot_id=%s", slot_id)
            return cls.mark_failed(
                slot_id,
                "structural_database_error",
                str(exc),
                retryable=False,
                now=now_dt,
            )
        except Exception as exc:
            logger.exception("emotion slot execution failed slot_id=%s", slot_id)
            return cls.mark_failed(slot_id, "execution_error", str(exc), now=now_dt)

    @classmethod
    def precheck(cls, slot: dict, *, now=None) -> dict:
        """Validate only the fixed slot and discussion lifecycle."""
        now_dt = _now_dt(now)
        now_text = _fmt(now_dt)
        scope_row = query_one(
            """
            SELECT gsd.id AS discussion_id, gsd.group_id, gsd.session_id,
                   gsd.status AS discussion_status, gsd.started_at,
                   gsd.deadline, gsd.submitted_at, gsd.auto_submitted_at,
                   gsd.expected_student_count, gsd.ready_student_count,
                   es.status AS session_status, es.session_no, es.task_id,
                   es.agent_mode
            FROM group_session_discussions AS gsd
            JOIN experiment_sessions AS es ON es.id=gsd.session_id
            WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
            """,
            (int(slot["discussion_id"]), int(slot["group_id"]), int(slot["session_id"])),
        )
        scope = _row_dict(scope_row)
        if not scope:
            return {"allowed": False, "reason": "discussion_scope_not_found"}
        if scope["discussion_status"] != "running" or scope["session_status"] != "running":
            return {"allowed": False, "reason": "discussion_closed", "scope": scope}
        if scope.get("submitted_at") or scope.get("auto_submitted_at"):
            return {"allowed": False, "reason": "discussion_closed", "scope": scope}
        if scope.get("deadline") and scope["deadline"] <= now_text:
            return {"allowed": False, "reason": "discussion_closed", "scope": scope}
        if not scope.get("started_at") or int(scope.get("expected_student_count") or 0) < 1:
            return {"allowed": False, "reason": "all_members_not_entered", "scope": scope}
        if int(scope.get("ready_student_count") or 0) < int(scope.get("expected_student_count") or 0):
            return {"allowed": False, "reason": "all_members_not_entered", "scope": scope}
        if scope.get("agent_mode") != "emotion":
            return {"allowed": False, "reason": "agent_disabled", "scope": scope}

        snapshot = EmotionWindowService.ensure_slot_snapshot(
            int(slot["id"]), frozen_at=now_text
        )
        current_window_end = parse_dt(snapshot.get("current_window_end"))
        if not current_window_end or current_window_end > now_dt:
            return {
                "allowed": False,
                "reason": "slot_not_complete",
                "scope": scope,
                "snapshot": snapshot,
            }

        newer_slot = query_one(
            """
            SELECT id, slot_index
            FROM emotion_reflection_slots
            WHERE discussion_id=? AND slot_index>?
            ORDER BY slot_index DESC, id DESC LIMIT 1
            """,
            (int(slot["discussion_id"]), int(slot["slot_index"])),
        )
        if newer_slot:
            return {
                "allowed": False,
                "reason": "newer_slot_exists",
                "scope": scope,
                "newer_slot_id": int(newer_slot["id"]),
            }

        from services.session_lifecycle import check_agent_allowed

        allowed, reason = check_agent_allowed(
            int(slot["group_id"]),
            session_id=int(slot["session_id"]),
            task_id=scope.get("task_id"),
            session_no=scope.get("session_no"),
            now=now_dt,
            agent_type="emotion",
        )
        if not allowed:
            mapped = "discussion_closed" if reason in {
                "session_not_active", "document_submitted", "document_locked",
                "group_closed", "group_discussion_closed",
            } else reason
            return {"allowed": False, "reason": mapped, "scope": scope}
        current_messages = snapshot.get("current_messages") or []
        current_sequences = [
            int(item["sequence"])
            for item in current_messages
            if item.get("sequence") is not None
        ]
        return {
            "allowed": True,
            "reason": "active",
            "scope": scope,
            "snapshot": snapshot,
            "latest_student_sequence": max(current_sequences) if current_sequences else 0,
        }

    @classmethod
    def mark_sent(cls, slot_id: int, *, message_id: int, intervention_run_id: int, now=None) -> dict:
        now_text = _fmt(_now_dt(now))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            linked = conn.execute(
                """
                SELECT m.id
                FROM messages AS m
                JOIN intervention_runs AS ir ON ir.id=m.intervention_run_id
                WHERE m.id=? AND ir.id=? AND ir.message_id=m.id
                  AND COALESCE(m.agent_type, '')='emotion'
                """,
                (int(message_id), int(intervention_run_id)),
            ).fetchone()
            if not linked:
                raise ValueError("emotion message/intervention link is incomplete")
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='sent', message_id=?, intervention_run_id=?,
                       completed_at=?, skip_reason=NULL, next_retry_at=NULL,
                       last_error=NULL, updated_at=?
                 WHERE id=? AND status='running'
                """,
                (int(message_id), int(intervention_run_id), now_text, now_text, int(slot_id)),
            )
            row = conn.execute("SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info(
            "emotion slot sent group_id=%s session_id=%s discussion_id=%s slot_index=%s slot_id=%s intervention_run_id=%s message_id=%s",
            row["group_id"], row["session_id"], row["discussion_id"], row["slot_index"], row["id"], intervention_run_id, message_id,
        )
        return {"status": "sent", "slot": _row_dict(row), "message_id": message_id, "run_id": intervention_run_id}

    @classmethod
    def mark_deferred(
        cls,
        slot_id: int,
        reason: str,
        *,
        strategy_run_id: int = None,
        intervention_run_id: int = None,
        now=None,
    ) -> dict:
        """Retry a mandatory slot without publishing it before its cadence."""
        now_dt = _now_dt(now)
        now_text = _fmt(now_dt)
        row = query_one(
            """
            SELECT ers.*, gsd.deadline AS discussion_deadline
            FROM emotion_reflection_slots AS ers
            LEFT JOIN group_session_discussions AS gsd ON gsd.id=ers.discussion_id
            WHERE ers.id=?
            """,
            (int(slot_id),),
        )
        if not row:
            raise ValueError("emotion slot not found")
        scheduled = parse_dt(row["scheduled_at"])
        if not scheduled:
            return cls.mark_failed(
                slot_id,
                "invalid_slot_schedule",
                "scheduled_at is invalid",
                retryable=False,
                now=now_dt,
            )
        configured_deadline = scheduled + timedelta(
            seconds=max(1, int(EMOTION_SLOT_MAX_COMPENSATION_SECONDS or 120))
        )
        next_slot_deadline = scheduled + timedelta(seconds=cls.interval_seconds())
        deadlines = [configured_deadline, next_slot_deadline]
        if row["defer_deadline_at"]:
            parsed_deadline = parse_dt(row["defer_deadline_at"])
            if parsed_deadline:
                deadlines.append(parsed_deadline)
        if row["discussion_deadline"]:
            discussion_deadline = parse_dt(row["discussion_deadline"])
            if discussion_deadline:
                deadlines.append(discussion_deadline)
        deadline_dt = min(deadlines)
        defer_count = int(row["defer_count"] or 0) + 1
        max_defers = max(1, int(EMOTION_SLOT_DEFER_MAX_ATTEMPTS or 6))
        if now_dt >= deadline_dt:
            return cls.mark_expired(
                slot_id,
                "fixed_interval_retry_window_expired",
                intervention_run_id=intervention_run_id,
                now=now_dt,
            )
        if defer_count > max_defers:
            return cls.mark_expired(
                slot_id,
                "fixed_interval_retry_exhausted",
                intervention_run_id=intervention_run_id,
                now=now_dt,
            )

        initial_delay = max(1, int(EMOTION_SLOT_DEFER_INITIAL_SECONDS or 5))
        max_delay = max(initial_delay, int(EMOTION_SLOT_DEFER_MAX_SECONDS or 30))
        delay_seconds = min(max_delay, initial_delay * (2 ** (defer_count - 1)))
        next_retry_dt = min(now_dt + timedelta(seconds=delay_seconds), deadline_dt)
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='deferred', completed_at=NULL, skip_reason=?,
                       next_retry_at=?, enqueued_at=NULL,
                       defer_count=?, defer_deadline_at=?,
                       intervention_run_id=COALESCE(?, intervention_run_id),
                       coordination_strategy_run_id=COALESCE(?, coordination_strategy_run_id),
                       updated_at=?
                 WHERE id=? AND status='running'
                """,
                (
                    str(reason)[:200],
                    _fmt(next_retry_dt),
                    defer_count,
                    _fmt(deadline_dt),
                    intervention_run_id,
                    strategy_run_id,
                    now_text,
                    int(slot_id),
                ),
            )
            final = conn.execute(
                "SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info(
            "emotion slot deferred group_id=%s session_id=%s discussion_id=%s slot_index=%s slot_id=%s reason=%s defer_count=%s next_retry_at=%s defer_deadline_at=%s",
            final["group_id"], final["session_id"], final["discussion_id"],
            final["slot_index"], final["id"], reason, defer_count,
            final["next_retry_at"], final["defer_deadline_at"],
        )
        return {
            "status": "deferred",
            "reason": reason,
            "slot": _row_dict(final),
            "next_retry_at": final["next_retry_at"],
        }

    @staticmethod
    def mark_suppressed(
        slot_id: int,
        reason: str,
        *,
        strategy_run_id: int = None,
        intervention_run_id: int = None,
        now=None,
    ) -> dict:
        return EmotionSlotService._mark_terminal(
            slot_id,
            "suppressed",
            reason,
            strategy_run_id=strategy_run_id,
            intervention_run_id=intervention_run_id,
            now=now,
        )

    @staticmethod
    def mark_expired(
        slot_id: int,
        reason: str,
        *,
        intervention_run_id: int = None,
        now=None,
    ) -> dict:
        return EmotionSlotService._mark_terminal(
            slot_id,
            "expired",
            reason,
            intervention_run_id=intervention_run_id,
            now=now,
        )

    @staticmethod
    def mark_superseded(
        slot_id: int,
        reason: str,
        *,
        superseded_by_slot_id: int = None,
        intervention_run_id: int = None,
        now=None,
    ) -> dict:
        return EmotionSlotService._mark_terminal(
            slot_id,
            "superseded",
            reason,
            superseded_by_slot_id=superseded_by_slot_id,
            intervention_run_id=intervention_run_id,
            now=now,
        )

    @staticmethod
    def _mark_terminal(
        slot_id: int,
        status: str,
        reason: str,
        *,
        strategy_run_id: int = None,
        superseded_by_slot_id: int = None,
        intervention_run_id: int = None,
        now=None,
    ) -> dict:
        now_text = _fmt(_now_dt(now))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status=?, completed_at=?, skip_reason=?,
                       next_retry_at=NULL, enqueued_at=NULL,
                       intervention_run_id=COALESCE(?, intervention_run_id),
                       coordination_strategy_run_id=COALESCE(?, coordination_strategy_run_id),
                       superseded_by_slot_id=COALESCE(?, superseded_by_slot_id),
                       updated_at=?
                 WHERE id=? AND status IN ('pending','running','deferred','failed')
                """,
                (
                    status,
                    now_text,
                    str(reason)[:200],
                    intervention_run_id,
                    strategy_run_id,
                    superseded_by_slot_id,
                    now_text,
                    int(slot_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info(
            "emotion slot %s group_id=%s session_id=%s discussion_id=%s slot_index=%s slot_id=%s reason=%s",
            status,
            row["group_id"] if row else None,
            row["session_id"] if row else None,
            row["discussion_id"] if row else None,
            row["slot_index"] if row else None,
            slot_id,
            reason,
        )
        return {"status": status, "reason": reason, "slot": _row_dict(row)}

    @staticmethod
    def mark_skipped(slot_id: int, reason: str, *, now=None) -> dict:
        now_text = _fmt(_now_dt(now))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='skipped', completed_at=?, skip_reason=?,
                       next_retry_at=NULL, updated_at=?
                 WHERE id=? AND status='running'
                """,
                (now_text, str(reason)[:200], now_text, int(slot_id)),
            )
            row = conn.execute("SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info(
            "emotion slot skipped group_id=%s session_id=%s discussion_id=%s slot_index=%s slot_id=%s skip_reason=%s",
            row["group_id"], row["session_id"], row["discussion_id"], row["slot_index"], row["id"], reason,
        )
        return {"status": "skipped", "reason": reason, "slot": _row_dict(row)}

    @classmethod
    def mark_failed(
        cls,
        slot_id: int,
        reason: str,
        error: str,
        *,
        retryable: bool = None,
        now=None,
    ) -> dict:
        now_dt = _now_dt(now)
        now_text = _fmt(now_dt)
        row = query_one("SELECT retry_count, max_attempts FROM emotion_reflection_slots WHERE id=?", (int(slot_id),))
        attempts_remain = bool(
            row and int(row["retry_count"] or 0) < int(row["max_attempts"] or 1)
        )
        retryable = attempts_remain if retryable is None else bool(retryable and attempts_remain)
        next_retry = _fmt(
            now_dt + timedelta(seconds=max(1, int(EMOTION_SLOT_RETRY_DELAY_SECONDS or 60)))
        ) if retryable else None
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE emotion_reflection_slots
                   SET status='failed', completed_at=?, skip_reason=?,
                       last_error=?, next_retry_at=?, enqueued_at=NULL, updated_at=?
                 WHERE id=? AND status IN ('pending','running','failed')
                """,
                (now_text, str(reason)[:200], str(error)[:500], next_retry, now_text, int(slot_id)),
            )
            final = conn.execute("SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.warning(
            "emotion slot failed group_id=%s session_id=%s discussion_id=%s slot_index=%s slot_id=%s reason=%s retry_count=%s next_retry_at=%s",
            final["group_id"] if final else None,
            final["session_id"] if final else None,
            final["discussion_id"] if final else None,
            final["slot_index"] if final else None,
            slot_id,
            reason,
            final["retry_count"] if final else None,
            next_retry,
        )
        return {"status": "failed", "reason": reason, "slot": _row_dict(final), "retryable": retryable}


__all__ = ["EmotionSlotService"]
