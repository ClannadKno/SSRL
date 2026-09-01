# -*- coding: utf-8 -*-
"""Regression coverage for ordered replay of terminal Stage 2 failures."""

from __future__ import annotations

import importlib
import json
import uuid

from tests.helpers import seed_running_session


def _seed_failed_scope(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    service_module = importlib.import_module(
        "services.state_assessment_batch_service"
    )
    scope = seed_running_session(db, session_no=970, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='none', strategy_agent_enabled=0,
               emotion_agent_enabled=0,
               research_state_monitoring_enabled=1
         WHERE id=?
        """,
        (scope["session_id"],),
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
    student_id = scope["students"][0][0]
    for sequence in range(1, 5):
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_id, session_no, task_id, discussion_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                student_id,
                f"student message {sequence}",
                sequence,
                "student",
                "student",
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                discussion_id,
                db.now_str(),
            ),
        )
    batch_ids = []
    for index, (start, end, error_code) in enumerate(
        ((1, 2, "schema_validation_error"), (3, 4, "read_timeout")),
        start=1,
    ):
        batch_id = db.execute(
            """
            INSERT INTO state_assessment_batches(
                group_id, session_id, session_no, task_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence,
                trigger_type, window_key, status,
                attempt_count, max_attempts, student_sequences_json,
                terminal_status, terminal_at, fallback_action,
                error_code, error_detail, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'failed',2,2,?,'quarantined',?,
                     'unclassified',?,?,?,?)
            """,
            (
                scope["group_id"],
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                discussion_id,
                start,
                end,
                "message_count_periodic",
                f"replay-window-{scope['session_id']}-{index}",
                json.dumps(list(range(start, end + 1))),
                db.now_str(),
                error_code,
                "old failure",
                db.now_str(),
                db.now_str(),
            ),
        )
        batch_ids.append(batch_id)
        db.execute(
            """
            INSERT INTO strategy_pipeline_runs(
                run_uuid, group_id, session_id, discussion_id,
                input_start_sequence, input_end_sequence,
                input_cutoff_student_sequence,
                stage1_status, stage2_status, stage3_status,
                publish_status, final_status, skip_reason,
                failure_code, failure_detail, idempotency_key,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,'SUCCEEDED','FAILED','SKIPPED',
                     'NOT_READY','FAILED','STAGE2_FAILED',?,?,?, ?,?)
            """,
            (
                str(uuid.uuid4()),
                scope["group_id"],
                scope["session_id"],
                discussion_id,
                start,
                end,
                end,
                error_code,
                "old failure",
                f"replay-pipeline-{scope['session_id']}-{index}",
                db.now_str(),
                db.now_str(),
            ),
        )
    db.execute(
        """
        INSERT INTO discussion_assessment_cursors(
            group_id, session_id, session_no, task_id, discussion_id,
            last_finalized_student_sequence,
            last_scheduled_student_sequence, observation_status, updated_at
        ) VALUES(?,?,?,?,?,0,4,'inactive',?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            db.now_str(),
        ),
    )
    return db, scheduler, service_module.StateAssessmentBatchService, scope, batch_ids


def test_terminal_failures_are_prepared_then_reopened_one_at_a_time(
    test_env,
    monkeypatch,
):
    db, scheduler, service, scope, batch_ids = _seed_failed_scope(test_env)
    queued = []
    monkeypatch.setattr(scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 1)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 2)
    monkeypatch.setattr(
        scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: queued.append(int(batch_id)),
    )

    preview = service.prepare_scope_reprocessing(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert preview["reason"] == "dry_run"
    assert preview["batch_ids"] == batch_ids
    assert db.query_one(
        "SELECT last_scheduled_student_sequence FROM discussion_assessment_cursors"
    )["last_scheduled_student_sequence"] == 4

    prepared = service.prepare_scope_reprocessing(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        apply=True,
    )
    assert prepared["prepared"] is True
    assert db.query_one(
        "SELECT last_scheduled_student_sequence FROM discussion_assessment_cursors"
    )["last_scheduled_student_sequence"] == 0
    rows = db.query_all(
        """
        SELECT id, status, attempt_count, terminal_status, error_code,
               fallback_action
        FROM state_assessment_batches
        ORDER BY id
        """
    )
    assert [row["status"] for row in rows] == ["failed", "failed"]
    assert [row["attempt_count"] for row in rows] == [0, 0]
    assert all(row["terminal_status"] is None for row in rows)
    assert all(row["error_code"] is None for row in rows)
    assert all(row["fallback_action"] == "state_only_replay" for row in rows)

    first = scheduler.request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type="message_count_periodic",
        continuation=True,
    )
    assert first["assessment_batch_id"] == batch_ids[0]
    assert first["enqueued"] is True
    assert queued == [batch_ids[0]]
    rows = db.query_all(
        "SELECT id, status FROM state_assessment_batches ORDER BY id"
    )
    assert [(row["id"], row["status"]) for row in rows] == [
        (batch_ids[0], "pending"),
        (batch_ids[1], "failed"),
    ]


def test_state_only_replay_cannot_enter_stage3(test_env):
    db, _scheduler, service, scope, batch_ids = _seed_failed_scope(test_env)
    service.prepare_scope_reprocessing(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        apply=True,
    )
    batch = dict(
        db.query_one(
            "SELECT * FROM state_assessment_batches WHERE id=?",
            (batch_ids[-1],),
        )
    )
    stage2 = importlib.import_module("services.three_stage_stage2")
    payload = {
        "schema_version": "stage2.v1",
        "segments": [
            {
                "raw_sub_state": "interpersonal_conflict",
                "canonical_sub_state": "interpersonal_conflict",
                "secondary_tags": [],
                "start_sequence": 3,
                "end_sequence": 4,
                "confidence": 0.9,
                "evidence_message_ids": [3, 4],
                "reason": "historical evidence",
                "is_active_at_window_end": True,
                "detected_self_regulation": False,
            }
        ],
        "active_sub_state": {
            "raw_sub_state": "interpersonal_conflict",
            "canonical_sub_state": "interpersonal_conflict",
            "secondary_tags": [],
            "start_sequence": 3,
            "end_sequence": 4,
            "confidence": 0.9,
            "evidence_message_ids": [3, 4],
            "detected_self_regulation": False,
        },
        "should_intervene": True,
        "inhibition": {
            "is_inhibited": False,
            "strategy_id": None,
            "reason": None,
        },
        "candidate_strategy_ids": ["ER-001", "EE-001", "SS-004", "ER-007"],
        "decision_reason": "historical replay",
    }

    result = stage2.Stage2PipelineService.persist_success(
        batch=batch,
        stage2_result=payload,
        suppress_intervention=True,
    )
    pipeline = db.query_one(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?",
        (result["pipeline_run_id"],),
    )

    assert result["should_intervene"] is True
    assert result["should_enter_stage3"] is False
    assert result["final_status"] == "SUPPRESSED"
    assert result["skip_reason"] == "STATE_ONLY_REPLAY"
    assert pipeline["stage3_status"] == "SKIPPED"
    assert pipeline["publish_status"] == "SUPPRESSED"
