# -*- coding: utf-8 -*-
"""Focused verification for P0 batch 1 latency observation."""

from __future__ import annotations

import importlib
import json
import logging
import uuid

from tests.helpers import seed_running_session


def _seed_pipeline_and_batch(db):
    scope = seed_running_session(db, session_no=9801, member_count=1)
    timestamp = db.now_str()
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, created_at, updated_at
        ) VALUES(?,?,'running',?,?)
        """,
        (scope["session_id"], scope["group_id"], timestamp, timestamp),
    )
    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, session_no, task_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            trigger_type, window_key, status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,'message_count_periodic',?,'pending',?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            4,
            8,
            f"latency-test:{uuid.uuid4()}",
            timestamp,
            timestamp,
        ),
    )
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, assessment_batch_id,
            input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
            stage1_status, stage2_status, publish_status, final_status,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            discussion_id,
            scope["task_id"],
            "message_count_periodic",
            batch_id,
            4,
            8,
            8,
            "SUCCEEDED",
            "PENDING",
            "NOT_READY",
            "PENDING_STAGE2",
            f"latency-pipeline:{uuid.uuid4()}",
            timestamp,
            timestamp,
        ),
    )
    return scope, discussion_id, batch_id, pipeline_id


def test_latency_schema_and_structured_event_are_safe(test_env, caplog):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    db.init_db()
    db.init_db()
    columns = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(strategy_pipeline_latency_events)")
    }
    assert {
        "pipeline_run_id",
        "assessment_batch_id",
        "lock_token_hash",
        "call_id",
        "attempt",
        "stage",
        "event",
        "occurred_at",
        "elapsed_ms",
    } <= columns
    pipeline_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(strategy_pipeline_runs)")
    }
    assert "assessment_batch_id" in pipeline_columns

    scope, discussion_id, batch_id, pipeline_id = _seed_pipeline_and_batch(db)
    telemetry = importlib.import_module("services.three_stage_latency")
    raw_token = "never-log-this-room-token"
    with caplog.at_level(logging.INFO, logger="services.three_stage_latency"):
        recorded = telemetry.record_latency_event(
            stage="stage2",
            event="stage2_llm_finished",
            pipeline_run_id=pipeline_id,
            assessment_batch_id=batch_id,
            lock_token=raw_token,
            call_id="call-1",
            attempt=1,
            elapsed=123.456,
            details={"success": True, "private_message": "must-not-persist"},
        )

    assert recorded["recorded"] is True
    row = db.query_one(
        "SELECT * FROM strategy_pipeline_latency_events WHERE pipeline_run_id=?",
        (pipeline_id,),
    )
    assert row["group_id"] == scope["group_id"]
    assert row["session_id"] == scope["session_id"]
    assert row["discussion_id"] == discussion_id
    assert row["task_id"] == scope["task_id"]
    assert row["assessment_batch_id"] == batch_id
    assert row["cutoff_sequence"] == 8
    assert row["lock_owner"] == -pipeline_id
    assert row["lock_token_hash"] == telemetry.lock_token_hash(raw_token)
    assert raw_token not in row["lock_token_hash"]
    assert row["elapsed_ms"] == 123.456
    assert json.loads(row["details_json"]) == {"success": True}

    structured = next(
        record.getMessage().split(" ", 1)[1]
        for record in caplog.records
        if record.getMessage().startswith("[three_stage_latency]")
    )
    payload = json.loads(structured)
    assert set(telemetry.REQUIRED_LOG_FIELDS) <= set(payload)
    assert payload["pipeline_run_id"] == pipeline_id
    assert payload["assessment_batch_id"] == batch_id
    assert raw_token not in structured
    assert "private_message" not in structured


def test_latency_duration_uses_high_resolution_timestamps(test_env):
    telemetry = importlib.import_module("services.three_stage_latency")
    assert telemetry.duration_ms(
        "2026-07-29 10:00:00.125", "2026-07-29 10:00:01.375"
    ) == 1250.0


def test_room_lease_events_distinguish_ttl_from_cooldown(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    _scope, _discussion_id, _batch_id, pipeline_id = _seed_pipeline_and_batch(db)
    db.execute(
        """
        UPDATE experiment_sessions
           SET strategy_agent_enabled=1, agent_intervention_enabled=1
         WHERE id=?
        """,
        (_scope["session_id"],),
    )
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (_scope["task_id"],),
    )
    db.execute(
        "UPDATE state_assessment_batches SET status='running' WHERE id=?",
        (_batch_id,),
    )
    db.execute(
        "UPDATE strategy_pipeline_runs SET coarse_should_escalate=1 WHERE id=?",
        (pipeline_id,),
    )
    lease_module = importlib.import_module(
        "services.intervention_pipeline_v2.room_lease_service"
    )
    claimed = lease_module.RoomLeaseService.claim_strategy_pipeline(
        pipeline_id, lock_seconds=75
    )
    assert claimed["acquired"] is True

    acquired = db.query_one(
        """
        SELECT * FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=? AND event='room_lock_acquired'
        ORDER BY id DESC LIMIT 1
        """,
        (pipeline_id,),
    )
    details = json.loads(acquired["details_json"])
    assert details["lock_ttl_seconds"] == 75
    assert details["strategy_cooldown_seconds"] == 120
    assert acquired["lock_token_hash"]
    assert claimed["lock_token"] not in acquired["lock_token_hash"]

    assert lease_module.RoomLeaseService.release(
        _scope["group_id"], claimed["lock_token"]
    ) is True
    released = db.query_one(
        """
        SELECT * FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=? AND event='room_lock_released'
        ORDER BY id DESC LIMIT 1
        """,
        (pipeline_id,),
    )
    assert released is not None
    assert released["lock_token_hash"] == acquired["lock_token_hash"]
