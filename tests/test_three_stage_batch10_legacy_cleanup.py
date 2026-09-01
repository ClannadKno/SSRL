# -*- coding: utf-8 -*-
"""Batch 10 guards for retired strategy publish paths."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta

from tests.helpers import seed_running_session


def test_legacy_teacher_suggestion_push_endpoint_is_disabled(teacher_login):
    client, headers = teacher_login

    response = client.post("/api/agent/suggestion/999/push", headers=headers)

    assert response.status_code == 410
    assert response.get_json()["code"] == "LEGACY_SUGGESTION_PUSH_DISABLED"


def test_legacy_direct_publish_helpers_do_not_write_agent_messages(db_and_app):
    db, _app_module, _client = db_and_app

    from agent.trigger import push_agent_suggestion, push_intervention as agent_push
    from services.intervention_execution import execute_intervention

    assert push_agent_suggestion(12345, teacher_user_id=1) is None
    assert agent_push(1, 1, pushed_by_user_id=1) is None
    assert db.push_intervention(1, 1, pushed_by_user_id=1) is None
    assert execute_intervention(
        1,
        {"id": 1},
        {"message": "legacy"},
        suggestion={"id": 1, "message": "legacy"},
    ) is None

    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM intervention_logs")["c"] == 0


def test_legacy_state_batch_direct_bridge_is_disabled_by_default(db_and_app):
    db, _app_module, _client = db_and_app
    service = importlib.import_module("services.assessment_batch_intervention_service")
    result = service.AssessmentBatchInterventionService.execute(99999)

    assert result["skipped"] is True
    assert result["reason"] == "legacy_state_batch_direct_publish_disabled"
    assert result["assessment_batch_id"] == 99999
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM intervention_runs")["c"] == 0


def test_legacy_fallback_does_not_publish_and_releases_room_lock(db_and_app):
    db, _app_module, _client = db_and_app
    scope = seed_running_session(db, session_no=1011, member_count=1)
    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, session_no, task_id, status,
            dry_run, lock_acquired, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            "LOCKED",
            0,
            1,
            db.now_str(),
        ),
    )
    lock_token = "legacy-fallback-token"
    expires = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """
        UPDATE groups
           SET state='AI_INTERVENING', lock_token=?, lock_expires_at=?,
               active_intervention_run_id=?
         WHERE id=?
        """,
        (lock_token, expires, run_id, scope["group_id"]),
    )

    from services.intervention_pipeline_v2.fallback_service import FallbackService

    applied = FallbackService.apply_fallback(
        intervention_run_id=run_id,
        group_id=scope["group_id"],
        strategy={"fallback_template": "legacy fallback should not publish"},
        lock_token=lock_token,
        reason="unit_test",
    )

    assert applied is False
    run = db.query_one("SELECT * FROM intervention_runs WHERE id=?", (run_id,))
    room = db.query_one("SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?", (scope["group_id"],))
    assert run["status"] == "FAILED"
    assert run["fallback_used"] == 0
    assert run["failure_reason"] == "legacy_direct_fallback_publish_disabled"
    assert room["state"] == "OPEN"
    assert room["lock_token"] is None
    assert room["active_intervention_run_id"] is None
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0
