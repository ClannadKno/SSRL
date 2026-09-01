# -*- coding: utf-8 -*-

import json
from datetime import timedelta

from tests.helpers import create_group, create_student
from tests.test_emotion_slot_scheduler_batch6 import (
    INTERVAL,
    _add_student_message,
    _fake_gateway,
    _ready_discussion,
    _time,
)


def _scoped_message(
    db,
    seeded,
    scope,
    *,
    created_at,
    content,
    user_id=None,
    role="student",
    session_id=None,
    discussion_id=None,
):
    return db.create_message(
        seeded["group_id"],
        user_id or seeded["students"][0][0],
        content,
        role=role,
        created_at=_time(created_at),
        session_id=(
            seeded["session_id"] if session_id is None else session_id
        ),
        discussion_id=(
            scope["discussion_id"] if discussion_id is None else discussion_id
        ),
    )


def test_slot_freezes_equal_half_open_windows_and_strict_student_scope(db_and_app):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=121)
    started = db.parse_dt(scope["all_members_entered_at"])
    previous_start = started - timedelta(seconds=INTERVAL)
    current_end = started + timedelta(seconds=INTERVAL)

    previous_first = _scoped_message(
        db,
        seeded,
        scope,
        created_at=previous_start,
        content="先梳理评价依据",
    )
    previous_last = _scoped_message(
        db,
        seeded,
        scope,
        created_at=started - timedelta(seconds=1),
        content="再比较两个方案",
        user_id=seeded["students"][1][0],
    )
    current_ack = _scoped_message(
        db,
        seeded,
        scope,
        created_at=started,
        content="好",
    )
    current_effective = _scoped_message(
        db,
        seeded,
        scope,
        created_at=current_end - timedelta(seconds=1),
        content="我建议把证据按支持和反对分成两列",
        user_id=seeded["students"][1][0],
    )
    boundary_for_next = _scoped_message(
        db,
        seeded,
        scope,
        created_at=current_end,
        content="这条应进入下一时间槽",
    )

    other_group_id = create_group(db, name="窗口隔离组")
    other_student_id, _key = create_student(db, other_group_id, index=1)
    db.create_message(
        other_group_id,
        other_student_id,
        "其他组消息",
        role="student",
        created_at=_time(current_end - timedelta(seconds=2)),
        session_id=seeded["session_id"],
        discussion_id=scope["discussion_id"],
    )
    wrong_session = _scoped_message(
        db,
        seeded,
        scope,
        created_at=current_end - timedelta(seconds=2),
        content="其他课次消息",
    )
    db.execute(
        "UPDATE messages SET session_id=? WHERE id=?",
        (seeded["session_id"] + 9999, wrong_session["id"]),
    )
    agent_message = _scoped_message(
        db,
        seeded,
        scope,
        created_at=current_end - timedelta(seconds=2),
        content="Agent 消息",
        role="agent",
    )

    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    slot = db.query_one(
        "SELECT * FROM emotion_reflection_slots WHERE id=?",
        (result["slot"]["id"],),
    )
    previous_ids = json.loads(slot["previous_message_ids_json"])
    current_ids = json.loads(slot["current_message_ids_json"])
    previous_metrics = json.loads(slot["previous_metrics_json"])
    current_metrics = json.loads(slot["current_metrics_json"])

    previous_window_seconds = int(
        (
            db.parse_dt(slot["previous_window_end"])
            - db.parse_dt(slot["previous_window_start"])
        ).total_seconds()
    )
    current_window_seconds = int(
        (
            db.parse_dt(slot["current_window_end"])
            - db.parse_dt(slot["current_window_start"])
        ).total_seconds()
    )
    assert previous_window_seconds == current_window_seconds == INTERVAL
    assert slot["previous_window_end"] == slot["current_window_start"]
    assert slot["current_window_end"] == slot["scheduled_at"]
    assert previous_ids == [previous_first["id"], previous_last["id"]]
    assert current_ids == [current_ack["id"], current_effective["id"]]
    assert boundary_for_next["id"] not in current_ids
    assert wrong_session["id"] not in current_ids
    assert agent_message["id"] not in current_ids
    assert previous_metrics["message_count"] == 2
    assert current_metrics["message_count"] == 2
    assert current_metrics["effective_message_count"] == 1
    assert current_metrics["short_acknowledgement_count"] == 1


def test_frozen_slot_is_idempotent_and_new_messages_are_left_for_next_slot(db_and_app):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=122)
    from services.emotion_slot_service import EmotionSlotService

    first = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    first_slot = first["slot"]
    frozen_ids = json.loads(first_slot["current_message_ids_json"])
    late_message = _scoped_message(
        db,
        seeded,
        scope,
        created_at=now,
        content="冻结后出现的新消息",
    )

    repeated = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    repeated_slot = db.query_one(
        "SELECT * FROM emotion_reflection_slots WHERE id=?", (first_slot["id"],)
    )
    assert repeated["reason"] == "slot_already_recorded"
    assert json.loads(repeated_slot["current_message_ids_json"]) == frozen_ids
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM emotion_feedback_assessments WHERE slot_id=?",
        (first_slot["id"],),
    )["c"] == 1

    second_now = now + timedelta(seconds=INTERVAL)
    second = EmotionSlotService.ensure_latest_due_slot(scope, now=second_now)
    assert second["slot_index"] == 2
    assert late_message["id"] in json.loads(second["slot"]["current_message_ids_json"])
    assert db.query_one(
        """
        SELECT COUNT(*) AS c FROM emotion_reflection_slots
        WHERE group_id=? AND session_id=? AND discussion_id=? AND slot_index=1
        """,
        (seeded["group_id"], seeded["session_id"], scope["discussion_id"]),
    )["c"] == 1


def test_emotion_assessment_and_generation_ledgers_are_idempotent(db_and_app):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=123)
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    assessment = db.query_one(
        "SELECT * FROM emotion_feedback_assessments WHERE slot_id=?",
        (result["slot"]["id"],),
    )
    assert assessment["status"] == "prepared"
    assert assessment["prompt_version"] == EmotionSlotService.prompt_version
    assert json.loads(assessment["input_message_ids_json"]) == json.loads(
        result["slot"]["input_message_ids_json"]
    )
    assert db.query_one(
        """
        SELECT COUNT(*) AS c FROM sqlite_master
        WHERE type='table' AND name='emotion_feedback_generations'
        """
    )["c"] == 1


def test_emotion_execution_does_not_take_room_lock_or_return_423(
    db_and_app, monkeypatch
):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=124)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "大家继续保持现在的交流节奏。"},
    )
    from services.emotion_slot_service import EmotionSlotService

    reserved = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    result = EmotionSlotService.execute_slot(reserved["enqueue_slot_id"], now=now)
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (seeded["group_id"],),
    )
    assert result["status"] == "sent"
    assert result.get("status_code") != 423
    assert dict(group) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }
