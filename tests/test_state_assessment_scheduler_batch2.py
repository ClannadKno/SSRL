# -*- coding: utf-8 -*-
"""Batch 2 coverage for unified incremental assessment scheduling."""

from __future__ import annotations

import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def scheduler_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    batch_module = importlib.import_module("services.state_assessment_batch_service")
    queued = []

    monkeypatch.setattr(
        scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: queued.append((int(batch_id), int(delay or 0))),
    )
    monkeypatch.setattr(scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 4)
    monkeypatch.setattr(scheduler, "STATE_LLM_TIME_THRESHOLD_SECONDS", 180)
    monkeypatch.setattr(scheduler, "STATE_LLM_MIN_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 8)
    monkeypatch.setattr(scheduler, "STATE_LLM_CONTEXT_MESSAGES", 3)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_BACKOFF_SECONDS", 1)

    def make_scope(session_no: int):
        scope = seed_running_session(db, session_no=session_no, member_count=1)
        db.execute(
            """
            UPDATE experiment_sessions
               SET agent_mode='none',
                   strategy_agent_enabled=0,
                   emotion_agent_enabled=0,
                   agent_intervention_enabled=0,
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
        scope.update(
            {
                "discussion_id": discussion_id,
                "student_id": scope["students"][0][0],
            }
        )
        return scope

    return db, scheduler, batch_module.StateAssessmentBatchService, make_scope, queued


def _add_messages(db, scope, sequences, *, role="student", user_id=None):
    ids = []
    resolved_user_id = user_id or scope["student_id"]
    for sequence in sequences:
        ids.append(
            db.execute(
                """
                INSERT INTO messages(
                    group_id, user_id, content, sequence, sender_type, role,
                    session_id, session_no, task_id, discussion_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    scope["group_id"],
                    resolved_user_id,
                    f"{role} message {sequence}",
                    sequence,
                    role,
                    role,
                    scope["session_id"],
                    scope["session_no"],
                    scope["task_id"],
                    scope["discussion_id"],
                    db.now_str(),
                ),
            )
        )
    return ids


def _request(scheduler, scope, trigger="message_count_periodic", sequence=None, **kwargs):
    return scheduler.request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type=trigger,
        trigger_sequence=sequence,
        **kwargs,
    )


def _success_detection(message_ids, state="conflict_tension"):
    return {
        "monitor_run_id": 91,
        "state_llm_result": {
            "primary_state": state,
            "confidence": 0.9,
            "evidence_message_ids": list(message_ids),
        },
        "state_llm_meta": {
            "success": True,
            "analysis_failed": False,
            "analysis_skipped": False,
            "model_name": "mock-model",
            "prompt_version": "legacy-v1",
        },
    }


def test_message_and_time_triggers_share_one_batch_and_one_enqueue(scheduler_env):
    db, scheduler, _, make_scope, queued = scheduler_env
    scope = make_scope(901)
    _add_messages(db, scope, range(1, 5))

    first = _request(scheduler, scope, "message_count_periodic", 4)
    second = _request(scheduler, scope, "time_periodic", 4)

    assert first["created"] is True
    assert second["reason"] == "assessment_in_progress"
    assert second["assessment_batch_id"] == first["assessment_batch_id"]
    assert len(queued) == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM state_assessment_batches")["c"] == 1


def test_strong_trigger_upgrades_active_batch_priority_without_requeue(scheduler_env):
    db, scheduler, _, make_scope, queued = scheduler_env
    scope = make_scope(902)
    _add_messages(db, scope, range(1, 5))
    first = _request(scheduler, scope, "message_count_periodic", 4)
    upgraded = _request(scheduler, scope, "help_request", 4)
    row = db.query_one("SELECT * FROM state_assessment_batches WHERE id=?", (first["assessment_batch_id"],))

    assert upgraded["rerun_requested"] is True
    assert row["trigger_type"] == "help_request"
    assert row["request_priority"] == scheduler.TRIGGER_PRIORITIES["help_request"]
    assert len(queued) == 1


def test_partial_unique_index_is_database_single_flight_boundary(scheduler_env):
    db, _, _, make_scope, _ = scheduler_env
    scope = make_scope(903)
    now = db.now_str()
    db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            trigger_type, window_key, status, created_at, updated_at
        ) VALUES(?,?,?,?,?,'help_request','window-a','pending',?,?)
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"], 1, 1, now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO state_assessment_batches(
                group_id, session_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence,
                trigger_type, window_key, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,'rule_high_risk','window-b','running',?,?)
            """,
            (scope["group_id"], scope["session_id"], scope["discussion_id"], 2, 2, now, now),
        )


def test_different_discussions_can_claim_in_parallel(scheduler_env):
    db, scheduler, _, make_scope, queued = scheduler_env
    first_scope = make_scope(904)
    second_scope = make_scope(905)
    db.execute("UPDATE experiment_sessions SET status='running' WHERE id=?", (first_scope["session_id"],))
    _add_messages(db, first_scope, [1])
    _add_messages(db, second_scope, [1])

    first = _request(scheduler, first_scope, "help_request", 1)
    second = _request(scheduler, second_scope, "help_request", 1)

    assert first["assessment_batch_id"] != second["assessment_batch_id"]
    assert len(queued) == 2


def test_no_student_messages_or_agent_only_messages_do_not_create_batch(scheduler_env):
    db, scheduler, _, make_scope, queued = scheduler_env
    scope = make_scope(906)
    agent = db.query_one("SELECT id FROM users WHERE role='agent' ORDER BY id LIMIT 1")
    if not agent:
        agent_id = db.execute(
            "INSERT INTO users(username,password_hash,real_name,role,created_at) VALUES('batch2-agent','x','Agent','agent',?)",
            (db.now_str(),),
        )
    else:
        agent_id = agent["id"]
    _add_messages(db, scope, [1, 2, 3, 4], role="agent", user_id=agent_id)

    result = _request(scheduler, scope, "help_request", 4)

    assert result["reason"] == "no_new_student_messages"
    assert not queued
    assert db.query_one("SELECT COUNT(*) AS c FROM state_assessment_batches")["c"] == 0


def test_candidate_and_context_ranges_are_separate(scheduler_env):
    db, scheduler, service, make_scope, _ = scheduler_env
    scope = make_scope(907)
    _add_messages(db, scope, [1, 2, 3, 4])
    cursor = service.get_or_create_cursor(
        group_id=scope["group_id"], session_id=scope["session_id"], discussion_id=scope["discussion_id"]
    )
    db.execute(
        "UPDATE discussion_assessment_cursors SET last_finalized_student_sequence=2 WHERE id=?",
        (cursor["id"],),
    )

    result = _request(scheduler, scope, "help_request", 4)
    batch = result["batch"]

    assert (batch["candidate_start_sequence"], batch["candidate_end_sequence"]) == (3, 4)
    assert (batch["context_start_sequence"], batch["context_end_sequence"]) == (1, 2)


def test_messages_arriving_during_running_batch_continue_from_exact_boundary(scheduler_env, monkeypatch):
    db, scheduler, service, make_scope, queued = scheduler_env
    scope = make_scope(908)
    first_ids = _add_messages(db, scope, [1, 2, 3, 4])
    first = _request(scheduler, scope, "message_count_periodic", 4)
    _add_messages(db, scope, [5, 6])
    rerun = _request(scheduler, scope, "rule_high_risk", 6)
    assert rerun["assessment_batch_id"] == first["assessment_batch_id"]

    monitoring = importlib.import_module("services.discussion_pipeline_v2.monitoring_service")
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(lambda **kwargs: _success_detection(first_ids)),
    )
    outcome = scheduler.execute_state_assessment_batch(first["assessment_batch_id"])
    batches = db.query_all("SELECT * FROM state_assessment_batches ORDER BY id")

    assert outcome["succeeded"] is True
    assert [(row["candidate_start_sequence"], row["candidate_end_sequence"]) for row in batches] == [(1, 4), (5, 6)]
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"], session_id=scope["session_id"], discussion_id=scope["discussion_id"]
    ) == 4
    assert len(queued) == 2


def test_success_advances_cursor_to_claimed_end_only(scheduler_env, monkeypatch):
    db, scheduler, service, make_scope, _ = scheduler_env
    scope = make_scope(909)
    message_ids = _add_messages(db, scope, [10, 12, 15, 19])
    requested = _request(scheduler, scope, "message_count_periodic", 19)
    monitoring = importlib.import_module("services.discussion_pipeline_v2.monitoring_service")
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(lambda **kwargs: _success_detection(message_ids)),
    )

    scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])

    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"], session_id=scope["session_id"], discussion_id=scope["discussion_id"]
    ) == 19


def test_candidate_backlog_is_split_by_configured_max(scheduler_env, monkeypatch):
    db, scheduler, _, make_scope, _ = scheduler_env
    scope = make_scope(910)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 3)
    message_ids = _add_messages(db, scope, range(1, 8))
    first = _request(scheduler, scope, "message_count_periodic", 7)
    assert (first["batch"]["candidate_start_sequence"], first["batch"]["candidate_end_sequence"]) == (1, 3)
    assert first["batch"]["rerun_requested"] == 1

    monitoring = importlib.import_module("services.discussion_pipeline_v2.monitoring_service")
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(lambda **kwargs: _success_detection(message_ids[:3])),
    )
    scheduler.execute_state_assessment_batch(first["assessment_batch_id"])
    batches = db.query_all("SELECT candidate_start_sequence, candidate_end_sequence FROM state_assessment_batches ORDER BY id")

    assert [(row["candidate_start_sequence"], row["candidate_end_sequence"]) for row in batches] == [(1, 3), (4, 6)]


def test_structured_failure_keeps_cursor_and_does_not_repeat_whole_batch(scheduler_env, monkeypatch):
    db, scheduler, service, make_scope, queued = scheduler_env
    scope = make_scope(911)
    _add_messages(db, scope, range(1, 5))
    requested = _request(scheduler, scope, "message_count_periodic", 4)
    monitoring = importlib.import_module("services.discussion_pipeline_v2.monitoring_service")
    failure = {
        "state_llm_result": {"primary_state": "unknown", "evidence_message_ids": []},
        "state_llm_meta": {
            "success": False,
            "analysis_failed": True,
            "analysis_skipped": False,
            "failure_type": "schema_validation_error",
            "failure_message": "bad json",
        },
    }
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(lambda **kwargs: failure),
    )

    first = scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])
    second = scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])
    row = db.query_one("SELECT * FROM state_assessment_batches WHERE id=?", (requested["assessment_batch_id"],))

    assert first["retry"]["retried"] is False
    assert first["retry"]["reason"] == "structured_output_retry_exhausted"
    assert second["claimed"] is False
    assert row["status"] == "failed"
    assert row["attempt_count"] == 1
    assert row["next_retry_at"] is None
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"], session_id=scope["session_id"], discussion_id=scope["discussion_id"]
    ) == 0
    assert len(queued) == 1


def test_discussion_close_blocks_new_and_queued_assessments(scheduler_env):
    db, scheduler, _, make_scope, _ = scheduler_env
    scope = make_scope(912)
    _add_messages(db, scope, range(1, 5))
    db.execute("UPDATE group_session_discussions SET status='submitted' WHERE id=?", (scope["discussion_id"],))

    blocked = _request(scheduler, scope, "help_request", 4)
    assert blocked["reason"] == "discussion_not_running"


def test_multiworker_requests_create_one_window(scheduler_env):
    db, scheduler, _, make_scope, queued = scheduler_env
    scope = make_scope(913)
    _add_messages(db, scope, range(1, 5))

    def call_once(_):
        return _request(scheduler, scope, "message_count_periodic", 4)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call_once, range(4)))

    assert sum(1 for item in results if item.get("created")) == 1
    assert len(queued) == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM state_assessment_batches")["c"] == 1


def test_time_trigger_requires_due_time_and_at_least_one_new_student_message(scheduler_env, monkeypatch):
    db, scheduler, _, make_scope, queued = scheduler_env
    scope = make_scope(914)
    _add_messages(db, scope, [1])
    not_due = _request(scheduler, scope, "time_periodic", 1)
    assert not_due["reason"] == "time_threshold_not_reached"

    monkeypatch.setattr(scheduler, "STATE_LLM_TIME_THRESHOLD_SECONDS", 0)
    due = _request(scheduler, scope, "time_periodic", 1)
    assert due["created"] is True
    assert len(queued) == 1


def test_periodic_scanner_uses_unified_request_and_skips_empty_discussion(scheduler_env):
    _, scheduler, _, make_scope, queued = scheduler_env
    make_scope(915)

    summary = scheduler.scan_due_state_assessments()

    assert summary["scanned"] == 1
    assert summary["enqueued"] == 0
    assert summary["skipped"] == 1
    assert not queued


def test_schema_exposes_retry_and_single_flight_indexes(scheduler_env):
    db, _, _, _, _ = scheduler_env
    columns = {row["name"] for row in db.query_all("PRAGMA table_info(state_assessment_batches)")}
    indexes = {row["name"] for row in db.query_all("PRAGMA index_list(state_assessment_batches)")}

    assert {"request_priority", "attempt_count", "max_attempts", "next_retry_at", "enqueued_at"} <= columns
    assert {"idx_state_assessment_batches_one_active", "idx_state_assessment_batches_retry"} <= indexes
