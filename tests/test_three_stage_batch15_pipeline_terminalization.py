# -*- coding: utf-8 -*-
"""Batch 15 coverage for terminal assessment-batch pipeline ownership."""

from __future__ import annotations

import importlib

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def batch15_env(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=1515, member_count=1)
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
    return db, scope


def _insert_batch(
    db,
    scope,
    *,
    key,
    start,
    end,
    status,
    terminal_status=None,
    error_code=None,
    error_detail=None,
):
    return db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            context_start_sequence, context_end_sequence,
            trigger_type, trigger_sequence, window_key,
            status, request_priority, attempt_count, max_attempts,
            error_code, error_detail, terminal_status, terminal_at,
            completed_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            start,
            end,
            start,
            end,
            "message_count_periodic",
            end,
            f"batch15:{key}:{start}-{end}",
            status,
            200,
            2 if terminal_status else 0,
            2,
            error_code,
            error_detail,
            terminal_status,
            db.now_str() if terminal_status else None,
            db.now_str() if status in {"succeeded", "failed"} else None,
            db.now_str(),
            db.now_str(),
        ),
    )


def _insert_pipeline(
    db,
    scope,
    *,
    key,
    cutoff,
    assessment_batch_id=None,
    stage2_status="PENDING",
    publish_status="NOT_READY",
    final_status="PENDING_STAGE2",
    failure_code=None,
    failure_detail=None,
):
    canonical = "execution_progress" if stage2_status == "SUCCEEDED" else None
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority, assessment_batch_id,
            input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence,
            stage1_status, stage2_status, stage2_completed_at,
            canonical_sub_state_code, should_intervene,
            publish_status, final_status, failure_code, failure_detail,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"batch15-{key}",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "message_count_periodic",
            200,
            assessment_batch_id,
            cutoff,
            cutoff,
            cutoff,
            "SUCCEEDED",
            stage2_status,
            db.now_str() if stage2_status in {"SUCCEEDED", "FAILED"} else None,
            canonical,
            0 if canonical else None,
            publish_status,
            final_status,
            failure_code,
            failure_detail,
            f"batch15:{key}",
            db.now_str(),
            db.now_str(),
        ),
    )


def test_terminal_batch_recovery_closes_g04_and_g06_orphans_idempotently(
    batch15_env,
):
    db, scope = batch15_env
    stage2 = importlib.import_module("services.three_stage_stage2")
    api = importlib.import_module("routes.api")

    success_batch_id = _insert_batch(
        db,
        scope,
        key="g04",
        start=1,
        end=6,
        status="succeeded",
    )
    success_owner_id = _insert_pipeline(
        db,
        scope,
        key="g04-owner",
        cutoff=6,
        assessment_batch_id=success_batch_id,
        stage2_status="SUCCEEDED",
        publish_status="SKIPPED",
        final_status="SUPPRESSED",
    )
    g04_orphans = [
        _insert_pipeline(db, scope, key=f"g04-orphan-{sequence}", cutoff=sequence)
        for sequence in range(1, 7)
    ]

    failure_batch_id = _insert_batch(
        db,
        scope,
        key="g06",
        start=7,
        end=7,
        status="failed",
        terminal_status="quarantined",
        error_code="reasoning_budget_exhausted",
        error_detail="structured final content was empty",
    )
    failure_owner_id = _insert_pipeline(
        db,
        scope,
        key="g06-owner",
        cutoff=7,
        assessment_batch_id=failure_batch_id,
        stage2_status="FAILED",
        final_status="FAILED",
        failure_code="reasoning_budget_exhausted",
        failure_detail="structured final content was empty",
    )
    g06_orphan_id = _insert_pipeline(
        db,
        scope,
        key="g06-orphan-7",
        cutoff=7,
    )

    active_batch_id = _insert_batch(
        db,
        scope,
        key="active",
        start=8,
        end=8,
        status="running",
    )
    active_pipeline_id = _insert_pipeline(
        db,
        scope,
        key="active-owner",
        cutoff=8,
        assessment_batch_id=active_batch_id,
    )

    first = stage2.Stage2PipelineService.recover_terminal_batch_orphans(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    first_by_batch = {item["assessment_batch_id"]: item for item in first}

    assert first_by_batch[success_batch_id]["pipeline_run_ids"] == g04_orphans
    assert first_by_batch[failure_batch_id]["pipeline_run_ids"] == [g06_orphan_id]
    pipeline_columns = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(strategy_pipeline_runs)")
    }
    pipeline_indexes = {
        row["name"]
        for row in db.query_all("PRAGMA index_list(strategy_pipeline_runs)")
    }
    assert "assessment_owner_pipeline_run_id" in pipeline_columns
    assert "idx_strategy_pipeline_runs_assessment_owner" in pipeline_indexes

    success_rows = db.query_all(
        """
        SELECT id, assessment_batch_id, assessment_owner_pipeline_run_id,
               stage2_status, publish_status,
               final_status, skip_reason, superseded_by_run_id
        FROM strategy_pipeline_runs
        WHERE id IN (?,?,?,?,?,?)
        ORDER BY id
        """,
        tuple(g04_orphans),
    )
    assert [row["id"] for row in success_rows] == g04_orphans
    assert {
        (
            row["assessment_batch_id"],
            row["assessment_owner_pipeline_run_id"],
            row["stage2_status"],
            row["publish_status"],
            row["final_status"],
            row["skip_reason"],
            row["superseded_by_run_id"],
        )
        for row in success_rows
    } == {
        (
            success_batch_id,
            success_owner_id,
            "PENDING",
            "SKIPPED",
            "SUPERSEDED",
            "SUPERSEDED_BY_STATE_BATCH",
            success_owner_id,
        )
    }

    failure_row = db.query_one(
        """
        SELECT assessment_batch_id, assessment_owner_pipeline_run_id,
               stage2_status, publish_status,
               final_status, skip_reason, failure_code, failure_detail,
               superseded_by_run_id, updated_at
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (g06_orphan_id,),
    )
    assert dict(failure_row) == {
        "assessment_batch_id": failure_batch_id,
        "assessment_owner_pipeline_run_id": failure_owner_id,
        "stage2_status": "SKIPPED",
        "publish_status": "SKIPPED",
        "final_status": "FAILED",
        "skip_reason": "COVERED_ASSESSMENT_BATCH_FAILED",
        "failure_code": "reasoning_budget_exhausted",
        "failure_detail": "structured final content was empty",
        "superseded_by_run_id": None,
        "updated_at": failure_row["updated_at"],
    }
    assert api._student_pipeline_terminal(dict(failure_row)) == (
        "FAILED",
        "FAILED",
    )

    owner = db.query_one(
        """
        SELECT assessment_owner_pipeline_run_id, stage2_status, final_status,
               failure_code, failure_detail
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (failure_owner_id,),
    )
    assert dict(owner) == {
        "assessment_owner_pipeline_run_id": failure_owner_id,
        "stage2_status": "FAILED",
        "final_status": "FAILED",
        "failure_code": "reasoning_budget_exhausted",
        "failure_detail": "structured final content was empty",
    }
    active = db.query_one(
        """
        SELECT assessment_batch_id, stage2_status, final_status, skip_reason
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (active_pipeline_id,),
    )
    assert dict(active) == {
        "assessment_batch_id": active_batch_id,
        "stage2_status": "PENDING",
        "final_status": "PENDING_STAGE2",
        "skip_reason": None,
    }

    first_updated_at = failure_row["updated_at"]
    second = stage2.Stage2PipelineService.recover_terminal_batch_orphans(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert all(item["pipeline_run_ids"] == [] for item in second)
    assert db.query_one(
        "SELECT updated_at FROM strategy_pipeline_runs WHERE id=?",
        (g06_orphan_id,),
    )["updated_at"] == first_updated_at


def test_terminal_batch_without_authoritative_owner_is_not_guessed(batch15_env):
    db, scope = batch15_env
    stage2 = importlib.import_module("services.three_stage_stage2")

    batch_id = _insert_batch(
        db,
        scope,
        key="no-owner",
        start=10,
        end=10,
        status="failed",
        terminal_status="quarantined",
        error_code="schema_validation_error",
    )
    orphan_id = _insert_pipeline(
        db,
        scope,
        key="no-owner-orphan",
        cutoff=10,
    )

    result = stage2.Stage2PipelineService.finalize_terminal_batch_siblings(
        batch_id
    )

    assert result["finalized"] is False
    assert result["reason"] == "authoritative_pipeline_not_terminal"
    row = db.query_one(
        """
        SELECT assessment_batch_id, stage2_status, final_status
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (orphan_id,),
    )
    assert dict(row) == {
        "assessment_batch_id": None,
        "stage2_status": "PENDING",
        "final_status": "PENDING_STAGE2",
    }
