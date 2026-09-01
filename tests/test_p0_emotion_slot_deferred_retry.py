# -*- coding: utf-8 -*-
"""Emotion-slot retry, fixed-cadence deferral and isolation regressions."""

from datetime import datetime, timedelta
import sqlite3

from tests.test_emotion_slot_scheduler_batch6 import (
    _add_student_message,
    _due_slot,
    _fake_gateway,
    _ready_discussion,
)


def _e1_output():
    return {
        "feedback_state": "GROUP_EXCELLENT",
        "confidence": 0.82,
        "comparison_summary": "当前完整时间槽存在正常的群体参与。",
        "current_window_summary": "当前窗口包含任务相关交流。",
        "previous_window_summary": "第一槽不使用上一窗口作比较判断。",
        "evidence_message_ids": [],
        "excluded_alternatives": [
            {
                "state": "GROUP_LOW_PARTICIPATION",
                "reason": "当前已有正常的任务相关交流。",
            }
        ],
    }


def test_new_student_message_during_generation_does_not_mutate_frozen_slot(
    db_and_app, monkeypatch
):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=86)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    from services.llm_gateway import LlmResult
    import services.emotion_agent.emotion_reflection_service as emotion_module

    class StudentRaceGateway:
        inserted = False

        def call(self, profile_name, payload, response_type="json"):
            if profile_name == "emotion_reflection_generator" and not self.inserted:
                self.inserted = True
                message = db.create_message(
                    seeded["group_id"],
                    seeded["students"][0][0],
                    "生成期间出现了新的学生上下文",
                    role="student",
                    created_at=db.now_str(),
                )
                db.execute(
                    "UPDATE messages SET session_id=?, discussion_id=? WHERE id=?",
                    (seeded["session_id"], scope["discussion_id"], message["id"]),
                )
            result_output = (
                _e1_output()
                if profile_name == "emotion_feedback_classifier"
                else {"should_send": True, "message": "大家继续保持当前交流节奏就好 😊"}
            )
            return LlmResult(
                success=True,
                output=result_output,
                raw_text="fixed",
                profile_name=profile_name,
                model_name="student-race-mock",
            )

    monkeypatch.setattr(emotion_module, "get_gateway", lambda: StudentRaceGateway())
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "sent"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'"
    )["c"] == 1


def test_duplicate_worker_delivery_creates_one_emotion_message(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=76)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "大家正认真参与讨论，继续保持就好 😊"},
    )
    from services.emotion_slot_service import EmotionSlotService

    assert EmotionSlotService.execute_slot(slot_id, now=now)["status"] == "sent"
    duplicate = EmotionSlotService.execute_slot(slot_id, now=now)
    assert duplicate["claimed"] is False
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion' AND discussion_id=?",
        (scope["discussion_id"],),
    )["c"] == 1


def test_retry_after_slot_link_failure_reuses_committed_message(
    db_and_app, monkeypatch
):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=85)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "大家的投入很清晰，继续保持轻松节奏 😊"},
    )
    from services.emotion_slot_service import EmotionSlotService

    original_mark_sent = EmotionSlotService.mark_sent.__func__
    calls = {"count": 0}

    def flaky_mark_sent(cls, target_slot_id, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_mark_sent(cls, target_slot_id, **kwargs)

    monkeypatch.setattr(EmotionSlotService, "mark_sent", classmethod(flaky_mark_sent))
    first = EmotionSlotService.execute_slot(slot_id, now=now)
    assert first["status"] == "failed"
    assert first["retryable"] is True
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion' AND discussion_id=?",
        (scope["discussion_id"],),
    )["c"] == 1
    retry_at = datetime.strptime(first["slot"]["next_retry_at"], "%Y-%m-%d %H:%M:%S")
    assert EmotionSlotService.ensure_latest_due_slot(scope, now=retry_at)[
        "enqueue_slot_id"
    ] == slot_id
    sent = EmotionSlotService.execute_slot(slot_id, now=retry_at)
    assert sent["status"] == "sent"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion' AND discussion_id=?",
        (scope["discussion_id"],),
    )["c"] == 1


def test_emotion_publish_never_acquires_student_room_lock(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=79)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "大家正在积极交流，保持自然节奏就好 😊"},
    )
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

    monkeypatch.setattr(
        RoomLeaseService,
        "acquire",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("emotion must not acquire the room lock")
            )
        ),
    )
    from services.emotion_slot_service import EmotionSlotService

    assert EmotionSlotService.execute_slot(slot_id, now=now)["status"] == "sent"


def test_temporary_database_error_is_retryable(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=81)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    from services.emotion_slot_service import EmotionSlotService

    def raise_locked(_cls, _slot, *, now=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(EmotionSlotService, "precheck", classmethod(raise_locked))
    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "failed"
    assert result["reason"] == "temporary_database_error"
    assert result["retryable"] is True
    assert result["slot"]["next_retry_at"] is not None


def test_structural_database_error_is_terminal(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=82)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    from services.emotion_slot_service import EmotionSlotService

    def raise_integrity(_cls, _slot, *, now=None):
        raise sqlite3.IntegrityError("malformed emotion slot relation")

    monkeypatch.setattr(EmotionSlotService, "precheck", classmethod(raise_integrity))
    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "failed"
    assert result["reason"] == "structural_database_error"
    assert result["retryable"] is False
    assert result["slot"]["next_retry_at"] is None


def test_legacy_slot_statuses_remain_schema_compatible(db_and_app):
    db, _app, _client = db_and_app
    table = db.query_one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='emotion_reflection_slots'"
    )
    assert "'deferred'" in table["sql"]
    assert "'suppressed'" in table["sql"]
    index = db.query_one(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_emotion_slots_one_active'"
    )
    assert "'deferred'" in index["sql"]


def test_legacy_slot_status_migration_preserves_rows_and_is_idempotent():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE emotion_reflection_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                discussion_id INTEGER NOT NULL,
                slot_index INTEGER NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','running','sent','skipped','failed')),
                started_at TEXT, completed_at TEXT, message_id INTEGER,
                intervention_run_id INTEGER, skip_reason TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                enqueued_at TEXT, next_retry_at TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX custom_emotion_slot_audit_idx "
            "ON emotion_reflection_slots(group_id, scheduled_at)"
        )
        conn.execute(
            """
            INSERT INTO emotion_reflection_slots(
                id, group_id, session_id, discussion_id, slot_index,
                scheduled_at, status, created_at, updated_at
            ) VALUES(1, 2, 3, 4, 1, '2026-07-29 12:00:00', 'pending',
                     '2026-07-29 12:00:00', '2026-07-29 12:00:00')
            """
        )
        from migrations import _upgrade_emotion_reflection_slot_statuses

        _upgrade_emotion_reflection_slot_statuses(conn)
        _upgrade_emotion_reflection_slot_statuses(conn)
        row = conn.execute(
            "SELECT id, group_id, status, defer_count FROM emotion_reflection_slots"
        ).fetchone()
        assert row == (1, 2, "pending", 0)
        conn.execute("UPDATE emotion_reflection_slots SET status='deferred' WHERE id=1")
        assert conn.execute(
            "SELECT status FROM emotion_reflection_slots WHERE id=1"
        ).fetchone()[0] == "deferred"
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='custom_emotion_slot_audit_idx'"
        ).fetchone()[0] == "custom_emotion_slot_audit_idx"
    finally:
        conn.close()
