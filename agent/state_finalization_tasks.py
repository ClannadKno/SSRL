# -*- coding: utf-8 -*-
"""Huey tasks for one-shot collaboration state finalization."""

import logging

from core import app
from huey_instance import huey

logger = logging.getLogger(__name__)


@huey.task(retries=1, retry_delay=30, priority=60)
def finalize_collaboration_states_task(group_id: int, session_id: int, reason: str):
    """Run final tail state analysis for one group/session."""
    with app.app_context():
        try:
            from services.collaboration_state_finalization_service import finalize_collaboration_states

            return finalize_collaboration_states(
                group_id=group_id,
                session_id=session_id,
                reason=reason,
            )
        except Exception as exc:
            logger.exception(
                "finalize_collaboration_states_task failed group=%s session=%s reason=%s",
                group_id,
                session_id,
                reason,
            )
            return {"ok": False, "error": str(exc), "group_id": group_id, "session_id": session_id}


@huey.task()
def state_finalization_smoke():
    """Smoke test for state_finalization_tasks module."""
    with app.app_context():
        return "state_finalization_smoke ok"
