# -*- coding: utf-8 -*-
"""Batch 1 regression tests for incremental state assessment persistence."""

from __future__ import annotations

import importlib

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def batch_env(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    service_module = importlib.import_module("services.state_assessment_batch_service")

    def make_scope(session_no: int):
        scope = seed_running_session(db, session_no=session_no, member_count=1)
        discussion_id = db.execute(
            """
            INSERT INTO group_session_discussions(
                session_id, group_id, status, created_at, updated_at
            ) VALUES(?,?,'running',?,?)
            """,
            (
                scope["session_id"],
                scope["group_id"],
                db.now_str(),
                db.now_str(),
            ),
        )
        scope["discussion_id"] = discussion_id
        scope["student_id"] = scope["students"][0][0]
        return scope

    return db, service_module.StateAssessmentBatchService, make_scope


def _add_student_messages(db, scope, sequences):
    for sequence in sequences:
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_id, session_no, task_id, discussion_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                scope["student_id"],
                f"student message {sequence}",
                sequence,
                "student",
                "student",
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                scope["discussion_id"],
                db.now_str(),
            ),
        )


def _create_batch(service, scope, start=1, end=4):
    return service.create_batch(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        candidate_start_sequence=start,
        candidate_end_sequence=end,
        context_start_sequence=start,
        context_end_sequence=end,
        trigger_type="rule_high_risk",
        trigger_sequence=end,
        model="test-model",
        prompt_version="batch1-test",
    )


def _claim(service, created):
    batch_id = created["batch"]["id"]
    claimed = service.claim_batch(batch_id)
    assert claimed["claimed"] is True
    assert claimed["batch"]["status"] == "running"
    return batch_id


def test_database_initialization_is_idempotent_and_schema_is_complete(batch_env):
    db, _, _ = batch_env

    db.init_db()
    db.init_db()

    batch_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(state_assessment_batches)")
    }
    cursor_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(discussion_assessment_cursors)")
    }
    segment_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(collaboration_state_segments)")
    }
    intervention_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(intervention_runs)")
    }

    assert {
        "candidate_start_sequence",
        "candidate_end_sequence",
        "window_key",
        "rerun_requested",
        "parsed_response",
    } <= batch_columns
    assert {
        "last_finalized_student_sequence",
        "observation_started_sequence",
        "observation_status",
    } <= cursor_columns
    assert {
        "assessment_batch_id",
        "start_sequence",
        "end_sequence",
        "evidence_sequences",
        "segment_order",
        "is_active_at_batch_end",
    } <= segment_columns
    assert {"assessment_batch_id", "target_segment_id", "reason_code"} <= intervention_columns


def test_cursor_get_or_create_is_unique_per_discussion(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(801)

    first = service.get_or_create_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    second = service.get_or_create_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )

    assert first["id"] == second["id"]
    count = db.query_one(
        """
        SELECT COUNT(*) AS count FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert count["count"] == 1


def test_duplicate_candidate_window_returns_existing_batch(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(802)

    first = _create_batch(service, scope)
    second = _create_batch(service, scope)

    assert first["created"] is True
    assert second["created"] is False
    assert first["batch"]["id"] == second["batch"]["id"]
    count = db.query_one(
        """
        SELECT COUNT(*) AS count FROM state_assessment_batches
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert count["count"] == 1
    assert service.get_active_batch(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )["id"] == first["batch"]["id"]


def test_equal_sequence_windows_are_isolated_across_sessions_and_discussions(batch_env):
    db, service, make_scope = batch_env
    first_scope = make_scope(803)
    second_scope = make_scope(804)

    first = _create_batch(service, first_scope, 10, 12)
    second = _create_batch(service, second_scope, 10, 12)

    assert first["batch"]["id"] != second["batch"]["id"]
    assert db.query_one("SELECT COUNT(*) AS count FROM state_assessment_batches")["count"] == 2


def test_multiple_segments_save_in_order_and_advance_cursor_atomically(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(805)
    _add_student_messages(db, scope, range(1, 5))
    batch_id = _claim(service, _create_batch(service, scope))

    before = service.get_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert before["last_finalized_student_sequence"] == 0

    result = service.save_successful_segments(
        batch_id,
        [
            {
                "state": "positive_collaboration",
                "start_sequence": 1,
                "end_sequence": 2,
                "confidence": 0.82,
                "evidence_sequences": [1, 2],
                "segment_order": 0,
            },
            {
                "state": "conflict_tension",
                "start_sequence": 3,
                "end_sequence": 4,
                "confidence": 0.91,
                "evidence_sequences": [3, 4],
                "segment_order": 1,
                "is_active_at_batch_end": True,
            },
        ],
        parsed_response={"segments": 2},
    )

    assert result["batch"]["status"] == "succeeded"
    assert [row["segment_order"] for row in result["segments"]] == [0, 1]
    assert {row["assessment_batch_id"] for row in result["segments"]} == {batch_id}
    after = service.get_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert after["last_finalized_student_sequence"] == 4


def test_failed_batch_does_not_advance_cursor(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(806)
    _add_student_messages(db, scope, range(1, 5))
    batch_id = _claim(service, _create_batch(service, scope))

    failed = service.mark_batch_failed(
        batch_id, error_code="schema_validation_error", error_detail="test failure"
    )

    assert failed["status"] == "failed"
    cursor = service.get_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert cursor["last_finalized_student_sequence"] == 0
    assert db.query_one(
        "SELECT COUNT(*) AS count FROM collaboration_state_segments WHERE assessment_batch_id=?",
        (batch_id,),
    )["count"] == 0


def test_segment_validation_failure_rolls_back_entire_success_transaction(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(807)
    _add_student_messages(db, scope, range(1, 5))
    batch_id = _claim(service, _create_batch(service, scope))

    with pytest.raises(ValueError, match="segment_outside_candidate_window"):
        service.save_successful_segments(
            batch_id,
            [
                {
                    "state": "positive_collaboration",
                    "start_sequence": 1,
                    "end_sequence": 2,
                    "evidence_sequences": [1],
                },
                {
                    "state": "conflict_tension",
                    "start_sequence": 3,
                    "end_sequence": 9,
                    "evidence_sequences": [3],
                },
            ],
        )

    assert db.query_one(
        "SELECT COUNT(*) AS count FROM collaboration_state_segments WHERE assessment_batch_id=?",
        (batch_id,),
    )["count"] == 0
    assert db.query_one(
        "SELECT status FROM state_assessment_batches WHERE id=?", (batch_id,)
    )["status"] == "running"
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    ) == 0


def test_legacy_segment_remains_readable_by_original_query(batch_env):
    db, _, make_scope = batch_env
    scope = make_scope(808)
    _add_student_messages(db, scope, [1, 2])
    now = db.now_str()
    legacy_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id,
            state_code, segment_kind, start_message_id, end_message_id,
            evidence_message_ids_json, confidence, source, is_finalized,
            dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,'message_range',?,?,?,?,?,1,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            "positive_collaboration",
            1,
            2,
            "[1,2]",
            0.8,
            "strategy_llm",
            "legacy-batch1-read",
            now,
            now,
        ),
    )

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    rows = CollaborationStateSegmentService.list_segments(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        session_no=scope["session_no"],
        include_provisional=False,
    )
    assert [row["id"] for row in rows] == [legacy_id]
    assert rows[0]["start_message_id"] == 1
    assert rows[0]["end_message_id"] == 2


def test_new_segments_mirror_explicit_and_legacy_sequence_fields(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(809)
    _add_student_messages(db, scope, range(5, 8))
    batch_id = _claim(service, _create_batch(service, scope, 5, 7))

    result = service.save_successful_segments(
        batch_id,
        [
            {
                "state": "frustration_stuck",
                "start_sequence": 5,
                "end_sequence": 7,
                "evidence_sequences": [5, 7],
                "confidence": 0.88,
            }
        ],
    )
    segment = result["segments"][0]

    assert segment["start_sequence"] == segment["start_message_id"] == 5
    assert segment["end_sequence"] == segment["end_message_id"] == 7
    assert segment["evidence_sequences"] == segment["evidence_message_ids_json"] == "[5,7]"


def test_message_classification_supports_confirmed_observing_and_unclassified(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(810)
    _add_student_messages(db, scope, [1, 2, 3])
    batch_id = _claim(service, _create_batch(service, scope, 1, 1))
    service.save_successful_segments(
        batch_id,
        [
            {
                "state": "positive_collaboration",
                "start_sequence": 1,
                "end_sequence": 1,
                "evidence_sequences": [1],
                "confidence": 0.9,
            }
        ],
    )

    confirmed = service.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=1,
    )
    unclassified = service.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=2,
    )
    service.set_observation(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        observation_status="observing",
        observation_started_sequence=2,
        last_intervention_sequence=1,
    )
    observing = service.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=2,
    )

    assert confirmed["assessment_status"] == "confirmed"
    assert confirmed["semantic_state"] == "positive_collaboration"
    assert unclassified["assessment_status"] == "unclassified"
    assert observing["assessment_status"] == "observing"


def test_intervention_run_links_to_exact_batch_segment(batch_env):
    db, service, make_scope = batch_env
    scope = make_scope(811)
    _add_student_messages(db, scope, [1, 2])
    batch_id = _claim(service, _create_batch(service, scope, 1, 2))
    saved = service.save_successful_segments(
        batch_id,
        [
            {
                "state": "conflict_tension",
                "start_sequence": 1,
                "end_sequence": 2,
                "evidence_sequences": [1, 2],
                "confidence": 0.94,
                "is_active_at_batch_end": True,
            }
        ],
    )
    segment_id = saved["segments"][0]["id"]
    run_id = db.execute(
        """
        INSERT INTO intervention_runs(group_id, session_id, status, created_at)
        VALUES(?,?,'pending',?)
        """,
        (scope["group_id"], scope["session_id"], db.now_str()),
    )

    linked = service.link_intervention_to_segment(
        run_id,
        assessment_batch_id=batch_id,
        target_segment_id=segment_id,
        trigger_type="rule_high_risk",
        reason_code="active_conflict",
    )

    assert linked["assessment_batch_id"] == batch_id
    assert linked["target_segment_id"] == segment_id
    assert linked["reason_code"] == "active_conflict"
