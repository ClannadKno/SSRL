# -*- coding: utf-8 -*-
"""Batch 8 coverage for post-intervention observation and state closure."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest

from tests.helpers import seed_running_session
from tests.test_three_stage_batch6_decision_gate import _publish, _ready_pipeline


def _time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def batch8_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)

    scope = seed_running_session(db, session_no=9808, member_count=1, limit_minutes=60)
    db.execute(
        """
        UPDATE experiment_sessions
        SET strategy_agent_enabled=1,
            agent_intervention_enabled=1
        WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1 WHERE id=?",
        (scope["group_id"],),
    )
    now = datetime.now().replace(microsecond=0)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, deadline,
            expected_student_count, ready_student_count,
            created_at, updated_at
        ) VALUES(?,?,'running',?,?,1,1,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            _time(now - timedelta(minutes=10)),
            _time(now + timedelta(minutes=30)),
            _time(now),
            _time(now),
        ),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    return db, scope


def _publish_observed_intervention(db, scope):
    pipeline_id = _ready_pipeline(
        db,
        scope,
        canonical="interpersonal_conflict",
        selected_strategy_id="ER-001",
        candidates=["ER-001", "EE-001", "SS-004", "ER-007"],
    )
    result = _publish(pipeline_id)
    assert result["published"] is True
    message = db.query_one("SELECT * FROM messages WHERE id=?", (result["message_id"],))
    return pipeline_id, result, dict(message)


def _student_message(db, scope, content: str, *, created_at: str = None) -> dict:
    message = db.create_message(
        scope["group_id"],
        scope["student_id"],
        content,
        role="student",
        sender_type="student",
        session_no=scope["session_no"],
        task_id=scope["task_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        created_at=created_at,
    )
    return dict(message)


def _stage2_payload(canonical: str, *, start_sequence: int, end_sequence: int) -> dict:
    schema = importlib.import_module("services.three_stage_schema")
    route = schema.route_for_sub_state(canonical)
    evidence = [start_sequence] if start_sequence == end_sequence else [start_sequence, end_sequence]
    segment = {
        "state_code": schema.legacy_state_for_sub_state(canonical),
        "raw_sub_state": canonical,
        "raw_sub_state_code": canonical,
        "canonical_sub_state": canonical,
        "canonical_sub_state_code": canonical,
        "secondary_tags": [],
        "confidence": 0.9,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "evidence_message_ids": evidence,
        "evidence_sequences": evidence,
        "reason": "batch8 observation fixture",
        "detected_self_regulation": False,
        "is_active_at_window_end": True,
        "is_active_at_batch_end": True,
    }
    inhibition_id = route.get("inhibition_strategy_id")
    return {
        "schema_version": "stage2.v1",
        "analysis_scope": {
            "candidate_start_sequence": start_sequence,
            "candidate_end_sequence": end_sequence,
            "input_cutoff_student_sequence": end_sequence,
            "trigger_source": "post_intervention_observation",
        },
        "segments": [segment],
        "active_segment_index": 0,
        "active_sub_state": {
            "raw_sub_state": canonical,
            "canonical_sub_state": canonical,
            "secondary_tags": [],
            "confidence": 0.9,
            "start_sequence": start_sequence,
            "end_sequence": end_sequence,
            "evidence_message_ids": evidence,
            "detected_self_regulation": False,
        },
        "should_intervene": bool(route["should_intervene"]),
        "inhibition": {
            "is_inhibited": bool(inhibition_id),
            "strategy_id": inhibition_id,
            "reason": "non-intervention observation route" if inhibition_id else None,
        },
        "candidate_strategy_ids": list(route["candidate_strategy_ids"]),
        "decision_reason": "batch8 observation fixture",
    }


def _run_observation_round(db, scope, *, start_sequence: int, end_sequence: int, canonical: str) -> dict:
    service = importlib.import_module("services.state_assessment_batch_service").StateAssessmentBatchService
    stage2 = importlib.import_module("services.three_stage_stage2").Stage2PipelineService
    observation = importlib.import_module("services.three_stage_observation")
    payload = _stage2_payload(
        canonical,
        start_sequence=start_sequence,
        end_sequence=end_sequence,
    )
    created = service.create_batch(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        candidate_start_sequence=start_sequence,
        candidate_end_sequence=end_sequence,
        context_start_sequence=1,
        context_end_sequence=end_sequence,
        trigger_type="post_intervention_observation",
        trigger_sequence=end_sequence,
        model="batch8-stage2-model",
        prompt_version="stage2.v1",
        request_priority=300,
    )
    assert created["created"] is True
    claimed = service.claim_batch(created["batch"]["id"])
    assert claimed["claimed"] is True
    saved = service.save_successful_segments(
        claimed["batch"]["id"],
        payload["segments"],
        raw_response=json.dumps(payload, ensure_ascii=False),
        parsed_response=payload,
        model="batch8-stage2-model",
        prompt_version="stage2.v1",
    )
    pipeline = stage2.persist_success(
        batch=saved["batch"],
        stage2_result=payload,
        llm_meta={
            "success": True,
            "model_name": "batch8-stage2-model",
            "prompt_version": "stage2.v1",
            "raw_response": json.dumps(payload, ensure_ascii=False),
        },
        saved_segments=saved["segments"],
    )
    observation_result = observation.record_observation_assessment(
        observation_pipeline_run_id=pipeline["pipeline_run_id"],
        batch=saved["batch"],
        stage2_result=payload,
    )
    return {
        "batch": saved["batch"],
        "pipeline": pipeline,
        "observation": observation_result,
        "payload": payload,
    }


def test_publish_marks_pipeline_and_cursor_observing(batch8_env):
    db, scope = batch8_env
    pipeline_id, result, message = _publish_observed_intervention(db, scope)

    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    details = json.loads(row["observation_details_json"])
    assert result["observation_started"]["updated"] is True
    assert row["observation_status"] == "observing"
    assert row["observation_window_start_sequence"] == message["sequence"] + 1
    assert row["observation_previous_sub_state_code"] == "interpersonal_conflict"
    assert details["selected_strategy_id"] == "ER-001"
    cursor = db.query_one(
        """
        SELECT observation_status, observation_started_sequence, last_intervention_sequence
        FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert dict(cursor) == {
        "observation_status": "observing",
        "observation_started_sequence": None,
        "last_intervention_sequence": message["sequence"],
    }


def test_observation_context_carries_previous_state_strategy_and_window(batch8_env):
    db, scope = batch8_env
    pipeline_id, _result, message = _publish_observed_intervention(db, scope)
    first = _student_message(db, scope, "我先把刚才的担心改成一个可比较条件。")

    monitoring = importlib.import_module("services.discussion_pipeline_v2.monitoring_service")
    context = monitoring._build_state_detector_context(
        {
            "session_id": scope["session_id"],
            "discussion_id": scope["discussion_id"],
        },
        group_id=scope["group_id"],
        cutoff_sequence=first["sequence"],
        previous_cutoff=message["sequence"],
        scope={
            **scope,
            "trigger_type": "post_intervention_observation",
        },
    )

    observed = context["post_intervention_observation"]
    assert observed["previous_pipeline_run_id"] == pipeline_id
    assert observed["previous_sub_state"] == "interpersonal_conflict"
    assert observed["selected_strategy_id"] == "ER-001"
    assert observed["published_message_id"] == message["id"]
    assert observed["observation_window_start_sequence"] == message["sequence"] + 1
    assert observed["first_response_sequence"] == first["sequence"]
    assert context["recent_intervention"]["pipeline_run_id"] == pipeline_id
    assert context["recent_intervention"]["canonical_sub_state_code"] == "interpersonal_conflict"


def test_student_response_advances_only_matching_session_cursor(batch8_env):
    db, scope = batch8_env
    _pipeline_id, _result, message = _publish_observed_intervention(db, scope)
    other_task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Other session task", "Discuss separately", 60, db.now_str()),
    )
    other_session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            scope["session_no"] + 1,
            "discussion",
            other_task_id,
            "ended",
            db.now_str(),
            60,
            db.now_str(),
            db.now_str(),
        ),
    )
    other_discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'closed',?,?,?)
        """,
        (
            other_session_id,
            scope["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    db.execute(
        """
        INSERT INTO discussion_assessment_cursors(
            group_id, session_id, discussion_id,
            last_finalized_student_sequence, last_scheduled_student_sequence,
            observation_started_sequence, observation_status,
            last_intervention_sequence, updated_at
        ) VALUES(?,?,?,0,0,NULL,'observing',?,?)
        """,
        (
            scope["group_id"],
            other_session_id,
            other_discussion_id,
            message["sequence"],
            db.now_str(),
        ),
    )

    first = _student_message(db, scope, "这条回复只属于当前 discussion。")

    current_cursor = db.query_one(
        """
        SELECT observation_started_sequence
        FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    other_cursor = db.query_one(
        """
        SELECT observation_started_sequence
        FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], other_session_id, other_discussion_id),
    )
    assert current_cursor["observation_started_sequence"] == first["sequence"]
    assert other_cursor["observation_started_sequence"] is None


def test_single_recovery_message_keeps_observation_open(batch8_env):
    db, scope = batch8_env
    pipeline_id, _result, message = _publish_observed_intervention(db, scope)
    first = _student_message(db, scope, "我同意先列证据，但还需要等另一个人确认。")

    outcome = _run_observation_round(
        db,
        scope,
        start_sequence=first["sequence"],
        end_sequence=first["sequence"],
        canonical="standard",
    )

    assert outcome["observation"]["observation_result"] == "insufficient_evidence"
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["observation_status"] == "observing"
    assert row["observation_result"] == "insufficient_evidence"
    assert row["observation_first_response_sequence"] == first["sequence"]
    assert row["observation_first_response_seconds"] is not None
    cursor = db.query_one(
        "SELECT observation_status, observation_started_sequence FROM discussion_assessment_cursors WHERE group_id=? AND session_id=? AND discussion_id=?",
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert dict(cursor) == {
        "observation_status": "observing",
        "observation_started_sequence": first["sequence"],
    }
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 1
    assert message["sequence"] < first["sequence"]


def test_two_recovery_messages_complete_observation_window(batch8_env):
    db, scope = batch8_env
    pipeline_id, _result, _message = _publish_observed_intervention(db, scope)
    first = _student_message(db, scope, "我先按证据重新说。")
    second = _student_message(db, scope, "我也同意，这样我们能继续推进。")

    outcome = _run_observation_round(
        db,
        scope,
        start_sequence=first["sequence"],
        end_sequence=second["sequence"],
        canonical="standard",
    )

    assert outcome["observation"]["observation_result"] == "recovered"
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["observation_status"] == "completed"
    assert row["observation_result"] == "recovered"
    assert row["observation_window_end_sequence"] == second["sequence"]
    assert row["observation_completed_at"]
    cursor = db.query_one(
        "SELECT observation_status FROM discussion_assessment_cursors WHERE group_id=? AND session_id=? AND discussion_id=?",
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert cursor["observation_status"] == "inactive"


def test_persistent_risk_records_reintervention_pipeline_without_direct_publish(batch8_env):
    db, scope = batch8_env
    pipeline_id, _result, _message = _publish_observed_intervention(db, scope)
    first = _student_message(db, scope, "你还是没有回应我的问题，我觉得这个讨论没法继续。")

    outcome = _run_observation_round(
        db,
        scope,
        start_sequence=first["sequence"],
        end_sequence=first["sequence"],
        canonical="interpersonal_conflict",
    )

    reintervention_id = outcome["pipeline"]["pipeline_run_id"]
    assert outcome["observation"]["observation_result"] == "persistent_risk"
    observed = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert observed["observation_status"] == "completed"
    assert observed["observation_result"] == "persistent_risk"
    assert observed["observation_reintervention_run_id"] == reintervention_id
    current = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (reintervention_id,))
    assert current["parent_run_id"] == pipeline_id
    assert current["stage2_status"] == "SUCCEEDED"
    assert current["stage3_status"] == "PENDING"
    assert current["publish_status"] == "NOT_READY"
    assert current["final_status"] == "PENDING_STAGE3"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 1
    cursor = db.query_one(
        "SELECT observation_status FROM discussion_assessment_cursors WHERE group_id=? AND session_id=? AND discussion_id=?",
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert cursor["observation_status"] == "inactive"


def test_session_end_finalizes_observation_without_new_agent_message(batch8_env):
    db, scope = batch8_env
    pipeline_id, _result, _message = _publish_observed_intervention(db, scope)
    before_agents = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"]

    from services.session_lifecycle import close_session

    close_session(scope["session_id"], reason="manual")

    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["observation_status"] == "finalization_only"
    assert row["observation_result"] == "finalization_only"
    assert row["observation_completed_at"]
    details = json.loads(row["observation_details_json"])
    assert details["finalization_reason"] == "manual"
    cursor = db.query_one(
        "SELECT observation_status FROM discussion_assessment_cursors WHERE group_id=? AND session_id=? AND discussion_id=?",
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert cursor["observation_status"] == "inactive"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == before_agents
