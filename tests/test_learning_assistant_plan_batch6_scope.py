# -*- coding: utf-8 -*-
"""Batch 6 acceptance tests for canonical discussion scope persistence."""

from __future__ import annotations

import sqlite3

import pytest

from tests.helpers import seed_running_session


SCOPE_COLUMNS = {
    "messages": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
        "scope_resolved_from",
        "legacy_scope_fallback",
        "scope_fallback_reason",
    },
    "state_assessments": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "group_states": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "collaboration_state_segments": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "state_assessment_batches": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "discussion_assessment_cursors": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "monitor_runs": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
        "scope_resolved_from",
        "legacy_scope_fallback",
        "scope_fallback_reason",
    },
    "intervention_runs": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "intervention_logs": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "help_requests": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "agent_research_events": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
    "collaboration_state_finalizations": {
        "session_id",
        "session_no",
        "task_id",
        "discussion_id",
    },
}


@pytest.fixture
def canonical_scope(test_env):
    import db
    from services.group_discussion_runtime_service import (
        enter_group_discussion_stage,
    )

    db.ensure_database_ready()
    scope = seed_running_session(
        db,
        session_no=61,
        member_count=1,
        limit_minutes=20,
    )
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
    student_id = scope["students"][0][0]
    discussion = enter_group_discussion_stage(
        scope["session_id"],
        scope["group_id"],
        student_id,
    )
    return db, {
        **scope,
        "student_id": student_id,
        "discussion_id": discussion["id"],
    }


def _scope_tuple(row):
    return tuple(
        row[name]
        for name in ("group_id", "session_id", "session_no", "task_id", "discussion_id")
    )


def _expected_scope(scope):
    return (
        scope["group_id"],
        scope["session_id"],
        scope["session_no"],
        scope["task_id"],
        scope["discussion_id"],
    )


def _create_reused_session_scope(db, first):
    now = db.now_str()
    db.execute("DROP INDEX IF EXISTS idx_experiment_sessions_session_no_unique")
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            first["session_no"],
            "discussion",
            first["task_id"],
            "ended",
            now,
            20,
            now,
            now,
        ),
    )
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'closed',?,?,?)
        """,
        (session_id, first["group_id"], now, now, now),
    )
    return {
        **first,
        "session_id": session_id,
        "discussion_id": discussion_id,
    }


def _create_legacy_scope_schema(conn):
    conn.executescript(
        """
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            session_no INTEGER, task_id INTEGER, sequence INTEGER
        );
        CREATE TABLE state_assessments(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            session_no INTEGER, task_id INTEGER, created_at TEXT
        );
        CREATE TABLE group_states(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_no INTEGER,
            task_id INTEGER, created_at TEXT
        );
        CREATE TABLE collaboration_state_segments(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            session_no INTEGER, task_id INTEGER, start_sequence INTEGER
        );
        CREATE TABLE state_assessment_batches(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            discussion_id INTEGER, status TEXT
        );
        CREATE TABLE discussion_assessment_cursors(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            discussion_id INTEGER
        );
        CREATE TABLE monitor_runs(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            task_id INTEGER, cutoff_sequence INTEGER
        );
        CREATE TABLE intervention_runs(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            task_id INTEGER, discussion_id INTEGER, created_at TEXT
        );
        CREATE TABLE intervention_logs(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            task_id INTEGER, created_at TEXT
        );
        CREATE TABLE help_requests(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            session_no INTEGER, task_id INTEGER, status TEXT
        );
        CREATE TABLE agent_research_events(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            session_no INTEGER, task_id INTEGER, created_at TEXT
        );
        CREATE TABLE collaboration_state_finalizations(
            id INTEGER PRIMARY KEY, group_id INTEGER, session_id INTEGER,
            session_no INTEGER, task_id INTEGER, status TEXT
        );
        """
    )


def test_scope_migration_is_additive_idempotent_and_indexed(test_env):
    import db
    import migrations

    db.ensure_database_ready()
    db.init_db()
    db.init_db()
    for table, expected in SCOPE_COLUMNS.items():
        actual = {
            row["name"]
            for row in db.query_all(f"PRAGMA table_info({table})")
        }
        assert expected <= actual, (table, expected - actual)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_legacy_scope_schema(conn)
    before = conn.execute("PRAGMA page_count").fetchone()[0]
    migrations._migration_unified_discussion_scope(conn)
    conn.commit()
    after_first = conn.execute("PRAGMA page_count").fetchone()[0]
    migrations._migration_unified_discussion_scope(conn)
    conn.commit()
    after_second = conn.execute("PRAGMA page_count").fetchone()[0]

    assert after_second == after_first
    assert after_first - before < 64
    for table, expected in SCOPE_COLUMNS.items():
        actual = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        assert expected <= actual, (table, expected - actual)

    plan = db.query_all(
        """
        EXPLAIN QUERY PLAN
        SELECT id FROM messages
        WHERE group_id=? AND session_id=? AND discussion_id=? AND sequence>=?
        """,
        (1, 1, 1, 0),
    )
    assert any(
        "idx_messages_discussion_scope" in row["detail"]
        for row in plan
    )


def test_new_state_chain_persists_one_complete_scope(
    canonical_scope,
    monkeypatch,
):
    db, scope = canonical_scope
    expected = _expected_scope(scope)

    message = db.create_message(
        scope["group_id"],
        scope["student_id"],
        "We should compare both proposals.",
        role="student",
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert _scope_tuple(message) == expected
    assert message["scope_resolved_from"] == "session"
    assert message["legacy_scope_fallback"] is False

    from services.discussion_pipeline_v2.monitor_run_repo import MonitorRunRepo

    monitor_id = MonitorRunRepo.create(
        scope["group_id"],
        message["sequence"],
        trigger_type="new_message",
        shadow=False,
    )

    from services.state_assessment_service import persist_state_assessment

    assessment = persist_state_assessment(
        {
            "group_id": scope["group_id"],
            "session_id": scope["session_id"],
            "session_no": scope["session_no"],
            "task_id": scope["task_id"],
            "discussion_id": scope["discussion_id"],
            "state_code": "positive_collaboration",
            "state_score": 0.88,
            "rule_assessment": {"assessment_status": "state_detected"},
            "context_json": {
                "discussion_id": scope["discussion_id"],
                "window_start": db.now_str(),
                "window_end": db.now_str(),
            },
            "feature_json": {},
        }
    )

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )

    monitor_segment = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        session_no=scope["session_no"],
        task_id=scope["task_id"],
        discussion_id=scope["discussion_id"],
        state_code="positive_collaboration",
        start_message_id=message["sequence"],
        end_message_id=message["sequence"],
        evidence_message_ids=[message["sequence"]],
        confidence=0.88,
        source_run_id=monitor_id,
        assessment_id=assessment["assessment_id"],
        trigger_sequence=message["sequence"],
    )

    from services.state_assessment_batch_service import StateAssessmentBatchService

    created = StateAssessmentBatchService.create_batch(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        candidate_start_sequence=message["sequence"],
        candidate_end_sequence=message["sequence"],
        context_start_sequence=message["sequence"],
        context_end_sequence=message["sequence"],
        trigger_type="rule_high_risk",
        trigger_sequence=message["sequence"],
    )
    batch_id = created["batch"]["id"]
    assert StateAssessmentBatchService.claim_batch(batch_id)["claimed"] is True
    batch_result = StateAssessmentBatchService.save_successful_segments(
        batch_id,
        [
            {
                "state": "positive_collaboration",
                "start_sequence": message["sequence"],
                "end_sequence": message["sequence"],
                "confidence": 0.9,
                "evidence_sequences": [message["sequence"]],
                "segment_order": 0,
                "is_active_at_batch_end": True,
            }
        ],
    )
    llm_segment_id = batch_result["segments"][0]["id"]

    from services.intervention_pipeline_v2.intervention_run_repo import (
        InterventionRunRepo,
    )

    intervention_run_id = InterventionRunRepo.create(
        group_id=scope["group_id"],
        monitor_run_id=monitor_id,
        cutoff_sequence=message["sequence"],
        detected_state="positive_collaboration",
        confidence=0.88,
        dry_run=False,
        trigger_type="new_message",
        state_assessment_id=assessment["assessment_id"],
        target_segment_id=monitor_segment["segment_id"],
    )

    from services.agent_intervention_publisher import publish_agent_intervention

    published = publish_agent_intervention(
        group_id=scope["group_id"],
        message="Please summarize the shared decision.",
        trigger_source="new_message",
        intervention_run_id=intervention_run_id,
        monitor_run_id=monitor_id,
        state_assessment_id=assessment["assessment_id"],
        source_student_message_id=message["id"],
        session_id=scope["session_id"],
        session_no=scope["session_no"],
        task_id=scope["task_id"],
        discussion_id=scope["discussion_id"],
        cutoff_sequence=message["sequence"],
    )
    assert published["ok"] is True

    import agent.help_tasks as help_tasks

    monkeypatch.setattr(help_tasks, "_execute_help_flow", lambda _request_id: None)
    from services.student_help_service import request_student_help

    help_result = request_student_help(
        scope["group_id"],
        scope["student_id"],
        "Can you help us compare the options?",
        request_message=message,
        record_request_message=False,
    )

    event_id = db.create_agent_research_event(
        group_id=scope["group_id"],
        agent_type="strategy",
        event_type="scope_test",
        message_id=message["id"],
        monitor_run_id=monitor_id,
        intervention_run_id=intervention_run_id,
    )

    from services.collaboration_state_finalization_service import (
        finalize_collaboration_states,
    )

    finalization = finalize_collaboration_states(
        scope["group_id"],
        scope["session_id"],
        "session_end",
    )
    assert finalization["finalization_id"] is not None

    rows = [
        db.query_one("SELECT * FROM messages WHERE id=?", (message["id"],)),
        db.query_one("SELECT * FROM monitor_runs WHERE id=?", (monitor_id,)),
        db.query_one(
            "SELECT * FROM state_assessments WHERE id=?",
            (assessment["assessment_id"],),
        ),
        db.query_one(
            "SELECT * FROM group_states WHERE id=?",
            (assessment["group_state_id"],),
        ),
        db.query_one(
            "SELECT * FROM collaboration_state_segments WHERE id=?",
            (monitor_segment["segment_id"],),
        ),
        db.query_one(
            "SELECT * FROM collaboration_state_segments WHERE id=?",
            (llm_segment_id,),
        ),
        db.query_one(
            "SELECT * FROM state_assessment_batches WHERE id=?",
            (batch_id,),
        ),
        db.query_one(
            """
            SELECT * FROM discussion_assessment_cursors
            WHERE group_id=? AND session_id=? AND discussion_id=?
            """,
            (
                scope["group_id"],
                scope["session_id"],
                scope["discussion_id"],
            ),
        ),
        db.query_one(
            "SELECT * FROM intervention_runs WHERE id=?",
            (intervention_run_id,),
        ),
        db.query_one(
            "SELECT * FROM intervention_logs WHERE intervention_run_id=?",
            (intervention_run_id,),
        ),
        db.query_one(
            "SELECT * FROM help_requests WHERE id=?",
            (help_result["help_request_id"],),
        ),
        db.query_one(
            "SELECT * FROM agent_research_events WHERE id=?",
            (event_id,),
        ),
        db.query_one(
            "SELECT * FROM collaboration_state_finalizations WHERE id=?",
            (finalization["finalization_id"],),
        ),
    ]
    assert all(row is not None for row in rows)
    assert {_scope_tuple(row) for row in rows} == {expected}


def test_same_session_number_and_task_do_not_cross_or_share_cooldown(
    canonical_scope,
):
    db, first = canonical_scope
    now = db.now_str()
    first_message = db.create_message(
        first["group_id"],
        first["student_id"],
        "first session",
        role="student",
        session_id=first["session_id"],
        discussion_id=first["discussion_id"],
    )

    db.execute("DROP INDEX IF EXISTS idx_experiment_sessions_session_no_unique")
    second_session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            first["session_no"],
            "discussion",
            first["task_id"],
            "ended",
            now,
            20,
            now,
            now,
        ),
    )
    second_discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'closed',?,?,?)
        """,
        (second_session_id, first["group_id"], now, now, now),
    )
    second_message = db.create_message(
        first["group_id"],
        first["student_id"],
        "second session reusing the same task",
        role="student",
        session_id=second_session_id,
        session_no=first["session_no"],
        task_id=first["task_id"],
        discussion_id=second_discussion_id,
    )

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )

    first_segment = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=first["group_id"],
        session_id=first["session_id"],
        discussion_id=first["discussion_id"],
        state_code="positive_collaboration",
        start_message_id=first_message["sequence"],
        end_message_id=first_message["sequence"],
        evidence_message_ids=[first_message["sequence"]],
        confidence=0.8,
    )
    second_segment = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=first["group_id"],
        session_id=second_session_id,
        discussion_id=second_discussion_id,
        state_code="positive_collaboration",
        start_message_id=second_message["sequence"],
        end_message_id=second_message["sequence"],
        evidence_message_ids=[second_message["sequence"]],
        confidence=0.8,
    )
    assert first_segment["segment_id"] != second_segment["segment_id"]

    from services.teacher_emotion_trend_service import (
        _student_messages_by_sequence,
    )

    first_rows = _student_messages_by_sequence(
        first["group_id"],
        first["session_id"],
        first,
        "1900-01-01 00:00:00",
        "2999-12-31 23:59:59",
    )
    second_scope = {
        **first,
        "session_id": second_session_id,
        "discussion_id": second_discussion_id,
    }
    second_rows = _student_messages_by_sequence(
        first["group_id"],
        second_session_id,
        second_scope,
        "1900-01-01 00:00:00",
        "2999-12-31 23:59:59",
    )
    assert set(first_rows) == {first_message["sequence"]}
    assert set(second_rows) == {second_message["sequence"]}

    monitor_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, analyzer_version,
            status, session_id, session_no, task_id, discussion_id, created_at
        ) VALUES(?,?,'new_message','scope-test','completed',?,?,?,?,?)
        """,
        (
            first["group_id"],
            first_message["sequence"],
            first["session_id"],
            first["session_no"],
            first["task_id"],
            first["discussion_id"],
            now,
        ),
    )
    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, monitor_run_id, cutoff_sequence, status, agent_type,
            session_id, session_no, task_id, discussion_id, created_at
        ) VALUES(?,?,?,'PUBLISHED','strategy',?,?,?,?,?)
        """,
        (
            first["group_id"],
            monitor_id,
            first_message["sequence"],
            first["session_id"],
            first["session_no"],
            first["task_id"],
            first["discussion_id"],
            now,
        ),
    )

    from services.intervention_pipeline_v2.intervention_validator import (
        InterventionValidator,
    )

    assert InterventionValidator._check_cooldown(
        first["group_id"],
        {"session_id": first["session_id"]},
    )["ok"] is False
    assert InterventionValidator._check_cooldown(
        first["group_id"],
        {"session_id": second_session_id},
    )["ok"] is True


def test_confirmation_and_help_guards_use_canonical_session_scope(
    canonical_scope,
    monkeypatch,
):
    db, first = canonical_scope
    second = _create_reused_session_scope(db, first)

    import services.state_assessment_service as assessment_service

    monkeypatch.setattr(assessment_service, "STATE_CONFIRM_WINDOWS", 2)

    def persist(scope):
        return assessment_service.persist_state_assessment(
            {
                "group_id": scope["group_id"],
                "session_id": scope["session_id"],
                "session_no": scope["session_no"],
                "task_id": scope["task_id"],
                "discussion_id": scope["discussion_id"],
                "state_code": "conflict_tension",
                "state_score": 0.9,
                "rule_assessment": {"assessment_status": "state_detected"},
                "context_json": {"discussion_id": scope["discussion_id"]},
                "feature_json": {},
            }
        )

    first_assessment = persist(first)
    second_assessment = persist(second)
    second_confirmation = persist(second)

    assert first_assessment["confirmation"]["confirmed_windows"] == 1
    assert second_assessment["confirmation"]["confirmed_windows"] == 1
    assert second_confirmation["confirmation"]["confirmed_windows"] == 2
    assert second_confirmation["confirmation"]["confirmed"] is True

    first_message = db.create_message(
        first["group_id"],
        first["student_id"],
        "first scoped help",
        role="student",
        session_id=first["session_id"],
        discussion_id=first["discussion_id"],
    )
    second_message = db.create_message(
        second["group_id"],
        second["student_id"],
        "second scoped context",
        role="student",
        session_id=second["session_id"],
        discussion_id=second["discussion_id"],
    )
    db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            discussion_id, status, request_text, source_message_id, created_at
        ) VALUES(?,?,?,?,?,?,'COMPLETED',?,?,?)
        """,
        (
            first["group_id"],
            first["student_id"],
            first["task_id"],
            first["session_no"],
            first["session_id"],
            first["discussion_id"],
            "help in first session",
            first_message["id"],
            db.now_str(),
        ),
    )

    from services.student_help_service import (
        _recent_student_context,
        _student_help_rate_limit,
    )

    assert _student_help_rate_limit(
        first["group_id"],
        session_id=first["session_id"],
        discussion_id=first["discussion_id"],
    )["allowed"] is False
    assert _student_help_rate_limit(
        second["group_id"],
        session_id=second["session_id"],
        discussion_id=second["discussion_id"],
    )["allowed"] is True
    second_context = _recent_student_context(
        second["group_id"],
        session_id=second["session_id"],
        discussion_id=second["discussion_id"],
    )
    assert [row["id"] for row in second_context] == [second_message["id"]]


def test_same_session_legacy_null_discussion_does_not_enter_batch_window(
    canonical_scope,
    monkeypatch,
):
    db, first = canonical_scope
    first_message = db.create_message(
        first["group_id"],
        first["student_id"],
        "active discussion message",
        role="student",
        session_id=first["session_id"],
        discussion_id=first["discussion_id"],
    )
    legacy_sequence = first_message["sequence"] + 1
    legacy_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, role, sender_type, sequence,
            session_id, session_no, task_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            first["group_id"],
            first["student_id"],
            "legacy message without discussion scope",
            "student",
            "student",
            legacy_sequence,
            first["session_id"],
            first["session_no"],
            first["task_id"],
            None,
            db.now_str(),
        ),
    )

    import services.state_assessment_scheduler as scheduler

    monkeypatch.setattr(scheduler, "_enqueue_batch", lambda *_args, **_kwargs: None)
    result = scheduler.request_state_assessment(
        group_id=first["group_id"],
        session_id=first["session_id"],
        discussion_id=first["discussion_id"],
        trigger_type="rule_high_risk",
        continuation=True,
    )
    assert result["created"] is True
    assert result["candidate_start_sequence"] == first_message["sequence"]
    assert result["candidate_end_sequence"] == first_message["sequence"]
    batch = db.query_one(
        "SELECT * FROM state_assessment_batches WHERE id=?",
        (result["assessment_batch_id"],),
    )
    assert batch["discussion_id"] == first["discussion_id"]
    assert batch["student_sequences_json"] == f"[{first_message['sequence']}]"

    resolved = scheduler.resolve_message_scope(
        group_id=first["group_id"],
        sequence=legacy_sequence,
    )
    assert resolved is None
    assert db.query_one(
        "SELECT discussion_id FROM messages WHERE id=?",
        (legacy_message_id,),
    )["discussion_id"] is None


def test_legacy_null_scope_requires_explicit_opt_in(canonical_scope):
    db, scope = canonical_scope
    canonical = db.create_message(
        scope["group_id"],
        scope["student_id"],
        "canonical",
        role="student",
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    legacy_sequence = canonical["sequence"] + 1
    legacy_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, role, sender_type, sequence,
            session_id, session_no, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["student_id"],
            "legacy",
            "student",
            "student",
            legacy_sequence,
            None,
            scope["session_no"],
            scope["task_id"],
            db.now_str(),
        ),
    )

    from services.teacher_emotion_trend_service import (
        _student_messages_by_sequence,
        get_emotion_trend,
    )

    strict = _student_messages_by_sequence(
        scope["group_id"],
        scope["session_id"],
        scope,
        "1900-01-01 00:00:00",
        "2999-12-31 23:59:59",
    )
    compatible = _student_messages_by_sequence(
        scope["group_id"],
        scope["session_id"],
        scope,
        "1900-01-01 00:00:00",
        "2999-12-31 23:59:59",
        include_legacy_scope=True,
    )
    assert set(strict) == {canonical["sequence"]}
    assert set(compatible) == {canonical["sequence"], legacy_sequence}

    strict_response = get_emotion_trend(
        scope["group_id"],
        session_id=scope["session_id"],
        start_time="1900-01-01 00:00:00",
        end_time="2999-12-31 23:59:59",
        window_minutes=0,
    )
    legacy_response = get_emotion_trend(
        scope["group_id"],
        session_id=scope["session_id"],
        start_time="1900-01-01 00:00:00",
        end_time="2999-12-31 23:59:59",
        window_minutes=0,
        include_legacy_scope=True,
    )
    assert strict_response["legacy_scope_fallback"] is False
    assert legacy_response["legacy_scope_fallback"] is True
    assert legacy_response["fallback_reason"] == "explicit_legacy_scope_enabled"

    from services.discussion_scope import (
        legacy_scope_metadata,
        resolve_discussion_scope,
    )

    conn = db.db()
    try:
        legacy_row = conn.execute(
            "SELECT * FROM messages WHERE id=?",
            (legacy_id,),
        ).fetchone()
        metadata = legacy_scope_metadata(legacy_row)
        strict_scope = resolve_discussion_scope(
            conn,
            group_id=scope["group_id"],
            message_id=legacy_id,
        )
        compatible_scope = resolve_discussion_scope(
            conn,
            group_id=scope["group_id"],
            message_id=legacy_id,
            allow_legacy_fallback=True,
        )
    finally:
        conn.close()
    assert metadata["legacy_scope_fallback"] is True
    assert strict_scope.is_legacy_fallback is True
    assert strict_scope.session_id is None
    assert compatible_scope.is_legacy_fallback is True
    assert compatible_scope.session_id == scope["session_id"]
    assert compatible_scope.fallback_reason.startswith(
        "legacy_message_missing_scope:"
    )
