# -*- coding: utf-8 -*-
"""Batch 9 contract coverage for the simplified Stage3 publish pipeline."""

from __future__ import annotations

import importlib
import inspect
import json

import pytest

from tests.helpers import seed_running_session
from tests.test_three_stage_batch5_stage3 import (
    _FakeGateway,
    _core_output,
    _insert_stage2_pipeline,
)
from tests.test_three_stage_batch6_decision_gate import _add_message, _ready_pipeline


@pytest.fixture
def batch9_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=9909, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
        SET strategy_agent_enabled=1, agent_intervention_enabled=1
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


def _release_count(db, pipeline_id):
    row = db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=? AND stage='lock' AND event='room_lock_released'
        """,
        (pipeline_id,),
    )
    return int(row["count"])


def _group_lock(db, group_id):
    return db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (group_id,),
    )


def test_stage3_schema_contract_accepts_wrappers_and_rejects_core_errors(batch9_env):
    db, scope = batch9_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope)
    context = stage3.build_stage3_context(pipeline_id)
    text = "大家先说一条证据，再一起比较哪个条件更关键。"
    core = _core_output("ER-001", text)

    accepted = [
        core,
        {
            **core,
            "selection_reason": "legacy field is ignored",
            "supporting_strategy_ids": ["SS-004"],
        },
        "说明如下：\n```json\n"
        + json.dumps(core, ensure_ascii=False)
        + "\n```\n以上。",
    ]
    for output in accepted:
        parsed = stage3.parse_stage3_output(output, context)
        assert parsed["valid"] is True
        assert parsed["output"] == core

    rejected = [
        ({**core, "selected_strategy_id": "ER-999"}, "strategy_not_candidate"),
        ({**core, "intervention_text": ""}, "missing_intervention_text"),
        ({"intervention_text": text}, "missing_selected_strategy_id"),
    ]
    for output, reason in rejected:
        parsed = stage3.parse_stage3_output(output, context)
        assert parsed["valid"] is False
        assert parsed["reason"] == reason


def test_stage3_repair_is_single_and_total_gateway_budget_is_two(batch9_env):
    db, scope = batch9_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=True)
    gateway = _FakeGateway(
        [
            _core_output("ER-999", "先随便试一下。"),
            _core_output("ER-001", "大家先回到证据上，再比较哪个条件更关键。"),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "SUCCEEDED"
    assert len(gateway.calls) == 2
    assert [call["max_attempts_override"] for call in gateway.calls] == [1, 1]


def test_stage3_repair_failure_stops_at_two_calls_and_releases_lock(batch9_env):
    db, scope = batch9_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=True)
    gateway = _FakeGateway(
        [
            _core_output("ER-999", "先随便试一下。"),
            _core_output("ER-998", "再随便试一下。"),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "FAILED"
    assert len(gateway.calls) == 2
    assert _release_count(db, pipeline_id) == 1
    row = db.query_one(
        "SELECT final_status, room_lock_released_at FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["final_status"] == "FAILED"
    assert row["room_lock_released_at"] is not None
    assert dict(_group_lock(db, scope["group_id"])) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }


@pytest.mark.parametrize(
    "text",
    [
        "大家先回到预算条件，看看哪一项是当前最需要保留的。",
        "刚才这个限制确实容易让人卡住，不过它也让取舍更清楚了，大家可以先确定最想保留的一项，再从这里调整。",
        "观点不同很正常，大家可以先说清各自最担心的条件，再看看哪些部分能够合并。",
    ],
)
def test_publish_allows_required_text_without_content_validator(
    batch9_env,
    monkeypatch,
    text,
):
    db, scope = batch9_env
    publish = importlib.import_module("services.three_stage_publish")
    monkeypatch.setattr(
        publish.StudentFacingInterventionValidator,
        "validate",
        lambda *args, **kwargs: pytest.fail("Publish Gate must not validate text content"),
    )
    pipeline_id = _ready_pipeline(db, scope, text=text)

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is True
    row = db.query_one(
        "SELECT publish_status, validated_intervention_text FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["publish_status"] == "PUBLISHED"
    assert row["validated_intervention_text"] == text
    assert db.query_one(
        "SELECT COUNT(*) AS count FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["count"] == 1


def test_publish_gate_has_no_legacy_text_validator_call(batch9_env):
    publish = importlib.import_module("services.three_stage_publish")
    source = inspect.getsource(publish.InterventionDecisionGate.evaluate)

    assert "StudentFacingInterventionValidator" not in source
    assert "validate_stage3_text" not in source


def test_publish_runtime_gate_rejects_stale_and_releases_lock(batch9_env, monkeypatch):
    db, scope = batch9_env
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    monkeypatch.setattr(scheduler, "_enqueue_batch", lambda *args, **kwargs: None)
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)
    _add_message(db, scope, 4, "新的学生证据使旧窗口失效。")

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is False
    assert result["failure_category"] == "stale_new_student_message"
    assert _release_count(db, pipeline_id) == 1
    assert dict(_group_lock(db, scope["group_id"])) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }


def test_publish_runtime_gate_rejects_invalid_lease_without_releasing_other_owner(
    batch9_env,
):
    db, scope = batch9_env
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)
    group_before = _group_lock(db, scope["group_id"])
    db.execute(
        "UPDATE strategy_pipeline_runs SET room_lock_token=? WHERE id=?",
        ("invalid-lease-token", pipeline_id),
    )

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is False
    assert result["failure_category"] == "invalid_room_lease"
    assert _release_count(db, pipeline_id) == 0
    group_after = _group_lock(db, scope["group_id"])
    assert group_after["state"] == "AI_INTERVENING"
    assert group_after["lock_token"] == group_before["lock_token"]
    assert group_after["active_intervention_run_id"] == group_before[
        "active_intervention_run_id"
    ]


def test_publish_runtime_gate_rejects_closed_session_and_releases_lock(batch9_env):
    db, scope = batch9_env
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)
    db.execute(
        "UPDATE experiment_sessions SET status='ended' WHERE id=?",
        (scope["session_id"],),
    )

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is False
    assert result["failure_category"] == "session_closed"
    assert _release_count(db, pipeline_id) == 1
    row = db.query_one(
        "SELECT final_status, publish_status FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert dict(row) == {"final_status": "SUPPRESSED", "publish_status": "SKIPPED"}
    assert _group_lock(db, scope["group_id"])["state"] == "OPEN"


def test_publish_success_is_idempotent_and_releases_one_lock(batch9_env):
    db, scope = batch9_env
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)

    first = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)
    second = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert first["published"] is True
    assert second["duplicate"] is True
    assert _release_count(db, pipeline_id) == 1
    assert db.query_one(
        "SELECT COUNT(*) AS count FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["count"] == 1
    assert _group_lock(db, scope["group_id"])["state"] == "OPEN"


@pytest.mark.parametrize(
    "outputs",
    [
        pytest.param(
            [_core_output("ER-001", "大家先回到证据上，再比较哪个条件更关键。")],
            id="initial-success",
        ),
        pytest.param(
            [
                _core_output("ER-999", "先随便试一下。"),
                _core_output("ER-001", "大家先回到证据上，再比较哪个条件更关键。"),
            ],
            id="repair-success",
        ),
    ],
)
def test_stage3_success_then_publisher_has_one_terminal_lock_release(
    batch9_env,
    outputs,
):
    db, scope = batch9_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=True)

    stage3_result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=_FakeGateway(outputs),
    )
    assert stage3_result["stage3_status"] == "SUCCEEDED"
    assert _release_count(db, pipeline_id) == 0
    assert _group_lock(db, scope["group_id"])["state"] == "AI_INTERVENING"

    publish_result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(
        pipeline_id
    )

    assert publish_result["published"] is True
    assert _release_count(db, pipeline_id) == 1
    row = db.query_one(
        "SELECT final_status, room_lock_released_at FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["final_status"] == "PUBLISHED"
    assert row["room_lock_released_at"] is not None
    assert _group_lock(db, scope["group_id"])["state"] == "OPEN"
