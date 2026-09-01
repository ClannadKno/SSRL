# -*- coding: utf-8 -*-
"""Batch 8 coverage for one-ID three-stage pipeline observability."""

from __future__ import annotations

import importlib
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
def batch8_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=9808, member_count=1)
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


def _event_rows(db, pipeline_id):
    rows = db.query_all(
        """
        SELECT event, details_json
        FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=?
        ORDER BY id
        """,
        (pipeline_id,),
    )
    return [(row["event"], json.loads(row["details_json"])) for row in rows]


def _last_event(db, pipeline_id, event):
    row = db.query_one(
        """
        SELECT details_json
        FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=? AND event=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (pipeline_id, event),
    )
    assert row is not None, f"missing telemetry event: {event}"
    return json.loads(row["details_json"])


def test_initial_stage3_success_has_common_pipeline_context(batch8_env):
    db, scope = batch8_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=_FakeGateway(
            [_core_output("ER-001", "分歧先回到证据上，大家各说一个最担心的条件。")]
        ),
    )

    assert result["stage3_status"] == "SUCCEEDED"
    details = _last_event(db, pipeline_id, "stage3_finished")
    assert details["pipeline_run_id"] == pipeline_id
    assert details["group_id"] == scope["group_id"]
    assert details["session_id"] == scope["session_id"]
    assert details["discussion_id"] == scope["discussion_id"]
    assert details["task_id"] == scope["task_id"]
    assert details["trigger_message_id"] is not None
    assert details["cutoff_student_sequence"] == 3
    assert details["canonical_substate"] == "interpersonal_conflict"
    assert set(details["allowed_strategy_ids"]) == {
        "ER-001",
        "ER-007",
        "EE-001",
        "SS-004",
    }
    assert details["selected_strategy_id"] == "ER-001"
    assert details["stage3_attempt_count"] == 1
    assert details["stage3_success"] is True


def test_repair_success_uses_standard_categories(batch8_env):
    db, scope = batch8_env
    stage3 = importlib.import_module("services.three_stage_stage3")

    success_id = _insert_stage2_pipeline(db, scope)
    success = stage3.Stage3PipelineService.execute_for_pipeline(
        success_id,
        gateway=_FakeGateway(
            [
                _core_output("ER-999", "先随便试一下。"),
                _core_output("ER-001", "分歧先回到证据上，大家各说一个条件。"),
            ]
        ),
    )
    assert success["stage3_status"] == "SUCCEEDED"
    repair_details = _last_event(db, success_id, "stage3_repair_finished")
    assert repair_details["failure_category"] is None
    finished_details = _last_event(db, success_id, "stage3_finished")
    assert finished_details["stage3_attempt_count"] == 2
    assert finished_details["stage3_success"] is True


def test_repair_failure_uses_repair_failed_category(batch8_env):
    db, scope = batch8_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    failure_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="frustration",
        candidates=["ER-002", "EE-003", "SS-006", "EA-007"],
    )
    failure = stage3.Stage3PipelineService.execute_for_pipeline(
        failure_id,
        gateway=_FakeGateway(
            [
                _core_output("ER-999", "先随便试一下。"),
                _core_output("ER-998", "再随便试一下。"),
            ]
        ),
    )
    assert failure["stage3_status"] == "FAILED"
    assert failure["failure_category"] == "repair_failed"
    summary = _last_event(db, failure_id, "pipeline_failed")
    assert summary["stage3_failure_category"] == "repair_failed"
    assert summary["stage3_attempt_count"] == 2


def test_publish_success_summary_contains_message_and_lease(batch8_env):
    db, scope = batch8_env
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is True, result
    summary = _last_event(db, pipeline_id, "pipeline_completed")
    assert summary["publish_gate_result"] == "published"
    assert summary["publish_gate_allowed"] is True
    assert summary["published_message_id"] == result["message_id"]
    assert summary["lease_acquired"] is True
    assert summary["lease_released"] is True
    assert summary["lease_hold_duration_ms"] >= 0
    gate = _last_event(db, pipeline_id, "publish_gate_evaluated")
    assert gate["publish_gate_allowed"] is True


def test_stale_publish_summary_has_runtime_category(batch8_env, monkeypatch):
    db, scope = batch8_env
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    monkeypatch.setattr(scheduler, "_enqueue_batch", lambda *args, **kwargs: None)
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)
    _add_message(db, scope, 4, "新的学生证据使旧窗口失效。")

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is False
    assert result["failure_category"] == "stale_new_student_message", result
    summary = _last_event(db, pipeline_id, "pipeline_completed")
    assert summary["publish_gate_result"] == "blocked"
    assert summary["publish_gate_reason"] == "stale_new_student_message"
    assert summary["lease_released"] is True


def test_invalid_lease_publish_summary_is_distinct(batch8_env):
    db, scope = batch8_env
    publish = importlib.import_module("services.three_stage_publish")
    pipeline_id = _ready_pipeline(db, scope)
    db.execute(
        "UPDATE strategy_pipeline_runs SET room_lock_token=? WHERE id=?",
        ("invalid-lease-token", pipeline_id),
    )

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is False
    assert result["failure_category"] == "invalid_room_lease", result
    gate = _last_event(db, pipeline_id, "publish_gate_evaluated")
    assert gate["publish_gate_reason"] == "invalid_room_lease"
    summary = _last_event(db, pipeline_id, "pipeline_completed")
    assert summary["publish_gate_reason"] == "invalid_room_lease"
    assert summary["lease_released"] is False
