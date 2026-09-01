# -*- coding: utf-8 -*-
"""P0 batch 2 coverage for the preliminary/authoritative lease boundary."""

from __future__ import annotations

import uuid

import pytest

from tests.helpers import seed_running_session


def _scope(db, session_no: int) -> dict:
    scope = seed_running_session(db, session_no=session_no, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
           SET status='running', strategy_agent_enabled=1,
               agent_intervention_enabled=1
         WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (scope["task_id"],),
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
    return scope


def _authoritative_pipeline(
    db,
    scope: dict,
    *,
    start_sequence: int = 1,
    cutoff_sequence: int = 2,
    batch_status: str = "running",
    stage2_status: str = "PENDING",
    final_status: str = "PENDING_STAGE2",
    should_intervene=None,
    batch_id: int = None,
) -> tuple[int, int]:
    if batch_id is None:
        batch_id = db.execute(
            """
            INSERT INTO state_assessment_batches(
                group_id, session_id, session_no, task_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence,
                trigger_type, window_key, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                scope["discussion_id"],
                start_sequence,
                cutoff_sequence,
                "rule_high_risk",
                f"p0-lock-{uuid.uuid4()}",
                batch_status,
                db.now_str(),
                db.now_str(),
            ),
        )
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority, assessment_batch_id,
            input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence,
            stage1_status, coarse_decision, coarse_state_code,
            coarse_should_escalate, stage2_status,
            canonical_sub_state_code,
            sub_state_evidence_message_ids_json,
            should_intervene,
            stage3_status, publish_status, final_status,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "rule_high_risk",
            2,
            batch_id,
            start_sequence,
            cutoff_sequence,
            cutoff_sequence,
            "SUCCEEDED",
            "ESCALATE",
            "POSSIBLE_BLOCKED",
            1,
            stage2_status,
            "confusion" if stage2_status == "SUCCEEDED" else None,
            "[1]" if should_intervene else "[]",
            should_intervene,
            "PENDING" if stage2_status == "SUCCEEDED" else None,
            "NOT_READY",
            final_status,
            db.now_str(),
            db.now_str(),
        ),
    )
    return batch_id, pipeline_id


def _batch(db, batch_id: int) -> dict:
    return dict(
        db.query_one("SELECT * FROM state_assessment_batches WHERE id=?", (batch_id,))
    )


def _stage2_terminal_payload(*, oi_strategy_id: str = None) -> dict:
    return {
        "schema_version": "stage2.v1",
        "active_sub_state": {
            "raw_sub_state": "standard",
            "canonical_sub_state": "standard",
            "secondary_tags": [],
            "confidence": 0.91,
            "start_sequence": 1,
            "end_sequence": 2,
            "evidence_message_ids": [],
            "detected_self_regulation": False,
        },
        "segments": [],
        "should_intervene": False,
        "inhibition": {
            "is_inhibited": bool(oi_strategy_id),
            "strategy_id": oi_strategy_id,
            "reason": "validated OI" if oi_strategy_id else None,
        },
        "candidate_strategy_ids": [],
        "decision_reason": "no intervention required",
    }


def test_authoritative_lease_precedes_stage2_start(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    scope = _scope(db, 9301)
    batch_id, pipeline_id = _authoritative_pipeline(db, scope)

    import config
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
    from services.three_stage_stage2 import Stage2PipelineService

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    claim = RoomLeaseService.claim_strategy_pipeline(pipeline_id)
    assert claim["acquired"] is True
    before_start = db.query_one(
        "SELECT room_lock_acquired_at, stage2_started_at FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert before_start["room_lock_acquired_at"]
    assert before_start["stage2_started_at"] is None

    started = Stage2PipelineService.mark_started(
        batch=_batch(db, batch_id), pipeline_run_id=pipeline_id
    )
    assert started["started"] is True
    after_start = db.query_one(
        "SELECT room_lock_acquired_at, stage2_started_at FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert after_start["room_lock_acquired_at"] <= after_start["stage2_started_at"]
    failed = Stage2PipelineService.mark_failed(
        batch=_batch(db, batch_id),
        error_code="test_cleanup",
    )
    assert failed["lock_released"] is True


@pytest.mark.parametrize("oi_strategy_id", [None, "OI-001"])
def test_stage2_pass_or_oi_releases_immediately(
    db_and_app, monkeypatch, oi_strategy_id
):
    db, _app, _client = db_and_app
    scope = _scope(db, 9302 if oi_strategy_id is None else 9303)
    batch_id, pipeline_id = _authoritative_pipeline(db, scope)

    import config
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
    from services.three_stage_stage2 import Stage2PipelineService

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    assert RoomLeaseService.claim_strategy_pipeline(pipeline_id)["acquired"] is True
    assert Stage2PipelineService.mark_started(
        batch=_batch(db, batch_id), pipeline_run_id=pipeline_id
    )["started"] is True
    result = Stage2PipelineService.persist_success(
        batch=_batch(db, batch_id),
        stage2_result=_stage2_terminal_payload(oi_strategy_id=oi_strategy_id),
    )

    assert result["should_enter_stage3"] is False
    assert result["lock_released"] is True
    room = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(room) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }


def test_stage3_failure_releases_authoritative_lease(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    scope = _scope(db, 9304)
    batch_id, pipeline_id = _authoritative_pipeline(
        db,
        scope,
        batch_status="succeeded",
        stage2_status="SUCCEEDED",
        final_status="PENDING_STAGE3",
        should_intervene=1,
    )

    import config
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
    from services.three_stage_stage3 import Stage3PipelineService

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    assert RoomLeaseService.claim_strategy_pipeline(pipeline_id)["acquired"] is True
    failed = Stage3PipelineService.mark_failed(
        pipeline_id,
        "schema_validation_failure",
        "invalid generated schema",
    )
    assert failed["lock_released"] is True
    assert db.query_one(
        "SELECT state FROM groups WHERE id=?", (scope["group_id"],)
    )["state"] == "OPEN"


def test_same_group_authoritative_claim_is_single_owner(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    scope = _scope(db, 9305)
    batch_id, first_id = _authoritative_pipeline(db, scope)
    _, second_id = _authoritative_pipeline(db, scope, batch_id=batch_id)

    import config
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    first = RoomLeaseService.claim_strategy_pipeline(first_id)
    second = RoomLeaseService.claim_strategy_pipeline(second_id)
    assert first["acquired"] is True
    assert second["acquired"] is False
    assert second["reason"] == "room_locked_by_other_pipeline"
    assert RoomLeaseService.release(scope["group_id"], first["lock_token"]) is True


def test_different_groups_claim_independent_authoritative_leases(
    db_and_app, monkeypatch
):
    db, _app, _client = db_and_app
    first_scope = _scope(db, 9306)
    second_scope = _scope(db, 9307)
    db.execute(
        "UPDATE experiment_sessions SET status='running' WHERE id=?",
        (first_scope["session_id"],),
    )
    _, first_id = _authoritative_pipeline(db, first_scope)
    _, second_id = _authoritative_pipeline(db, second_scope)

    import config
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    first = RoomLeaseService.claim_strategy_pipeline(first_id)
    second = RoomLeaseService.claim_strategy_pipeline(second_id)
    assert first["acquired"] is True
    assert second["acquired"] is True
    assert first["lock_token"] != second["lock_token"]
    assert RoomLeaseService.release(first_scope["group_id"], first["lock_token"])
    assert RoomLeaseService.release(second_scope["group_id"], second["lock_token"])


def test_closed_discussion_blocks_authoritative_claim(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    scope = _scope(db, 9308)
    _, pipeline_id = _authoritative_pipeline(db, scope)
    db.execute(
        "UPDATE group_session_discussions SET status='closed' WHERE id=?",
        (scope["discussion_id"],),
    )

    import config
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    claim = RoomLeaseService.claim_strategy_pipeline(pipeline_id)
    assert claim == {
        "acquired": False,
        "reason": "discussion_not_running",
        "pipeline_run_id": pipeline_id,
        "failure_category": "invalid_room_lease",
    }
    assert db.query_one(
        "SELECT state FROM groups WHERE id=?", (scope["group_id"],)
    )["state"] == "OPEN"
