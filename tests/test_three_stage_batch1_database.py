# -*- coding: utf-8 -*-
"""Batch 1 database schema tests for the three-stage strategy pipeline."""

from __future__ import annotations

import importlib
import sqlite3
import uuid

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def batch1_db(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    return db


def _column_names(db, table):
    return {row["name"] for row in db.query_all(f"PRAGMA table_info({table})")}


def _index_names(db, table):
    rows = db.query_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (table,),
    )
    return {row["name"] for row in rows}


def _make_scope(db):
    scope = seed_running_session(db, session_no=9101, member_count=1)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, created_at, updated_at
        ) VALUES(?,?,'running',?,?)
        """,
        (scope["session_id"], scope["group_id"], db.now_str(), db.now_str()),
    )
    scope["discussion_id"] = discussion_id
    return scope


def _insert_strategy_definition(db, strategy_id="ER-001", should_intervene=1, is_exclusive=0):
    return db.execute(
        """
        INSERT INTO strategy_definitions(
            strategy_id, strategy_name, strategy_type,
            applicable_sub_states_json, version, content_hash,
            should_intervene, is_exclusive, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            strategy_id,
            f"{strategy_id} name",
            "emotion_regulation",
            '["interpersonal_conflict"]',
            "v1",
            f"hash-{strategy_id}",
            should_intervene,
            is_exclusive,
            db.now_str(),
            db.now_str(),
        ),
    )


def _insert_complete_pipeline_run(db, scope, selected_strategy_id="ER-001"):
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid,
            group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_message_id, trigger_priority,
            input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
            stage1_status, stage1_started_at, stage1_completed_at,
            coarse_decision, coarse_state_code, coarse_risk_group,
            coarse_should_escalate, coarse_confidence,
            coarse_rule_scores_json, coarse_quantitative_features_json,
            coarse_evidence_message_ids_json, coarse_reason_codes_json,
            stage2_status, stage2_started_at, stage2_completed_at,
            raw_sub_state_code, canonical_sub_state_code,
            secondary_sub_state_tags_json, sub_state_confidence, sub_state_reason,
            sub_state_start_sequence, sub_state_end_sequence,
            sub_state_evidence_message_ids_json, all_state_segments_json,
            detected_self_regulation, should_intervene,
            state_model_name, state_model_version, state_prompt_version,
            state_raw_response_json,
            stage3_status, stage3_started_at, stage3_completed_at,
            strategy_candidate_ids_json, selected_strategy_id,
            selected_strategy_name, selected_strategy_type,
            supporting_strategy_ids_json, strategy_selection_reason,
            strategy_library_version, strategy_library_hash,
            strategy_model_name, strategy_model_version, strategy_prompt_version,
            strategy_raw_response_json,
            generated_intervention_text, validated_intervention_text,
            text_validation_result_json,
            room_lock_token, room_lock_acquired_at, room_lock_released_at,
            publish_status, published_message_id, published_at,
            final_status, idempotency_key, created_at, updated_at
        ) VALUES(
            ?,?,?,?,?,?,
            ?,?,?,
            ?,?,?,
            ?,?,?,
            ?,?,?,
            ?,?,
            ?,?,?,?,
            ?,?,?,
            ?,?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,?,
            ?,?,?,
            ?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,?,
            ?,?,
            ?,?,?,
            ?,
            ?,?,
            ?,
            ?,?,?,
            ?,?,?,
            ?,?,?,?
        )
        """,
        (
            str(uuid.uuid4()),
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "student_message",
            101,
            2,
            1,
            3,
            3,
            "SUCCEEDED",
            db.now_str(),
            db.now_str(),
            "ESCALATE",
            "POSSIBLE_CONFLICT",
            "HIGH",
            1,
            0.81,
            '{"conflict":0.81}',
            '{"new_student_message_count":3}',
            "[101,102]",
            '["CONFLICT_TERMS"]',
            "SUCCEEDED",
            db.now_str(),
            db.now_str(),
            "relationship conflict",
            "interpersonal_conflict",
            '["psychological_safety_risk"]',
            0.84,
            "active interpersonal conflict",
            1,
            3,
            "[101,102]",
            '[{"canonical_sub_state":"interpersonal_conflict"}]',
            0,
            1,
            "test-state-model",
            "2026-07",
            "stage2.v1",
            '{"ok":true}',
            "SUCCEEDED",
            db.now_str(),
            db.now_str(),
            '["ER-001","SS-004"]',
            selected_strategy_id,
            "conflict reframing",
            "emotion_regulation",
            '["SS-004"]',
            "selected from candidate list",
            "v1",
            "hash-ER-001",
            "test-strategy-model",
            "2026-07",
            "stage3.v1",
            '{"ok":true}',
            "Please compare concerns first.",
            "Please compare concerns first.",
            '{"passed":true}',
            "lock-token",
            db.now_str(),
            db.now_str(),
            "PUBLISHED",
            999,
            db.now_str(),
            "PUBLISHED",
            f"pipeline:{uuid.uuid4()}",
            db.now_str(),
            db.now_str(),
        ),
    )


def test_batch1_schema_is_created_and_idempotent(batch1_db):
    db = batch1_db
    db.init_db()
    db.init_db()

    pipeline_columns = _column_names(db, "strategy_pipeline_runs")
    strategy_columns = _column_names(db, "strategy_definitions")
    segment_columns = _column_names(db, "collaboration_state_segments")
    intervention_columns = _column_names(db, "intervention_runs")

    assert {
        "run_uuid",
        "coarse_state_code",
        "raw_sub_state_code",
        "canonical_sub_state_code",
        "sub_category",
        "evidence_message_ids_json",
        "strategy_candidate_ids_json",
        "strategy_pool_json",
        "selected_strategy_id",
        "selected_strategy_json",
        "strategy_source",
        "validated_intervention_text",
        "publish_status",
        "idempotency_key",
        "observation_status",
        "observation_started_at",
        "observation_first_response_sequence",
        "observation_first_response_seconds",
        "observation_window_start_sequence",
        "observation_window_end_sequence",
        "observation_completed_at",
        "observation_result",
        "observation_assessment_run_id",
        "observation_assessment_batch_id",
        "observation_reintervention_run_id",
        "observation_previous_sub_state_code",
        "observation_current_sub_state_code",
        "observation_details_json",
    } <= pipeline_columns
    assert {
        "strategy_id",
        "strategy_name",
        "applicable_sub_states_json",
        "should_intervene",
        "is_exclusive",
        "version",
        "content_hash",
    } <= strategy_columns
    assert {
        "coarse_state_code",
        "raw_sub_state_code",
        "canonical_sub_state_code",
        "secondary_tags_json",
        "strategy_pipeline_run_id",
        "should_intervene",
        "selected_strategy_id",
        "strategy_library_version",
        "source_stage",
    } <= segment_columns
    assert {
        "strategy_pipeline_run_id",
        "canonical_sub_state_code",
        "sub_category",
        "strategy_candidate_ids_json",
        "strategy_pool_json",
        "strategy_source",
        "strategy_selection_reason",
        "evidence_message_ids_json",
        "input_cutoff_student_sequence",
        "generated_text",
        "validated_text",
        "publish_status",
    } <= intervention_columns
    assert "idx_strategy_pipeline_runs_idempotency" in _index_names(db, "strategy_pipeline_runs")
    assert "idx_strategy_pipeline_runs_observation" in _index_names(db, "strategy_pipeline_runs")
    assert "idx_strategy_pipeline_runs_state_strategy_audit" in _index_names(db, "strategy_pipeline_runs")
    assert "idx_intervention_runs_state_strategy_audit" in _index_names(db, "intervention_runs")
    assert "idx_strategy_definitions_active" in _index_names(db, "strategy_definitions")


def test_complete_three_stage_run_can_be_saved(batch1_db):
    db = batch1_db
    scope = _make_scope(db)
    _insert_strategy_definition(db)

    run_id = _insert_complete_pipeline_run(db, scope)
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (run_id,))

    assert row["stage2_status"] == "SUCCEEDED"
    assert row["canonical_sub_state_code"] == "interpersonal_conflict"
    assert row["stage3_status"] == "SUCCEEDED"
    assert row["selected_strategy_id"] == "ER-001"
    assert row["publish_status"] == "PUBLISHED"


def test_three_stage_integrity_rejects_incomplete_stage2_and_publish_rows(batch1_db):
    db = batch1_db
    scope = _make_scope(db)
    now = db.now_str()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO strategy_pipeline_runs(
                run_uuid, group_id, session_id, discussion_id,
                stage2_status, should_intervene, sub_state_evidence_message_ids_json,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (str(uuid.uuid4()), scope["group_id"], scope["session_id"], scope["discussion_id"],
             "SUCCEEDED", 1, "[1]", now, now),
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO strategy_pipeline_runs(
                run_uuid, group_id, session_id, discussion_id,
                stage2_status, canonical_sub_state_code,
                should_intervene, sub_state_evidence_message_ids_json,
                publish_status, validated_intervention_text, published_message_id,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (str(uuid.uuid4()), scope["group_id"], scope["session_id"], scope["discussion_id"],
             "SUCCEEDED", "confusion", 1, "[1]", "PUBLISHED", "text", 10, now, now),
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO strategy_pipeline_runs(
                run_uuid, group_id, session_id, discussion_id,
                stage2_status, canonical_sub_state_code,
                should_intervene, sub_state_evidence_message_ids_json,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (str(uuid.uuid4()), scope["group_id"], scope["session_id"], scope["discussion_id"],
             "SUCCEEDED", "confusion", 1, "[]", now, now),
        )


def test_oi_strategy_rows_are_non_publishing_and_exclusive(batch1_db):
    db = batch1_db
    scope = _make_scope(db)
    now = db.now_str()

    with pytest.raises(sqlite3.IntegrityError):
        _insert_strategy_definition(db, strategy_id="OI-001", should_intervene=1, is_exclusive=0)

    _insert_strategy_definition(db, strategy_id="OI-001", should_intervene=0, is_exclusive=1)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO strategy_pipeline_runs(
                run_uuid, group_id, session_id, discussion_id,
                stage2_status, canonical_sub_state_code,
                should_intervene, inhibition_strategy_id,
                sub_state_evidence_message_ids_json,
                publish_status, selected_strategy_id,
                validated_intervention_text, published_message_id,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (str(uuid.uuid4()), scope["group_id"], scope["session_id"], scope["discussion_id"],
             "SUCCEEDED", "deep_thinking", 0, "OI-002", "[]",
             "PUBLISHED", "OI-002", "text", 10, now, now),
        )


def test_three_stage_intervention_run_trigger_rejects_missing_strategy_and_oi_publish(batch1_db):
    db = batch1_db
    scope = _make_scope(db)
    _insert_strategy_definition(db)
    pipeline_run_id = _insert_complete_pipeline_run(db, scope)
    now = db.now_str()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO intervention_runs(
                group_id, strategy_pipeline_run_id, status, publish_status,
                canonical_sub_state_code, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (scope["group_id"], pipeline_run_id, "PUBLISHED", "PUBLISHED",
             "interpersonal_conflict", now),
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO intervention_runs(
                group_id, strategy_pipeline_run_id, status, publish_status,
                canonical_sub_state_code, selected_strategy_id, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (scope["group_id"], pipeline_run_id, "PUBLISHED", "PUBLISHED",
             "deep_thinking", "OI-002", now),
        )

    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, strategy_pipeline_run_id, status, publish_status,
            canonical_sub_state_code, selected_strategy_id, created_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (scope["group_id"], pipeline_run_id, "PUBLISHED", "PUBLISHED",
         "interpersonal_conflict", "ER-001", now),
    )
    row = db.query_one("SELECT selected_strategy_id FROM intervention_runs WHERE id=?", (run_id,))
    assert row["selected_strategy_id"] == "ER-001"


def test_existing_database_missing_three_stage_tables_is_upgraded(batch1_db):
    db = batch1_db
    db.execute("DROP TABLE strategy_pipeline_runs")
    db.execute("DROP TABLE strategy_definitions")

    db.init_db()

    tables = {row["name"] for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "strategy_pipeline_runs" in tables
    assert "strategy_definitions" in tables


def test_repeated_migration_does_not_grow_three_stage_tables(batch1_db):
    db = batch1_db
    before = {
        table: db.query_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
        for table in ("strategy_pipeline_runs", "strategy_definitions")
    }

    db.init_db()
    db.init_db()

    after = {
        table: db.query_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
        for table in ("strategy_pipeline_runs", "strategy_definitions")
    }
    assert after == before


def test_three_stage_state_mapping_constants_are_complete():
    schema = importlib.import_module("services.three_stage_schema")

    assert set(schema.SUB_STATE_STRATEGY_ROUTES) == set(schema.CANONICAL_SUB_STATE_CODES)
    assert schema.normalize_canonical_sub_state("not-a-state") == "unknown_sub_state"
    assert schema.SUB_STATE_STRATEGY_ROUTES["deep_thinking"]["inhibition_strategy_id"] == "OI-002"
    assert schema.SUB_STATE_STRATEGY_ROUTES["off_topic_self_regulated"]["inhibition_strategy_id"] == "OI-003"
    assert "interpersonal_conflict" in schema.INTERVENTION_SUB_STATE_CODES
    assert "constructive_conflict" in schema.NON_INTERVENTION_SUB_STATE_CODES
    assert schema.dumps_json({"z": "中文", "a": [2, 1]}) == '{"a":[2,1],"z":"中文"}'
