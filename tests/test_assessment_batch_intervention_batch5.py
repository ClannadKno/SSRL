# -*- coding: utf-8 -*-
"""Batch 5: multi-segment decisions drive one scoped strategy intervention."""

from __future__ import annotations

import importlib
import json

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def batch5_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_DRY_RUN", False)
    monkeypatch.setattr(config, "LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED", True)
    scope = seed_running_session(db, session_no=950, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
        SET strategy_agent_enabled=1, agent_intervention_enabled=1
        WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (scope["task_id"],),
    )
    scope["discussion_id"] = db.execute(
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
    scope["student_id"] = scope["students"][0][0]
    return db, scope


def _add_students(db, scope, sequences):
    for sequence in sequences:
        exists = db.query_one(
            "SELECT id FROM messages WHERE group_id=? AND sequence=?",
            (scope["group_id"], sequence),
        )
        if exists:
            continue
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
                f"student {sequence}",
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


def _make_batch(
    db,
    scope,
    *,
    start,
    end,
    segments,
    active_index,
    needed,
    target_index=None,
    reason="constructive_progress",
    message=None,
    trigger_type="message_count_periodic",
):
    _add_students(db, scope, range(start, end + 1))
    service_module = importlib.import_module("services.state_assessment_batch_service")
    service = service_module.StateAssessmentBatchService
    created = service.create_batch(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        candidate_start_sequence=start,
        candidate_end_sequence=end,
        trigger_type=trigger_type,
        trigger_sequence=end,
        model="batch5-model",
        prompt_version="batch5-v1",
    )
    batch_id = created["batch"]["id"]
    claimed = service.claim_batch(batch_id)
    assert claimed["claimed"] is True
    parsed = {
        "segments": segments,
        "active_segment_index": active_index,
        "intervention": {
            "needed": needed,
            "target_segment_index": target_index,
            "reason_code": reason,
            "message": message,
        },
    }
    saved = service.save_successful_segments(
        batch_id,
        segments,
        raw_response=json.dumps(parsed, ensure_ascii=False),
        parsed_response=parsed,
        model="batch5-model",
        prompt_version="batch5-v1",
    )
    batch = db.query_one("SELECT * FROM state_assessment_batches WHERE id=?", (batch_id,))
    stored = json.loads(batch["parsed_response"])
    return {
        "batch_id": batch_id,
        "segments": [dict(row) for row in saved["segments"]],
        "parsed": stored,
        "target_segment_id": (stored.get("intervention") or {}).get("target_segment_id"),
    }


def _segment(state, start, end, evidence, *, active=False, confidence=0.9):
    return {
        "state": state,
        "start_sequence": start,
        "end_sequence": end,
        "confidence": confidence,
        "evidence_sequences": evidence,
        "source": "llm",
        "assessment_status": "confirmed",
        "is_active_at_batch_end": active,
    }


def _execute(batch_id):
    service = importlib.import_module("services.assessment_batch_intervention_service")
    return service.AssessmentBatchInterventionService.execute(batch_id, monitor_run_id=None)


def test_recovered_historical_risk_does_not_intervene(batch5_env):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=1,
        end=4,
        segments=[
            _segment("frustration_stuck", 1, 2, [1, 2]),
            _segment("positive_collaboration", 3, 4, [3, 4], active=True),
        ],
        active_index=1,
        needed=False,
        reason="risk_already_recovered",
    )

    result = _execute(batch["batch_id"])

    assert result["skipped"] is True
    assert result["reason"] == "intervention_not_needed"
    assert db.query_one("SELECT COUNT(*) AS c FROM intervention_runs")["c"] == 0


@pytest.mark.parametrize(
    ("state", "reason"),
    [("conflict_tension", "active_conflict"), ("frustration_stuck", "group_stuck")],
)
def test_active_risk_publishes_from_batch_without_second_review(batch5_env, state, reason):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=10,
        end=12,
        segments=[_segment(state, 10, 12, [10, 11, 12], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason=reason,
        message="先暂停一下，按共同标准逐项核对证据。",
    )

    result = _execute(batch["batch_id"])

    assert result["published"] is True
    assert result["publish_attempts"] == 1
    run = db.query_one("SELECT * FROM intervention_runs WHERE id=?", (result["intervention_run_id"],))
    message = db.query_one("SELECT * FROM messages WHERE id=?", (result["message_id"],))
    assert run["assessment_batch_id"] == batch["batch_id"]
    assert run["target_segment_id"] == batch["target_segment_id"]
    assert run["reason_code"] == reason
    assert run["detected_state"] == state
    assert run["message_id"] == message["id"]
    assert message["intervention_run_id"] == run["id"]
    assert json.loads(run["evidence_sequences_json"]) == [10, 11, 12]


def test_non_active_risk_target_is_rejected(batch5_env):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=20,
        end=23,
        segments=[
            _segment("conflict_tension", 20, 21, [20, 21]),
            _segment("positive_collaboration", 22, 23, [22, 23], active=True),
        ],
        active_index=1,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="不应发布。",
    )

    result = _execute(batch["batch_id"])

    assert result["reason"] == "target_segment_not_active"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0


def test_same_target_segment_is_idempotent(batch5_env):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=30,
        end=31,
        segments=[_segment("conflict_tension", 30, 31, [30, 31], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="先暂停争论并核对共同标准。",
    )

    first = _execute(batch["batch_id"])
    second = _execute(batch["batch_id"])

    assert first["published"] is True
    assert second["duplicate"] is True
    assert second["existing_run_id"] == first["intervention_run_id"]
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM intervention_runs WHERE target_segment_id=?",
        (batch["target_segment_id"],),
    )["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 1


def test_pending_help_blocks_only_overlapping_request_evidence(batch5_env):
    db, scope = batch5_env
    _add_students(db, scope, [40])
    source = db.query_one(
        "SELECT id FROM messages WHERE group_id=? AND sequence=40",
        (scope["group_id"],),
    )
    help_id = db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, handling_status, source_message_id,
            help_request_message_sequence, request_text, created_at
        ) VALUES(?,?,?,?,?,'RUNNING','running',?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["student_id"],
            scope["task_id"],
            scope["session_no"],
            scope["session_id"],
            source["id"],
            40,
            "请帮忙",
            db.now_str(),
        ),
    )
    blocked = _make_batch(
        db,
        scope,
        start=40,
        end=41,
        segments=[_segment("frustration_stuck", 40, 41, [40, 41], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="group_stuck",
        message="先把卡点拆成两个小问题。",
    )

    result = _execute(blocked["batch_id"])

    assert result["reason"] == "same_issue_help_in_progress"
    assert result["help_request_id"] == help_id
    assert result["guard_evaluated"] is True
    assert result["guard_blocked"] is True
    assert result["evidence_overlap"] is True
    db.execute(
        """
        UPDATE help_requests
        SET status='COMPLETED', handling_status='handled', handled_at=?,
            covered_until_sequence=40, intervention_run_id=999
        WHERE id=?
        """,
        (db.now_str(), help_id),
    )
    independent = _make_batch(
        db,
        scope,
        start=44,
        end=45,
        segments=[_segment("conflict_tension", 44, 45, [44, 45], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="先暂停争论，按共同标准比较两个方案。",
    )

    later = _execute(independent["batch_id"])

    assert later["published"] is True


def test_overlapping_evidence_is_blocked_but_independent_evidence_is_allowed(batch5_env):
    db, scope = batch5_env
    first_batch = _make_batch(
        db,
        scope,
        start=50,
        end=52,
        segments=[_segment("conflict_tension", 50, 52, [50, 51], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="先暂停并核对证据。",
    )
    first = _execute(first_batch["batch_id"])
    assert first["published"] is True

    overlap_batch = _make_batch(
        db,
        scope,
        start=51,
        end=54,
        segments=[_segment("conflict_tension", 51, 54, [51, 54], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="不应重复发布。",
    )
    overlap = _execute(overlap_batch["batch_id"])
    assert overlap["reason"] == "overlapping_evidence_already_claimed"

    independent_batch = _make_batch(
        db,
        scope,
        start=56,
        end=57,
        segments=[_segment("conflict_tension", 56, 57, [56, 57], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="这是新的独立冲突证据。",
    )
    independent = _execute(independent_batch["batch_id"])
    assert independent["published"] is True


def test_publish_failure_retries_once_marks_failed_and_does_not_observe(
    batch5_env, monkeypatch
):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=60,
        end=61,
        segments=[_segment("frustration_stuck", 60, 61, [60, 61], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="group_stuck",
        message="先把卡点拆开。",
    )
    publisher = importlib.import_module("services.agent_intervention_publisher")
    calls = []

    def fail_publish(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "reason": "publish_failed"}

    monkeypatch.setattr(publisher, "publish_agent_intervention", fail_publish)

    result = _execute(batch["batch_id"])

    assert result["published"] is False
    assert result["publish_attempts"] == 2
    assert len(calls) == 2
    run = db.query_one("SELECT * FROM intervention_runs WHERE id=?", (result["intervention_run_id"],))
    cursor = db.query_one(
        """
        SELECT observation_status FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert run["status"] == "FAILED"
    assert run["retry_count"] == 1
    assert run["message_id"] is None
    assert cursor["observation_status"] == "inactive"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0


def test_success_starts_observation_only_for_following_students(batch5_env):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=70,
        end=71,
        segments=[_segment("conflict_tension", 70, 71, [70, 71], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="先暂停争论并逐项比较。",
    )

    result = _execute(batch["batch_id"])
    cursor = db.query_one(
        """
        SELECT * FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )

    assert result["published"] is True
    assert cursor["observation_status"] == "observing"
    assert cursor["observation_started_sequence"] is None
    intervention_message = db.query_one(
        "SELECT sequence FROM messages WHERE id=?", (result["message_id"],)
    )
    _add_students(db, scope, [intervention_message["sequence"] + 1])
    conn = db.db()
    try:
        db.record_observation_student_sequence(
            conn,
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            student_sequence=intervention_message["sequence"] + 1,
        )
        conn.commit()
    finally:
        conn.close()
    started = db.query_one(
        "SELECT observation_started_sequence FROM discussion_assessment_cursors WHERE discussion_id=?",
        (scope["discussion_id"],),
    )
    assert started["observation_started_sequence"] == intervention_message["sequence"] + 1


def test_discussion_closed_before_publish_creates_no_message(batch5_env):
    db, scope = batch5_env
    batch = _make_batch(
        db,
        scope,
        start=80,
        end=81,
        segments=[_segment("conflict_tension", 80, 81, [80, 81], active=True)],
        active_index=0,
        needed=True,
        target_index=0,
        reason="active_conflict",
        message="不应发布。",
    )
    db.execute(
        "UPDATE group_session_discussions SET status='submitted', submitted_at=? WHERE id=?",
        (db.now_str(), scope["discussion_id"]),
    )

    result = _execute(batch["batch_id"])

    assert result["published"] is False
    assert result["skipped"] is True
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0


def test_batch5_schema_migration_is_idempotent_and_has_target_unique_index(batch5_env):
    db, _scope = batch5_env
    db.init_db()
    db.init_db()

    help_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(help_requests)")
    }
    run_columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(intervention_runs)")
    }
    indexes = {
        row["name"] for row in db.query_all("PRAGMA index_list(intervention_runs)")
    }
    assert {
        "help_request_message_sequence",
        "handled_at",
        "handling_status",
        "covered_until_sequence",
    } <= help_columns
    assert {
        "discussion_id",
        "assessment_batch_id",
        "target_segment_id",
        "reason_code",
        "guard_result",
        "guard_reason",
        "retry_count",
        "raw_response",
        "started_at",
    } <= run_columns
    assert "idx_intervention_runs_target_segment_strategy" in indexes
    assert db.query_one("PRAGMA integrity_check")[0] == "ok"
