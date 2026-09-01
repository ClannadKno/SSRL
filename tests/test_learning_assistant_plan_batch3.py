# -*- coding: utf-8 -*-
"""Batch 3 regressions for the negative-silence segment lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


def _ts(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _start_discussion(db, context: dict, now: datetime) -> None:
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    runtime = enter_group_discussion_stage(
        context["session_id"],
        context["group_id"],
        context["students"][0][0],
    )
    db.execute(
        """
        UPDATE group_session_discussions
        SET status='running', started_at=?, deadline=?, updated_at=?
        WHERE id=?
        """,
        (
            _ts(now - timedelta(minutes=10)),
            _ts(now + timedelta(minutes=10)),
            _ts(now),
            runtime["id"],
        ),
    )


def _student_message(
    db,
    context: dict,
    *,
    sequence: int,
    created_at: datetime,
    content: str = "我们先想一想。",
) -> int:
    message_id = db.execute(
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
            _ts(created_at),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=MAX(COALESCE(last_message_sequence, 0), ?)
        WHERE id=?
        """,
        (sequence, context["group_id"]),
    )
    return message_id


def _agent_message(
    db,
    context: dict,
    *,
    sequence: int,
    created_at: datetime,
) -> int:
    message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, agent_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            "给小组的支持消息。",
            sequence,
            "agent",
            "agent",
            context["session_no"],
            context["task_id"],
            context["session_id"],
            "emotion",
            _ts(created_at),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=MAX(COALESCE(last_message_sequence, 0), ?)
        WHERE id=?
        """,
        (sequence, context["group_id"]),
    )
    return message_id


def test_silence_task_payload_captures_student_scope(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=901, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    _student_message(
        db,
        context,
        sequence=7,
        created_at=now,
    )

    import services.discussion_pipeline_v2.monitoring_service as monitoring
    from agent.monitoring_tasks import check_room_silence

    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    scheduled = []
    monkeypatch.setattr(
        check_room_silence,
        "schedule",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    monitoring.MonitoringService.schedule_silence_check(
        context["group_id"],
        7,
    )

    assert len(scheduled) == 1
    assert scheduled[0][1]["args"] == (
        context["group_id"],
        7,
        _ts(now),
        context["session_id"],
        context["task_id"],
    )


def test_stale_silence_task_exits_without_segment(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=902, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    first_at = now - timedelta(seconds=200)
    _student_message(db, context, sequence=1, created_at=first_at)
    _student_message(db, context, sequence=2, created_at=now - timedelta(seconds=1))

    from agent.monitoring_tasks import check_room_silence

    result = check_room_silence.call_local(
        context["group_id"],
        1,
        _ts(first_at),
        context["session_id"],
        context["task_id"],
    )

    assert result["skipped"] is True
    assert result["reason"] == "stale_silence_task"
    assert result["actual_sequence"] == 2
    count = db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM collaboration_state_segments
        WHERE group_id=? AND state_code='negative_silence'
        """,
        (context["group_id"],),
    )["c"]
    assert count == 0


def test_stale_retry_preserves_existing_completed_monitor_run(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=906, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    first_at = now - timedelta(seconds=200)
    _student_message(db, context, sequence=1, created_at=first_at)

    from services.discussion_pipeline_v2.monitor_run_repo import MonitorRunRepo

    run_id = MonitorRunRepo.create(
        context["group_id"],
        1,
        trigger_type="silence_check",
    )
    MonitorRunRepo.claim(run_id)
    MonitorRunRepo.complete(
        run_id,
        final_state="negative_silence",
        confidence=0.9,
    )
    _student_message(db, context, sequence=2, created_at=now - timedelta(seconds=1))

    from agent.monitoring_tasks import check_room_silence

    result = check_room_silence.call_local(
        context["group_id"],
        1,
        _ts(first_at),
        context["session_id"],
        context["task_id"],
    )

    assert result["reason"] == "stale_silence_task"
    assert result["monitor_run_id"] == run_id
    row = db.query_one(
        "SELECT status, rule_result_json FROM monitor_runs WHERE id=?",
        (run_id,),
    )
    assert row["status"] == "completed"
    assert '"reason": "stale_silence_task"' in row["rule_result_json"]


def test_threshold_creation_and_continuous_update_are_idempotent(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=903, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    message_at = now - timedelta(seconds=180)
    _student_message(db, context, sequence=11, created_at=message_at)

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )

    below = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=context["group_id"],
        expected_sequence=11,
        expected_last_student_message_at=_ts(message_at),
        expected_session_id=context["session_id"],
        expected_task_id=context["task_id"],
        now_value=_ts(now - timedelta(seconds=1)),
    )
    first = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=context["group_id"],
        expected_sequence=11,
        expected_last_student_message_at=_ts(message_at),
        expected_session_id=context["session_id"],
        expected_task_id=context["task_id"],
        source_run_id=301,
        assessment_id=401,
        now_value=_ts(now),
    )
    continued = (
        CollaborationStateSegmentService.record_negative_silence_if_applicable(
            group_id=context["group_id"],
            expected_sequence=11,
            expected_last_student_message_at=_ts(message_at),
            expected_session_id=context["session_id"],
            expected_task_id=context["task_id"],
            source_run_id=302,
            assessment_id=402,
            now_value=_ts(now + timedelta(seconds=35)),
        )
    )
    delayed_older_retry = (
        CollaborationStateSegmentService.record_negative_silence_if_applicable(
            group_id=context["group_id"],
            expected_sequence=11,
            expected_last_student_message_at=_ts(message_at),
            expected_session_id=context["session_id"],
            expected_task_id=context["task_id"],
            source_run_id=303,
            assessment_id=403,
            now_value=_ts(now + timedelta(seconds=10)),
        )
    )

    assert below == {
        "skipped": True,
        "reason": "below_silence_threshold",
        "silent_seconds": 179,
    }
    assert first["segment_id"] == continued["segment_id"]
    assert delayed_older_retry["segment_id"] == first["segment_id"]
    assert delayed_older_retry["last_observed_at"] == _ts(
        now + timedelta(seconds=35)
    )
    assert delayed_older_retry["gap_seconds"] == 35
    rows = db.query_all(
        """
        SELECT * FROM collaboration_state_segments
        WHERE group_id=? AND state_code='negative_silence'
        """,
        (context["group_id"],),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["trigger_sequence"] == 11
    assert row["raw_silence_started_at"] == _ts(message_at)
    assert row["threshold_reached_at"] == _ts(now)
    assert row["start_at"] == _ts(now)
    assert row["detected_at"] == _ts(now)
    assert row["last_observed_at"] == _ts(now + timedelta(seconds=35))
    assert row["silent_seconds_at_detection"] == 180
    assert row["gap_seconds"] == 35
    assert row["end_at"] is None
    assert row["is_active"] == 1
    assert row["is_finalized"] == 0


def test_only_student_message_closes_open_silence(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=904, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    message_at = now - timedelta(seconds=185)
    _student_message(db, context, sequence=21, created_at=message_at)

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )

    CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=context["group_id"],
        expected_sequence=21,
        now_value=_ts(now),
    )
    agent_id = _agent_message(
        db,
        context,
        sequence=22,
        created_at=now + timedelta(seconds=4),
    )
    agent_result = (
        CollaborationStateSegmentService.close_open_silence_on_student_message(
            message_id=agent_id
        )
    )
    student_at = now + timedelta(seconds=12)
    student_id = _student_message(
        db,
        context,
        sequence=23,
        created_at=student_at,
        content="我有一个新想法。",
    )
    close_result = (
        CollaborationStateSegmentService.close_open_silence_on_student_message(
            message_id=student_id
        )
    )

    assert agent_result == {"skipped": True, "reason": "message_not_student"}
    assert close_result["closed"] is True
    row = db.query_one(
        """
        SELECT * FROM collaboration_state_segments
        WHERE group_id=? AND state_code='negative_silence'
        """,
        (context["group_id"],),
    )
    assert row["end_at"] == _ts(student_at)
    assert row["end_sequence"] == 23
    assert row["next_student_message_id"] == 23
    assert row["is_active"] == 0
    assert row["is_finalized"] == 1
    assert row["resolution_reason"] == "student_message_resumed"
    assert row["gap_seconds"] == 17


def test_teacher_statistics_read_open_silence_segment(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=905, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    message_at = now - timedelta(seconds=185)
    _student_message(db, context, sequence=31, created_at=message_at)

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )
    from services.teacher_emotion_review_service import get_emotion_review
    from services.teacher_emotion_trend_service import get_emotion_trend

    CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=context["group_id"],
        expected_sequence=31,
        now_value=_ts(now),
    )
    query_range = {
        "session_id": context["session_id"],
        "start_time": _ts(now - timedelta(minutes=1)),
        "end_time": _ts(now + timedelta(minutes=1)),
    }
    trend = get_emotion_trend(
        context["group_id"],
        **query_range,
    )
    review = get_emotion_review(
        context["group_id"],
        **query_range,
    )

    assert len(trend["silence_segments"]) == 1
    silence = trend["silence_segments"][0]
    assert silence["start_at"] == _ts(now - timedelta(seconds=5))
    assert silence["end_at"] is None
    assert silence["last_observed_at"] == _ts(now)
    assert silence["is_active"] is True
    assert silence["duration_seconds"] == 5
    assert "negative_silence" not in trend["distribution"]
    assert trend["active_silence"]["active"] is True
    assert trend["active_silence"]["duration_seconds"] == 5
    assert review["summary"]["silence_segment_count"] == 1
    assert review["silence_segments"][0]["trigger_sequence"] == 31
