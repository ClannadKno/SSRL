# -*- coding: utf-8 -*-
"""Batch 2 regression tests for issue-aware student-help coverage."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


AUDIT_FIELDS = {
    "help_request_id",
    "help_status",
    "handled_state_code",
    "handled_segment_id",
    "handled_evidence_range",
    "target_state_code",
    "target_segment_id",
    "target_evidence_range",
    "same_state",
    "same_segment",
    "evidence_overlap",
    "grace_remaining_seconds",
    "guard_evaluated",
    "guard_blocked",
    "reason_code",
}


def _insert_student_message(db, context: dict, sequence: int, content: str) -> int:
    existing = db.query_one(
        "SELECT id FROM messages WHERE group_id=? AND sequence=?",
        (context["group_id"], sequence),
    )
    if existing:
        return int(existing["id"])
    return db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            content,
            sequence,
            "student",
            "student",
            context["session_no"],
            context["task_id"],
            context["session_id"],
            db.now_str(),
        ),
    )


def _insert_run(
    db,
    context: dict,
    *,
    state_code: str,
    cutoff_sequence: int,
    status: str = "PUBLISHED",
) -> int:
    return db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, task_id, cutoff_sequence, status,
            trigger_type, detected_state, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["task_id"],
            cutoff_sequence,
            status,
            "student_help_request",
            state_code,
            db.now_str(),
            db.now_str() if status in {"PUBLISHED", "FALLBACK"} else None,
        ),
    )


def _insert_help(
    db,
    context: dict,
    *,
    status: str,
    request_sequence: int,
    covered_until_sequence: int = None,
    intervention_run_id: int = None,
    handled_state_code: str = None,
    handled_segment_id: int = None,
    handled_start: int = None,
    handled_end: int = None,
    created_at: str = None,
) -> int:
    source_message_id = _insert_student_message(
        db, context, request_sequence, "这里卡住了，请帮忙。"
    )
    return db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, request_text, source_message_id,
            help_request_message_sequence, covered_until_sequence,
            intervention_run_id, handling_status, handled_at,
            handled_state_code, handled_segment_id,
            handled_evidence_start_sequence,
            handled_evidence_end_sequence,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            context["task_id"],
            context["session_no"],
            context["session_id"],
            status,
            "这里卡住了，请帮忙。",
            source_message_id,
            request_sequence,
            covered_until_sequence,
            intervention_run_id,
            "handled" if status.startswith("COMPLETED") else "running",
            db.now_str() if status.startswith("COMPLETED") else None,
            handled_state_code,
            handled_segment_id,
            handled_start,
            handled_end,
            created_at or db.now_str(),
            db.now_str() if status.startswith("COMPLETED") else None,
        ),
    )


def _insert_segment(
    db,
    context: dict,
    *,
    state_code: str,
    start: int,
    end: int,
    source_run_id: int = None,
) -> int:
    _insert_student_message(db, context, start, f"segment start {start}")
    _insert_student_message(db, context, end, f"segment end {end}")
    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )

    result = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=context["group_id"],
        session_id=context["session_id"],
        session_no=context["session_no"],
        task_id=context["task_id"],
        state_code=state_code,
        start_message_id=start,
        end_message_id=end,
        evidence_message_ids=sorted({start, end}),
        confidence=0.9,
        source_run_id=source_run_id,
        analysis_window_start_message_id=start,
        analysis_window_end_message_id=end,
    )
    return int(result["segment_id"])


def test_same_issue_is_blocked_with_complete_audit(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=811, member_count=1)
    segment_id = _insert_segment(
        db,
        context,
        state_code="blocked_frustration",
        start=21,
        end=23,
    )
    help_request_id = _insert_help(
        db,
        context,
        status="COMPLETED",
        request_sequence=21,
        covered_until_sequence=23,
        handled_state_code="blocked_frustration",
        handled_segment_id=segment_id,
        handled_start=21,
        handled_end=23,
    )

    from services.help_request_coverage_service import HelpRequestCoverageService

    guard = HelpRequestCoverageService.evaluate(
        context["group_id"],
        context["session_id"],
        "blocked_frustration",
        segment_id,
        21,
        23,
    )

    assert guard["blocked"] is True
    assert guard["reason_code"] == "same_issue_already_handled"
    assert guard["help_request_id"] == help_request_id
    assert guard["same_state"] is True
    assert guard["same_segment"] is True
    assert guard["evidence_overlap"] is True
    assert guard["guard_evaluated"] is True
    assert guard["guard_blocked"] is True
    assert AUDIT_FIELDS <= guard.keys()


def test_completed_help_allows_new_state_in_all_three_guard_entries(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=812, member_count=1)
    run_id = _insert_run(
        db,
        context,
        state_code="blocked_frustration",
        cutoff_sequence=21,
    )
    _insert_help(
        db,
        context,
        status="COMPLETED",
        request_sequence=21,
        covered_until_sequence=23,
        intervention_run_id=run_id,
    )
    db.execute(
        "UPDATE groups SET last_message_sequence=25, cutoff_sequence=25 WHERE id=?",
        (context["group_id"],),
    )

    from services.assessment_batch_intervention_service import _help_guard
    from services.discussion_pipeline_v2.monitoring_service import (
        _help_request_blocks_strategy,
    )
    from services.intervention_pipeline_v2.intervention_validator import (
        InterventionValidator,
    )

    candidate = {
        "batch": {
            "group_id": context["group_id"],
            "session_id": context["session_id"],
        },
        "segment": {
            "id": 99001,
            "state_code": "conflict_tension",
            "start_sequence": 21,
            "end_sequence": 25,
            "evidence_sequences": [25],
        },
    }
    conn = db.db()
    try:
        batch_guard = _help_guard(conn, candidate)
    finally:
        conn.close()
    monitor_guard = _help_request_blocks_strategy(
        context["group_id"],
        trigger_type="new_message",
        cutoff_sequence=25,
        window_start_sequence=21,
        scope={"session_id": context["session_id"]},
        target_state_code="conflict_tension",
        target_segment_id=99001,
        target_end_sequence=25,
    )
    validator_guard = InterventionValidator._check_pending_help_requests(
        context["group_id"],
        {
            "session_id": context["session_id"],
            "final_state": "conflict_tension",
            "target_segment_id": 99001,
            "target_start_sequence": 21,
            "target_end_sequence": 25,
            "cutoff_sequence": 25,
        },
    )

    for guard in (batch_guard, monitor_guard, validator_guard):
        assert guard["blocked"] is False
        assert guard["reason_code"] == "different_state_new_issue"
        assert guard["handled_state_code"] == "blocked_frustration"
        assert guard["target_state_code"] == "conflict_tension"
        assert guard["guard_evaluated"] is True
        assert guard["guard_blocked"] is False
    assert batch_guard["allowed"] is True
    assert validator_guard["ok"] is True


def test_same_state_new_non_overlapping_event_is_allowed(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=813, member_count=1)
    _insert_help(
        db,
        context,
        status="COMPLETED",
        request_sequence=21,
        covered_until_sequence=23,
        handled_state_code="blocked_frustration",
        handled_start=21,
        handled_end=23,
    )

    from services.help_request_coverage_service import HelpRequestCoverageService

    guard = HelpRequestCoverageService.evaluate(
        context["group_id"],
        context["session_id"],
        "blocked_frustration",
        None,
        30,
        32,
    )

    assert guard["blocked"] is False
    assert guard["reason_code"] == "same_state_new_issue"
    assert guard["same_state"] is True
    assert guard["evidence_overlap"] is False


def test_active_help_uses_short_grace_but_not_permanent_cross_state_block(
    db_and_app,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=814, member_count=1)
    run_id = _insert_run(
        db,
        context,
        state_code="blocked_frustration",
        cutoff_sequence=21,
        status="RUNNING",
    )
    help_request_id = _insert_help(
        db,
        context,
        status="RUNNING",
        request_sequence=21,
        intervention_run_id=run_id,
        created_at=db.now_str(),
    )

    from services.help_request_coverage_service import HelpRequestCoverageService

    now = datetime.now().replace(microsecond=0)
    during_grace = HelpRequestCoverageService.evaluate(
        context["group_id"],
        context["session_id"],
        "conflict_tension",
        None,
        25,
        25,
        now,
    )
    assert during_grace["blocked"] is True
    assert during_grace["reason_code"] == "help_request_race_grace"
    assert during_grace["grace_remaining_seconds"] > 0

    db.execute(
        "UPDATE help_requests SET created_at=? WHERE id=?",
        (
            (now - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S"),
            help_request_id,
        ),
    )
    after_grace = HelpRequestCoverageService.evaluate(
        context["group_id"],
        context["session_id"],
        "conflict_tension",
        None,
        25,
        25,
        now,
    )
    assert after_grace["blocked"] is False
    assert after_grace["reason_code"] == "different_state_new_issue"
    assert after_grace["grace_remaining_seconds"] == 0


def test_help_publish_persists_handled_issue_snapshot(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=815, member_count=1)
    source_message_id = _insert_student_message(
        db, context, 21, "这一步一直推不出来，请帮帮我们。"
    )
    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, session_id, task_id, cutoff_sequence, trigger_type,
            final_state, status, context_from_sequence, context_to_sequence,
            evidence_sequences_json, created_at, completed_at
        ) VALUES(?,?,?,?,?,'blocked_frustration','completed',21,23,'[21,22,23]',?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["task_id"],
            23,
            "new_message",
            db.now_str(),
            db.now_str(),
        ),
    )
    segment_id = _insert_segment(
        db,
        context,
        state_code="blocked_frustration",
        start=21,
        end=23,
        source_run_id=monitor_run_id,
    )
    help_request_id = db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, handling_status, request_text, source_message_id,
            help_request_message_sequence, created_at
        ) VALUES(?,?,?,?,?,'RUNNING','running',?,?,21,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            context["task_id"],
            context["session_no"],
            context["session_id"],
            "这一步一直推不出来，请帮帮我们。",
            source_message_id,
            db.now_str(),
        ),
    )

    from services.agent_intervention_publisher import publish_agent_intervention

    result = publish_agent_intervention(
        group_id=context["group_id"],
        message="可以先把卡住的条件说清楚，再一起核对已有依据。",
        trigger_source="student_help_request",
        agent_type="strategy",
        help_request_id=help_request_id,
        source_student_message_id=source_message_id,
        monitor_run_id=monitor_run_id,
        session_id=context["session_id"],
        task_id=context["task_id"],
        session_no=context["session_no"],
        detected_state="blocked_frustration",
        confidence=0.9,
        evidence_sequences=[21, 22, 23],
    )

    assert result["ok"] is True
    saved = db.query_one("SELECT * FROM help_requests WHERE id=?", (help_request_id,))
    assert saved["handled_state_code"] == "blocked_frustration"
    assert saved["handled_segment_id"] == segment_id
    assert saved["handled_evidence_start_sequence"] == 21
    assert saved["handled_evidence_end_sequence"] == 23
    assert saved["handling_status"] == "handled"
    assert saved["handled_at"] is not None

    columns = {
        row["name"] for row in db.query_all("PRAGMA table_info(help_requests)")
    }
    assert {
        "handled_state_code",
        "handled_segment_id",
        "handled_evidence_start_sequence",
        "handled_evidence_end_sequence",
    } <= columns
