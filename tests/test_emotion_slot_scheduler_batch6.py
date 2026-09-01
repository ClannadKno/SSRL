# -*- coding: utf-8 -*-

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import sqlite3

from tests.helpers import create_group, create_student, seed_running_session


INTERVAL = 300


def _time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _ready_discussion(db, *, member_count=4, now=None, session_no=61):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    now = now or datetime.now().replace(microsecond=0)
    seeded = seed_running_session(db, session_no=session_no, member_count=member_count, limit_minutes=60)
    db.execute(
        "UPDATE experiment_sessions SET emotion_agent_enabled=1 WHERE id=?",
        (seeded["session_id"],),
    )
    runtime = None
    for user_id, _key in seeded["students"]:
        runtime = enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], user_id)
    started = now - timedelta(seconds=INTERVAL + 5)
    deadline = now + timedelta(minutes=30)
    db.execute(
        """
        UPDATE group_session_discussions
        SET started_at=?, deadline=?, status='running', updated_at=?
        WHERE id=?
        """,
        (_time(started), _time(deadline), _time(now), runtime["id"]),
    )
    scope = {
        "group_id": seeded["group_id"],
        "session_id": seeded["session_id"],
        "discussion_id": runtime["id"],
        "all_members_entered_at": _time(started),
    }
    return seeded, scope, now


def _add_student_message(db, seeded, *, created_at, content="我们已经整理出共同思路"):
    user_id = seeded["students"][0][0]
    return db.create_message(
        seeded["group_id"], user_id, content, role="student", created_at=_time(created_at)
    )


def _due_slot(db, seeded, scope, now):
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    assert result.get("enqueue_slot_id")
    return result["enqueue_slot_id"]


def _fake_gateway(monkeypatch, output):
    from services.llm_gateway import LlmResult
    import services.emotion_agent.emotion_reflection_service as service_module

    class Gateway:
        def call(self, profile_name, payload, response_type="json"):
            result_output = output
            if profile_name == "emotion_feedback_classifier":
                result_output = {
                    "feedback_state": "GROUP_LOW_PARTICIPATION",
                    "confidence": 0.82,
                    "comparison_summary": "当前完整时间槽的群体有效参与较低。",
                    "current_window_summary": "当前窗口交流较少，需要低压力支持。",
                    "previous_window_summary": "第一槽不使用上一窗口作比较判断。",
                    "evidence_message_ids": [],
                    "excluded_alternatives": [
                        {
                            "state": "GROUP_EXCELLENT",
                            "reason": "当前有效参与尚不足以归入群体优秀。",
                        }
                    ],
                }
            return LlmResult(
                success=True,
                output=result_output,
                raw_text=str(result_output),
                profile_name=profile_name,
                model_name="fixed-emotion-mock",
            )

    monkeypatch.setattr(service_module, "get_gateway", lambda: Gateway())


def test_one_to_three_members_do_not_start_emotion_clock(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=60, member_count=4, limit_minutes=60)
    db.execute("UPDATE experiment_sessions SET emotion_agent_enabled=1 WHERE id=?", (seeded["session_id"],))
    from services.group_discussion_runtime_service import enter_group_discussion_stage
    from services.emotion_slot_service import EmotionSlotService

    for user_id, _key in seeded["students"][:3]:
        runtime = enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], user_id)
    assert runtime["status"] == "waiting"
    assert runtime["started_at"] is None
    result = EmotionSlotService.scan_due(now=datetime.now() + timedelta(hours=1), enqueue=False)
    assert result["scanned"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_reflection_slots")["c"] == 0


def test_fourth_member_sets_origin_once_and_refresh_does_not_reset(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=62, member_count=4)
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    runtime = None
    for user_id, _key in seeded["students"]:
        runtime = enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], user_id)
    first_origin = runtime["started_at"]
    refreshed = enter_group_discussion_stage(
        seeded["session_id"], seeded["group_id"], seeded["students"][0][0]
    )
    assert first_origin
    assert refreshed["started_at"] == first_origin
    assert refreshed["ready_student_count"] == 4


def test_first_slot_is_not_due_until_one_full_interval(db_and_app):
    db, _app, _client = db_and_app
    from services.emotion_slot_service import EmotionSlotService

    origin = datetime.now().replace(microsecond=0)
    assert EmotionSlotService.interval_seconds() == 300
    assert EmotionSlotService.due_slot_index(origin, now=origin + timedelta(seconds=INTERVAL - 1)) == 0
    assert EmotionSlotService.due_slot_index(origin, now=origin + timedelta(seconds=INTERVAL)) == 1


def test_repeated_scan_reserves_same_slot_only_once(db_and_app):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db)
    from services.emotion_slot_service import EmotionSlotService

    first = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    second = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    assert first.get("enqueue_slot_id")
    assert second.get("enqueue_slot_id") is None
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_reflection_slots")["c"] == 1


def test_concurrent_scanners_have_one_database_winner(db_and_app):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db)
    from services.emotion_slot_service import EmotionSlotService

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _i: EmotionSlotService.ensure_latest_due_slot(scope, now=now), range(12)))
    assert sum(1 for item in results if item.get("enqueue_slot_id")) == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_reflection_slots")["c"] == 1


def test_different_groups_in_same_session_get_independent_slots(db_and_app):
    db, _app, _client = db_and_app
    seeded, scope1, now = _ready_discussion(db, member_count=1)
    from services.group_discussion_runtime_service import enter_group_discussion_stage
    from services.emotion_slot_service import EmotionSlotService

    group2 = create_group(db, name="Second group", code="G62B")
    student2, _key = create_student(db, group2, index=1, username_prefix="second")
    runtime2 = enter_group_discussion_stage(seeded["session_id"], group2, student2)
    db.execute(
        "UPDATE group_session_discussions SET started_at=?, deadline=? WHERE id=?",
        (scope1["all_members_entered_at"], _time(now + timedelta(minutes=30)), runtime2["id"]),
    )
    scope2 = {
        "group_id": group2,
        "session_id": seeded["session_id"],
        "discussion_id": runtime2["id"],
        "all_members_entered_at": scope1["all_members_entered_at"],
    }
    assert EmotionSlotService.ensure_latest_due_slot(scope1, now=now).get("enqueue_slot_id")
    assert EmotionSlotService.ensure_latest_due_slot(scope2, now=now).get("enqueue_slot_id")
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_reflection_slots")["c"] == 2


def test_closed_discussion_is_not_scanned(db_and_app):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db)
    db.execute("UPDATE group_session_discussions SET status='closed' WHERE id=?", (scope["discussion_id"],))
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.scan_due(now=now, enqueue=False)
    assert result["scanned"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_reflection_slots")["c"] == 0


def test_auto_submitted_discussion_is_not_scanned(db_and_app):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db)
    db.execute(
        "UPDATE group_session_discussions SET auto_submitted_at=? WHERE id=?",
        (_time(now), scope["discussion_id"]),
    )
    from services.emotion_slot_service import EmotionSlotService

    assert EmotionSlotService.scan_due(now=now, enqueue=False)["scanned"] == 0


def test_disabled_agent_expires_already_due_slot(db_and_app):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db)
    slot_id = _due_slot(db, seeded, scope, now)
    db.execute("UPDATE experiment_sessions SET emotion_agent_enabled=0 WHERE id=?", (seeded["session_id"],))
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "expired"
    assert result["reason"] == "agent_disabled"


def test_no_new_student_messages_is_classified_as_low_participation(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db)
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(
        monkeypatch,
        {
            "should_send": True,
            "message": "小组暂时安静也很自然，大家不必有压力，按自己的节奏逐渐交流就好 🌱",
        },
    )
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "sent"
    assessment = db.query_one(
        "SELECT emotion_feedback_state FROM emotion_feedback_assessments WHERE slot_id=?",
        (slot_id,),
    )
    assert assessment["emotion_feedback_state"] == "GROUP_LOW_PARTICIPATION"


def test_recent_strategy_artifact_does_not_suppress_emotion_slot(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=30))
    sera = db.get_sera_user_id()
    strategy = db.create_message(
        seeded["group_id"], sera, "策略提示", role="agent", created_at=_time(now - timedelta(seconds=10))
    )
    db.execute("UPDATE messages SET agent_type='strategy' WHERE id=?", (strategy["id"],))
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "大家正在继续交流，小组保持自然节奏就好 🌿"},
    )
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "sent"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'"
    )["c"] == 1


def test_recent_emotion_publish_defers_instead_of_dropping_fixed_slot(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=631)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "大家正在认真交流，小组继续保持这份投入！😊"},
    )
    from services.emotion_slot_service import EmotionSlotService

    first_slot_id = _due_slot(db, seeded, scope, now)
    assert EmotionSlotService.execute_slot(first_slot_id, now=now)["status"] == "sent"

    second_now = now + timedelta(seconds=INTERVAL)
    second = EmotionSlotService.ensure_latest_due_slot(scope, now=second_now)
    second_slot_id = int(second["enqueue_slot_id"])
    deferred = EmotionSlotService.execute_slot(second_slot_id, now=second_now)

    assert deferred["status"] == "deferred"
    assert deferred["reason"] == "recent_emotion_publish"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'"
    )["c"] == 1

    old_enough = _time(datetime.now() - timedelta(seconds=INTERVAL + 1))
    db.execute(
        "UPDATE messages SET created_at=? WHERE agent_type='emotion'",
        (old_enough,),
    )
    db.execute(
        "UPDATE emotion_reflection_slots SET next_retry_at=? WHERE id=?",
        (_time(second_now), second_slot_id),
    )
    retry = EmotionSlotService.ensure_latest_due_slot(scope, now=second_now)
    assert retry.get("enqueue_slot_id") == second_slot_id
    assert EmotionSlotService.execute_slot(second_slot_id, now=second_now)[
        "status"
    ] == "sent"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'"
    )["c"] == 2


def test_legacy_strategy_lock_does_not_defer_or_get_overwritten_by_emotion(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=63)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=30))
    strategy_run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, discussion_id, task_id, cutoff_sequence,
            status, agent_type, trigger_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"],
            seeded["session_id"],
            scope["discussion_id"],
            seeded["task_id"],
            1,
            "GENERATING",
            "strategy",
            "auto_state",
            db.now_str(),
        ),
    )
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    lock_token = RoomLeaseService.acquire(
        seeded["group_id"],
        strategy_run_id,
        lock_seconds=60,
    )
    assert lock_token
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(
        monkeypatch,
        {
            "should_send": True,
            "message": "大家可以继续分享想法，小组按自己的节奏交流就好 🌿",
        },
    )
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "sent"
    locked_room = db.query_one(
        """
        SELECT state, lock_token, active_intervention_run_id
          FROM groups
         WHERE id=?
        """,
        (seeded["group_id"],),
    )
    assert dict(locked_room) == {
        "state": "AI_INTERVENING",
        "lock_token": lock_token,
        "active_intervention_run_id": strategy_run_id,
    }
    assert RoomLeaseService.release(seeded["group_id"], lock_token) is True


def test_missed_slots_are_skipped_and_only_latest_is_reserved(db_and_app):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db)
    scope["all_members_entered_at"] = _time(now - timedelta(seconds=INTERVAL * 3 + 5))
    db.execute(
        "UPDATE group_session_discussions SET started_at=? WHERE id=?",
        (scope["all_members_entered_at"], scope["discussion_id"]),
    )
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    rows = [dict(r) for r in db.query_all("SELECT * FROM emotion_reflection_slots ORDER BY slot_index")]
    assert result["slot_index"] == 3
    assert [row["status"] for row in rows] == ["skipped", "skipped", "pending"]
    assert [row["skip_reason"] for row in rows[:2]] == [
        "missed_due_to_downtime", "missed_due_to_downtime"
    ]


def test_model_decline_still_sends_fallback_message(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(monkeypatch, {"should_send": False, "message": None})
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "sent"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'")["c"] == 1
    run = db.query_one("SELECT status, fallback_used, skip_reason FROM intervention_runs WHERE agent_type='emotion'")
    assert dict(run) == {
        "status": "FALLBACK",
        "fallback_used": 1,
        "skip_reason": "llm_declined_output_forced",
    }


def test_sent_slot_backfills_bidirectional_message_and_run_links(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    slot_id = _due_slot(db, seeded, scope, now)
    _fake_gateway(monkeypatch, {"should_send": True, "message": "大家正稳稳推进着，保持轻松节奏 😊"})
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    assert result["status"] == "sent"
    slot = db.query_one("SELECT * FROM emotion_reflection_slots WHERE id=?", (slot_id,))
    run = db.query_one("SELECT * FROM intervention_runs WHERE id=?", (slot["intervention_run_id"],))
    message = db.query_one("SELECT * FROM messages WHERE id=?", (slot["message_id"],))
    assert run["message_id"] == message["id"]
    assert message["intervention_run_id"] == run["id"]
    assert message["session_id"] == seeded["session_id"]
    assert message["agent_type"] == "emotion"
    assert run["discussion_id"] == scope["discussion_id"]
    assert run["trigger_type"] == "emotion_time_slot"
    room = db.query_one(
        """
        SELECT state, lock_token, active_intervention_run_id
          FROM groups
         WHERE id=?
        """,
        (seeded["group_id"],),
    )
    assert dict(room) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }


def test_publish_failure_retries_at_most_configured_attempts(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db)
    _add_student_message(db, seeded, created_at=now - timedelta(seconds=20))
    _fake_gateway(monkeypatch, {"should_send": True, "message": "大家正稳稳推进着，保持轻松节奏 😊"})
    import services.emotion_agent.emotion_reflection_service as emotion_module

    monkeypatch.setattr(
        emotion_module.EmotionReflectionService,
        "_publish_emotion_message",
        staticmethod(lambda **_kwargs: {"ok": False, "reason": "publish_failed"}),
    )
    from services.emotion_slot_service import EmotionSlotService

    slot_id = _due_slot(db, seeded, scope, now)
    first = EmotionSlotService.execute_slot(slot_id, now=now)
    assert first["status"] == "failed" and first["retryable"] is True
    db.execute("UPDATE emotion_reflection_slots SET next_retry_at=? WHERE id=?", (_time(now), slot_id))
    retry = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    assert retry.get("enqueue_slot_id") == slot_id
    second = EmotionSlotService.execute_slot(slot_id, now=now)
    assert second["status"] == "failed" and second["retryable"] is False
    terminal = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    assert terminal.get("enqueue_slot_id") is None
    assert db.query_one("SELECT retry_count FROM emotion_reflection_slots WHERE id=?", (slot_id,))["retry_count"] == 2
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'")["c"] == 0


def test_legacy_tick_task_is_noop_and_does_not_seed_schedule(db_and_app, test_env):
    _db, _app, _client = db_and_app
    from agent.emotion_tasks import execute_emotion_reflection_tick

    path = test_env["tasks_db_path"]
    conn = sqlite3.connect(path)
    before = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
    conn.close()
    result = execute_emotion_reflection_tick.call_local(1, 1, 1, 1, 1, "x", "x", "x", 0)
    assert result["reason"] == "legacy_recursive_scheduler_disabled"
    conn = sqlite3.connect(path)
    after = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
    conn.close()
    assert after == before


def test_database_initialization_is_idempotent_for_slot_schema(db_and_app):
    db, _app, _client = db_and_app
    db.init_db()
    db.init_db()
    table = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='emotion_reflection_slots'"
    )
    index = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_emotion_slots_scope_slot'"
    )
    assert table and index


def test_migration_backfills_legacy_emotion_run_message_link(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=69, member_count=1)
    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, task_id, cutoff_sequence,
            agent_type, trigger_type, status, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"], seeded["session_id"], seeded["task_id"],
            0, "emotion", "scheduled_10min", "PUBLISHED", db.now_str(),
        ),
    )
    message = db.create_message(
        seeded["group_id"],
        db.get_sera_user_id(),
        "历史情绪消息 😊",
        role="agent",
        intervention_run_id=run_id,
    )
    db.execute("UPDATE messages SET agent_type='emotion' WHERE id=?", (message["id"],))
    assert db.query_one("SELECT message_id FROM intervention_runs WHERE id=?", (run_id,))["message_id"] is None
    db.init_db()
    assert db.query_one("SELECT message_id FROM intervention_runs WHERE id=?", (run_id,))["message_id"] == message["id"]
