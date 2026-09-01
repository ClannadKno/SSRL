# -*- coding: utf-8 -*-
"""Batch 10 coverage for stale, superseded, and replacement pipeline links."""

from __future__ import annotations

import importlib

import pytest

from tests.helpers import seed_running_session
from tests.test_three_stage_batch6_decision_gate import _add_message, _ready_pipeline


@pytest.fixture
def batch10_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=1010, member_count=1)
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


def _link(db, original_id, replacement_id, **kwargs):
    coordination = importlib.import_module("services.three_stage_coordination")
    conn = db.db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = coordination.link_pipeline_replacement(
            conn, original_id, replacement_id, **kwargs
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _seed_messages(db, scope):
    for sequence, content in (
        (1, "我们先看证据。"),
        (2, "先说清楚哪里不行。"),
        (3, "我担心这个条件没有被比较。"),
    ):
        _add_message(db, scope, sequence, content)


def _insert_minimal_pipeline(
    db,
    scope,
    *,
    key,
    stage2_status="PENDING",
    canonical=None,
    should_intervene=None,
    stage3_status="PENDING",
    publish_status="NOT_READY",
    final_status="PENDING_STAGE2",
):
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority,
            input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence,
            stage1_status, stage2_status, canonical_sub_state_code,
            sub_state_evidence_message_ids_json, should_intervene,
            stage3_status, strategy_candidate_ids_json,
            publish_status, final_status, idempotency_key,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"batch10-{key}-{scope['group_id']}",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "message_count_periodic",
            200,
            1,
            3,
            3,
            "NOT_RUN",
            stage2_status,
            canonical,
            "[1,2,3]" if should_intervene == 1 else "[]",
            should_intervene,
            stage3_status,
            "[]",
            publish_status,
            final_status,
            f"batch10-idempotency-{key}-{scope['group_id']}",
            db.now_str(),
            db.now_str(),
        ),
    )


def test_superseded_without_stage2_output_is_unavailable_not_false(batch10_env):
    db, scope = batch10_env
    coordination = importlib.import_module("services.three_stage_coordination")

    _seed_messages(db, scope)
    original_id = _insert_minimal_pipeline(
        db, scope, key="original", should_intervene=1
    )
    replacement_id = _insert_minimal_pipeline(
        db, scope, key="replacement", should_intervene=None
    )

    superseded = coordination.supersede_preliminary_runs_for_batch(
        replacement_id, start_sequence=1, end_sequence=3
    )

    assert superseded == [original_id]
    row = db.query_one(
        """
        SELECT final_status, publish_status, should_intervene,
               superseded_by_run_id, replaced_by_pipeline_run_id,
               replacement_reason, replacement_trigger_message_id,
               replacement_cutoff_sequence, latest_state,
               latest_should_intervene, latest_state_pipeline_run_id
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (original_id,),
    )
    assert dict(row) == {
        "final_status": "SUPERSEDED",
        "publish_status": "SKIPPED",
        "should_intervene": None,
        "superseded_by_run_id": replacement_id,
        "replaced_by_pipeline_run_id": replacement_id,
        "replacement_reason": "SUPERSEDED_BY_STATE_BATCH",
        "replacement_trigger_message_id": row["replacement_trigger_message_id"],
        "replacement_cutoff_sequence": 3,
        "latest_state": None,
        "latest_should_intervene": None,
        "latest_state_pipeline_run_id": replacement_id,
    }
    assert row["replacement_trigger_message_id"] is not None


def test_stale_records_continuation_and_preparing_replacement_fills_link(
    batch10_env, monkeypatch
):
    db, scope = batch10_env
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    publish = importlib.import_module("services.three_stage_publish")
    stage2 = importlib.import_module("services.three_stage_stage2")
    monkeypatch.setattr(scheduler, "_enqueue_batch", lambda *args, **kwargs: None)

    original_id = _ready_pipeline(db, scope)
    trigger_message_id = _add_message(
        db, scope, 4, "新的学生证据使旧窗口失效。"
    )

    result = publish.ThreeStageInterventionPublisher.publish_ready_pipeline(original_id)

    assert result["published"] is False
    assert result["failure_category"] == "stale_new_student_message"
    request = result["replacement_assessment"]
    assert request["created"] is True
    batch = db.query_one(
        "SELECT * FROM state_assessment_batches WHERE id=?",
        (request["assessment_batch_id"],),
    )
    assert batch["replacement_of_pipeline_run_id"] == original_id
    assert batch["replacement_reason"] == "STALE_NEW_STUDENT_MESSAGE"
    assert batch["replacement_trigger_message_id"] == trigger_message_id
    assert batch["replacement_cutoff_sequence"] == 4

    prepared = stage2.Stage2PipelineService.prepare_for_batch(dict(batch))
    replacement_id = prepared["pipeline_run_id"]
    original = db.query_one(
        """
        SELECT final_status, replaced_by_pipeline_run_id,
               replacement_reason, replacement_trigger_message_id,
               replacement_cutoff_sequence, latest_state,
               latest_should_intervene
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (original_id,),
    )
    replacement = db.query_one(
        """
        SELECT parent_run_id, trigger_message_id, stage2_status,
               final_status, should_intervene
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (replacement_id,),
    )
    assert original["final_status"] == "STALE"
    assert original["replaced_by_pipeline_run_id"] == replacement_id
    assert original["replacement_reason"] == "STALE_NEW_STUDENT_MESSAGE"
    assert original["replacement_trigger_message_id"] == trigger_message_id
    assert original["replacement_cutoff_sequence"] == 4
    assert original["latest_state"] is None
    assert original["latest_should_intervene"] is None
    assert replacement["parent_run_id"] == original_id
    assert replacement["trigger_message_id"] == trigger_message_id
    assert replacement["stage2_status"] == "PENDING"
    assert replacement["final_status"] == "PENDING_STAGE2"
    assert replacement["should_intervene"] is None


def test_replacement_latest_state_is_distinct_from_stale_trigger_state(batch10_env):
    db, scope = batch10_env

    _seed_messages(db, scope)
    original_id = _insert_minimal_pipeline(
        db,
        scope,
        key="original",
        stage2_status="SUCCEEDED",
        canonical="interpersonal_conflict",
        should_intervene=1,
        stage3_status="PENDING",
        final_status="PENDING_STAGE3",
    )
    replacement_id = _insert_minimal_pipeline(
        db,
        scope,
        key="replacement",
        stage2_status="SUCCEEDED",
        canonical="execution_progress",
        should_intervene=0,
        stage3_status="SKIPPED",
        publish_status="SKIPPED",
        final_status="SUPPRESSED",
    )
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET final_status='STALE', publish_status='SKIPPED',
               trigger_level_state='interpersonal_conflict',
               latest_state=NULL, latest_should_intervene=NULL
         WHERE id=?
        """,
        (original_id,),
    )
    trigger_message_id = db.query_one(
        """
        SELECT id FROM messages
        WHERE group_id=? AND session_id=? AND discussion_id=? AND sequence=3
        ORDER BY id DESC LIMIT 1
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )["id"]

    linked = _link(
        db,
        original_id,
        replacement_id,
        reason="STALE_NEW_STUDENT_MESSAGE",
        trigger_message_id=trigger_message_id,
        cutoff_sequence=3,
    )

    assert linked["linked"] is True
    row = db.query_one(
        """
        SELECT trigger_level_state, latest_state, latest_should_intervene,
               latest_state_pipeline_run_id, replaced_by_pipeline_run_id,
               replacement_reason, replacement_trigger_message_id,
               replacement_cutoff_sequence
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (original_id,),
    )
    assert dict(row) == {
        "trigger_level_state": "interpersonal_conflict",
        "latest_state": "execution_progress",
        "latest_should_intervene": 0,
        "latest_state_pipeline_run_id": replacement_id,
        "replaced_by_pipeline_run_id": replacement_id,
        "replacement_reason": "STALE_NEW_STUDENT_MESSAGE",
        "replacement_trigger_message_id": trigger_message_id,
        "replacement_cutoff_sequence": 3,
    }
