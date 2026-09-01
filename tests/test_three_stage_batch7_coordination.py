# -*- coding: utf-8 -*-
"""Batch 7 coverage for strategy priority and legacy cross-agent isolation."""

from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta

import pytest

from tests.helpers import create_group, create_student, seed_running_session
from tests.test_three_stage_batch6_decision_gate import _add_message, _publish, _ready_pipeline


def _time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def batch7_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)

    scope = seed_running_session(db, session_no=9707, member_count=1, limit_minutes=60)
    db.execute(
        """
        UPDATE experiment_sessions
        SET agent_mode='strategy',
            strategy_agent_enabled=1,
            agent_intervention_enabled=1,
            emotion_agent_enabled=0
        WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1 WHERE id=?",
        (scope["group_id"],),
    )
    now = datetime.now().replace(microsecond=0)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, deadline,
            expected_student_count, ready_student_count,
            created_at, updated_at
        ) VALUES(?,?,'running',?,?,1,1,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            _time(now - timedelta(minutes=10)),
            _time(now + timedelta(minutes=30)),
            _time(now),
            _time(now),
        ),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    return db, scope


def _help_request(db, scope, *, source_message_id=None):
    return db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            discussion_id, status, handling_status, request_text,
            source_message_id, created_at
        ) VALUES(?,?,?,?,?,?,'QUEUED','queued',?,?,?)
        """,
        (
            scope["group_id"],
            scope["student_id"],
            scope["task_id"],
            scope["session_no"],
            scope["session_id"],
            scope["discussion_id"],
            "Need help with the discussion.",
            source_message_id,
            db.now_str(),
        ),
    )


def _emotion_slot(db, scope, *, status="running", slot_index=1, started_at=None):
    now = datetime.now().replace(microsecond=0)
    slot_id = db.execute(
        """
        INSERT INTO emotion_reflection_slots(
            group_id, session_id, discussion_id, slot_index,
            scheduled_at, status, started_at, retry_count, max_attempts,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            slot_index,
            _time(now - timedelta(seconds=30)),
            status,
            started_at or _time(now - timedelta(seconds=5)),
            1 if status == "running" else 0,
            2,
            _time(now),
            _time(now),
        ),
    )
    return dict(db.query_one("SELECT * FROM emotion_reflection_slots WHERE id=?", (slot_id,)))


def _set_pipeline_published(db, pipeline_id, *, published_at=None):
    published_at = published_at or db.now_str()
    db.execute(
        """
        UPDATE strategy_pipeline_runs
        SET publish_status='PUBLISHED',
            final_status='PUBLISHED',
            published_at=?,
            published_message_id=COALESCE(published_message_id, 999999),
            updated_at=?
        WHERE id=?
        """,
        (published_at, published_at, pipeline_id),
    )


def test_student_help_preempts_lower_priority_strategy_and_does_not_touch_other_group(batch7_env):
    db, scope = batch7_env
    low = _ready_pipeline(
        db,
        scope,
        canonical="off_topic_unregulated",
        selected_strategy_id="ER-003",
        candidates=["ER-003"],
        priority=6,
    )
    other_group = create_group(db, name="Batch7 other group", code="B7-OTHER")
    other_student, _key = create_student(db, other_group)
    db.execute("UPDATE groups SET auto_intervention_enabled=1 WHERE id=?", (other_group,))
    other_discussion = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (scope["session_id"], other_group, db.now_str(), db.now_str(), db.now_str()),
    )
    other_scope = {
        **scope,
        "group_id": other_group,
        "student_id": other_student,
        "discussion_id": other_discussion,
    }
    other = _ready_pipeline(db, other_scope, cutoff=5)
    help_id = _help_request(db, scope)

    from agent.help_tasks import _acquire_help_room_lock

    help_token = _acquire_help_room_lock(scope["group_id"], help_id)

    assert help_token
    low_row = db.query_one(
        "SELECT final_status, publish_status, skip_reason, room_lock_released_at FROM strategy_pipeline_runs WHERE id=?",
        (low,),
    )
    assert dict(low_row) == {
        "final_status": "SUPERSEDED",
        "publish_status": "SKIPPED",
        "skip_reason": "SUPERSEDED_BY_STUDENT_HELP",
        "room_lock_released_at": low_row["room_lock_released_at"],
    }
    assert low_row["room_lock_released_at"]
    room = db.query_one("SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?", (scope["group_id"],))
    assert room["state"] == "AI_INTERVENING"
    assert room["lock_token"] == help_token
    assert room["active_intervention_run_id"] == -help_id
    other_row = db.query_one("SELECT final_status FROM strategy_pipeline_runs WHERE id=?", (other,))
    assert other_row["final_status"] == "PENDING_DECISION_GATE"


def test_newer_high_priority_pipeline_supersedes_older_ready_pipeline(batch7_env):
    db, scope = batch7_env
    low = _ready_pipeline(
        db,
        scope,
        canonical="off_topic_unregulated",
        selected_strategy_id="ER-003",
        candidates=["ER-003"],
        cutoff=3,
        priority=6,
    )
    high = _ready_pipeline(
        db,
        scope,
        canonical="interpersonal_conflict",
        selected_strategy_id="ER-001",
        candidates=["ER-001", "SS-004"],
        cutoff=4,
        priority=2,
        lock=False,
    )

    result = _publish(low)

    assert result["published"] is False
    assert result["final_status"] == "SUPERSEDED"
    assert result["superseding_run_id"] == high
    row = db.query_one(
        "SELECT final_status, publish_status, skip_reason, superseded_by_run_id FROM strategy_pipeline_runs WHERE id=?",
        (low,),
    )
    assert dict(row) == {
        "final_status": "SUPERSEDED",
        "publish_status": "SKIPPED",
        "skip_reason": "higher_priority_newer_run",
        "superseded_by_run_id": high,
    }
    room = db.query_one("SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?", (scope["group_id"],))
    assert room["state"] == "OPEN"
    assert room["lock_token"] is None
    assert room["active_intervention_run_id"] is None


def test_legacy_running_emotion_slot_does_not_block_strategy(batch7_env):
    db, scope = batch7_env
    _emotion_slot(db, scope, status="running")
    regular = _ready_pipeline(
        db,
        scope,
        canonical="off_topic_unregulated",
        selected_strategy_id="ER-003",
        candidates=["ER-003"],
        priority=6,
    )

    regular_result = _publish(regular)

    assert regular_result["published"] is True
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent' AND agent_type='strategy'",
        (scope["group_id"],),
    )["c"] == 1


def test_recent_emotion_message_does_not_create_cross_agent_cooldown(batch7_env):
    db, scope = batch7_env
    _add_message(
        db,
        scope,
        4,
        "Emotion reflection",
        role="agent",
        created_at=db.now_str(),
    )
    db.execute("UPDATE messages SET agent_type='emotion' WHERE group_id=? AND sequence=4", (scope["group_id"],))
    regular = _ready_pipeline(
        db,
        scope,
        canonical="off_topic_unregulated",
        selected_strategy_id="ER-003",
        candidates=["ER-003"],
        cutoff=3,
        priority=6,
    )

    regular_result = _publish(regular)

    assert regular_result["published"] is True


def test_emotion_precheck_ignores_legacy_strategy_lock_and_recent_publish(batch7_env):
    db, scope = batch7_env
    _ready_pipeline(db, scope, cutoff=3)
    slot = _emotion_slot(db, scope, status="pending")
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='emotion', strategy_agent_enabled=0,
               emotion_agent_enabled=1, agent_intervention_enabled=0
         WHERE id=?
        """,
        (scope["session_id"],),
    )

    from services.emotion_slot_service import EmotionSlotService

    locked = EmotionSlotService.precheck(slot)
    assert locked["allowed"] is True

    db.execute(
        """
        UPDATE groups
        SET state='OPEN',
            lock_token=NULL,
            lock_expires_at=NULL,
            active_intervention_run_id=NULL
        WHERE id=?
        """,
        (scope["group_id"],),
    )
    published = _ready_pipeline(db, scope, cutoff=4, lock=False)
    _set_pipeline_published(db, published, published_at=db.now_str())
    recent = EmotionSlotService.precheck(slot)
    assert recent["allowed"] is True


def test_close_session_cancels_active_strategy_and_releases_lock(batch7_env):
    db, scope = batch7_env
    pipeline_id = _ready_pipeline(db, scope)

    from services.session_lifecycle import close_session

    close_session(scope["session_id"], reason="manual")

    row = db.query_one(
        "SELECT final_status, publish_status, skip_reason, room_lock_released_at FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["final_status"] == "CANCELLED"
    assert row["publish_status"] == "SKIPPED"
    assert row["skip_reason"] == "CANCELLED_SESSION_ENDED"
    assert row["room_lock_released_at"]
    room = db.query_one("SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?", (scope["group_id"],))
    assert dict(room) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}


def test_coordination_helpers_skip_three_stage_checks_when_strategy_table_missing(tmp_path, monkeypatch):
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        """
        CREATE TABLE groups(
            id INTEGER PRIMARY KEY,
            name TEXT,
            state TEXT,
            lock_token TEXT,
            lock_expires_at TEXT,
            active_intervention_run_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE help_requests(
            id INTEGER PRIMARY KEY,
            group_id INTEGER,
            session_id INTEGER,
            discussion_id INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO groups(id, name, state) VALUES(1, 'legacy', 'OPEN')"
    )
    conn.execute(
        "INSERT INTO help_requests(id, group_id, session_id, discussion_id) VALUES(1, 1, 1, 1)"
    )
    conn.commit()
    conn.close()

    import services.three_stage_coordination as coordination

    def connect_legacy():
        db_conn = sqlite3.connect(legacy_db)
        db_conn.row_factory = sqlite3.Row
        return db_conn

    monkeypatch.setattr(coordination, "db", connect_legacy)

    assert coordination.preempt_for_student_help(1) == []
    assert coordination.cancel_active_runs_for_session_end(1) == []
    assert coordination.emotion_strategy_conflict_check(1, 1, 1)["allowed"] is True
