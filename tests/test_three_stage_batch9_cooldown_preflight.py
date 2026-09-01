# -*- coding: utf-8 -*-
"""Batch 9 coverage for the Stage 3 cooldown preflight boundary."""

from __future__ import annotations

import importlib

import pytest

from tests.helpers import seed_running_session
from tests.test_three_stage_batch5_stage3 import (
    _FakeGateway,
    _add_message,
    _core_output,
    _insert_stage2_pipeline,
)


@pytest.fixture
def batch9_preflight_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=9909, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy',
               strategy_agent_enabled=1,
               emotion_agent_enabled=0,
               agent_intervention_enabled=1
         WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1 WHERE id=?",
        (scope["group_id"],),
    )
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    return db, scope


def _add_agent_message(db, scope, *, agent_type="strategy", sequence=4):
    return db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, role, sender_type, sequence,
            created_at, session_no, task_id, session_id, agent_type,
            trigger_source, discussion_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["student_id"],
            "先停一下，回到当前讨论的证据。",
            "agent",
            "agent",
            sequence,
            db.now_str(),
            scope["session_no"],
            scope["task_id"],
            scope["session_id"],
            agent_type,
            agent_type,
            scope["discussion_id"],
        ),
    )


def _group_lock(db, group_id):
    return db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (group_id,),
    )


def test_cooldown_preflight_skips_stage3_and_room_lease(batch9_preflight_env):
    db, scope = batch9_preflight_env
    publish = importlib.import_module("services.three_stage_publish")
    previous_pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="cognitive_disagreement",
        locked=False,
    )
    db.execute(
        "UPDATE messages SET sequence=sequence+10 WHERE discussion_id=?",
        (scope["discussion_id"],),
    )
    published_message_id = _add_agent_message(
        db,
        scope,
        agent_type="strategy",
        sequence=14,
    )
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET publish_status='PUBLISHED', final_status='PUBLISHED',
               stage3_status='SUCCEEDED', selected_strategy_id='ER-001',
               validated_intervention_text='请回到当前证据继续讨论。',
               published_message_id=?, published_at=?, updated_at=?
         WHERE id=?
        """,
        (
            published_message_id,
            db.now_str(),
            db.now_str(),
            previous_pipeline_id,
        ),
    )
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=False)

    preflight = publish.InterventionDecisionGate.evaluate_preflight(pipeline_id)
    assert preflight["allowed"] is False
    assert preflight["preflight_gate_result"] == "blocked"
    assert preflight["preflight_gate_reason"] == "strategy_cooldown"
    assert preflight["preflight_terminal_reason"] == "SUPPRESSED_COOLDOWN"

    result = publish.ThreeStageInterventionPublisher.finish_preflight(
        pipeline_id, preflight
    )

    assert result["failure_code"] == "SUPPRESSED_COOLDOWN"
    assert result["stage3_skipped_by_preflight"] is True
    assert result["lock_skipped_by_preflight"] is True
    assert result["llm_call_saved"] is True
    row = db.query_one(
        """
        SELECT stage3_status, publish_status, final_status, skip_reason,
               room_lock_acquired_at, room_lock_released_at
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (pipeline_id,),
    )
    assert dict(row) == {
        "stage3_status": "SKIPPED",
        "publish_status": "SKIPPED",
        "final_status": "SUPPRESSED",
        "skip_reason": "SUPPRESSED_COOLDOWN",
        "room_lock_acquired_at": None,
        "room_lock_released_at": None,
    }
    assert dict(_group_lock(db, scope["group_id"])) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }
    events = db.query_all(
        """
        SELECT stage, event, details_json
        FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=?
        ORDER BY id
        """,
        (pipeline_id,),
    )
    assert any(row["event"] == "preflight_gate_evaluated" for row in events)
    assert any(row["event"] == "stage3_skipped_by_preflight" for row in events)
    assert not any(row["event"] == "room_lock_acquired" for row in events)
    assert not any(row["stage"] == "stage3" and "llm" in row["event"] for row in events)


def test_emotion_message_does_not_trigger_strategy_cooldown(batch9_preflight_env):
    db, scope = batch9_preflight_env
    router_module = importlib.import_module("services.state_strategy_router")
    publish = importlib.import_module("services.three_stage_publish")
    route = router_module.StateStrategyRouter().route("frustration")
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="frustration",
        candidates=list(route.strategy_pool),
        locked=False,
    )
    _add_agent_message(db, scope, agent_type="emotion")

    preflight = publish.InterventionDecisionGate.evaluate_preflight(pipeline_id)

    assert preflight["allowed"] is True
    assert preflight["preflight_gate_reason"] == "allowed"
    assert preflight["preflight_terminal_reason"] is None


def test_cooldown_clear_runs_stage3_and_final_gate_still_catches_stale(
    batch9_preflight_env,
):
    db, scope = batch9_preflight_env
    lease = importlib.import_module(
        "services.intervention_pipeline_v2.room_lease_service"
    )
    publish = importlib.import_module("services.three_stage_publish")
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=False)

    preflight = publish.InterventionDecisionGate.evaluate_preflight(pipeline_id)
    assert preflight["allowed"] is True
    assert preflight["preflight_gate_result"] == "allowed"
    assert preflight["stage3_skipped_by_preflight"] is False
    assert preflight["lock_skipped_by_preflight"] is False
    assert preflight["llm_call_saved"] is False
    publish.ThreeStageInterventionPublisher.record_preflight(pipeline_id, preflight)

    claimed = lease.RoomLeaseService.claim_strategy_pipeline(pipeline_id)
    assert claimed["acquired"] is True
    stage3_result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=_FakeGateway(
            [_core_output("ER-001", "大家先回到证据上，再比较哪个条件更关键。")]
        ),
    )
    assert stage3_result["stage3_status"] == "SUCCEEDED"

    _add_message(db, scope, 4, "新的学生消息让旧窗口失效。")
    published = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(
        pipeline_id
    )

    assert published["published"] is False
    assert published["failure_category"] == "stale_new_student_message"
    row = db.query_one(
        "SELECT stage3_status, final_status, skip_reason FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert dict(row) == {
        "stage3_status": "SUCCEEDED",
        "final_status": "STALE",
        "skip_reason": "STALE_NEW_STUDENT_MESSAGE",
    }
