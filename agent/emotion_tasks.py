# -*- coding: utf-8 -*-
"""Huey tasks for the single global emotion-slot scheduler."""

import logging

from huey import crontab

from core import app
from huey_instance import huey

logger = logging.getLogger(__name__)


@huey.periodic_task(crontab(minute="*"))
def scan_due_emotion_reflections_task():
    """Run the only production scanner; it never schedules a future chain."""
    with app.app_context():
        try:
            from services.emotion_slot_service import EmotionSlotService

            return EmotionSlotService.scan_due()
        except Exception as exc:
            logger.exception("scan_due_emotion_reflections failed: %s", exc)
            return {"ok": False, "error": str(exc)}


@huey.task(priority=20)
def execute_emotion_reflection_slot(slot_id: int):
    """Claim and execute one immutable discussion-scoped slot."""
    with app.app_context():
        from services.emotion_slot_service import EmotionSlotService

        return EmotionSlotService.execute_slot(int(slot_id))


@huey.task()
def schedule_emotion_reflection_for_session(session_id):
    """Disabled compatibility entry point for already-queued legacy tasks.

    Session creation/start no longer invokes this function.  Keeping the task
    name registered lets old queue payloads deserialize safely until the
    targeted cleanup utility removes them.
    """
    logger.info(
        "legacy emotion seed ignored session_id=%s reason=single_slot_scanner_enabled",
        session_id,
    )
    return {"skipped": True, "reason": "legacy_recursive_scheduler_disabled"}


@huey.task()
def execute_emotion_reflection_tick(
    group_id,
    session_id,
    task_id,
    session_no,
    tick_index,
    scheduled_at,
    window_start,
    window_end,
    retry_count=0,
):
    """Disabled compatibility consumer for legacy recursive tick payloads."""
    logger.info(
        "legacy emotion tick ignored group_id=%s session_id=%s tick_index=%s reason=single_slot_scanner_enabled",
        group_id,
        session_id,
        tick_index,
    )
    return {
        "status": "skipped",
        "reason": "legacy_recursive_scheduler_disabled",
        "group_id": group_id,
        "session_id": session_id,
        "tick_index": tick_index,
    }


def _is_duplicate_tick(group_id, session_id, tick_index):
    """Read-only legacy audit helper retained for historical tests/tools."""
    from db import query_all, query_one

    if tick_index is None:
        return False
    slot = query_one(
        """
        SELECT id FROM emotion_reflection_slots
        WHERE group_id=? AND session_id=? AND slot_index=?
        LIMIT 1
        """,
        (group_id, session_id, tick_index),
    )
    if slot:
        return True
    existing_run = query_one(
        """
        SELECT id FROM intervention_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(agent_type, '')='emotion'
          AND tick_index=?
          AND status NOT IN ('CANCELLED','FAILED','EXPIRED','STALE')
        ORDER BY id DESC LIMIT 1
        """,
        (group_id, session_id, tick_index),
    )
    if existing_run:
        return True
    rows = query_all(
        """
        SELECT 1 FROM agent_research_events
        WHERE agent_type='emotion' AND group_id=? AND session_id=?
          AND trigger_type IN ('scheduled_10min','emotion_time_slot')
          AND trigger_reason_json LIKE ? LIMIT 1
        """,
        (group_id, session_id, '%%"tick_index": %d,%%' % tick_index),
    )
    return len(rows) > 0


__all__ = [
    "execute_emotion_reflection_slot",
    "execute_emotion_reflection_tick",
    "scan_due_emotion_reflections_task",
    "schedule_emotion_reflection_for_session",
]
