# -*- coding: utf-8 -*-

from datetime import datetime, timedelta

import pytest

from tests.helpers import create_group, expire_group_discussion, seed_running_session


def _make_session(db, *, status="running", deadline=None, session_no=1, task_id=None):
    now = db.now_str()
    if task_id is None:
        task_id = db.execute(
            "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
            ("Lifecycle Task", "Discuss", 10, now),
        )
    return db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time, deadline,
            time_limit_minutes, created_by, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (session_no, "discussion", task_id, status, now, deadline, 10, 1, now, now),
    )


def test_legacy_session_deadline_does_not_close_or_expire_running_session(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db)
    past = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    session_id = _make_session(db, deadline=past)

    from services.session_lifecycle import (
        auto_end_expired_sessions,
        get_session_context,
        is_discussion_writable,
        is_session_expired,
    )

    result = auto_end_expired_sessions()
    assert result["ended"] == 0
    assert is_session_expired(session_id) is False
    assert is_discussion_writable(session_id, group_id) is True
    ctx = get_session_context(session_id, group_id=group_id)
    assert ctx["status"] == "running"
    assert ctx["deadline"] is None
    assert ctx["posttest_available"] is False


def test_teacher_end_closes_session_without_closing_group(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=11, member_count=1)

    from services.teacher_session_service import end_session
    from services.session_lifecycle import get_session_context

    ended = end_session(session_id=seeded["session_id"], operator_id=1)
    assert ended["status"] == "ended"

    group = db.query_one("SELECT state FROM groups WHERE id=?", (seeded["group_id"],))
    assert group["state"] == "OPEN"
    ctx = get_session_context(seeded["session_id"], group_id=seeded["group_id"])
    assert ctx["discussion_writable"] is False
    assert ctx["document_writable"] is False
    assert ctx["posttest_available"] is True


def test_teacher_start_session_sets_current_context_without_session_deadline(db_and_app):
    db, _app, _client = db_and_app
    now = db.now_str()
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Teacher Started Task", "Discuss", 15, now),
    )

    from services.teacher_session_service import create_session, start_session

    draft = create_session(operator_id=1, session_no=14, task_id=task_id, time_limit_minutes=15)
    running = start_session(session_id=draft["id"], operator_id=1)

    assert running["status"] == "running"
    assert running["deadline"] is None
    assert db.get_setting("current_session_id") == str(running["id"])
    assert db.get_setting("current_session_no") == "14"
    assert db.get_setting("current_task_id") == str(task_id)


def test_create_session_rejects_duplicate_session_no(db_and_app):
    db, _app, _client = db_and_app
    now = db.now_str()
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Unique Session Task", "Discuss", 15, now),
    )

    from services.teacher_session_service import create_session

    create_session(operator_id=1, session_no=15, task_id=task_id, time_limit_minutes=15)
    with pytest.raises(ValueError):
        create_session(operator_id=1, session_no=15, task_id=task_id, time_limit_minutes=15)


def test_teacher_create_session_endpoint_rejects_duplicate_session_no(db_and_app, teacher_login):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    now = db.now_str()
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Teacher Endpoint Unique Task", "Discuss", 15, now),
    )

    first = client.post(
        "/api/teacher/session/create",
        json={"session_no": 151, "task_id": task_id},
        headers=headers,
    )
    second = client.post(
        "/api/teacher/session/create",
        json={"session_no": 151, "task_id": task_id},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 400


def test_start_session_rejects_duplicate_session_no_from_legacy_data(db_and_app):
    db, _app, _client = db_and_app
    db.execute("DROP INDEX IF EXISTS idx_experiment_sessions_session_no_unique")
    now = db.now_str()
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Dirty Duplicate Task", "Discuss", 15, now),
    )

    from services.teacher_session_service import create_session, start_session

    draft = create_session(operator_id=1, session_no=16, task_id=task_id, time_limit_minutes=15)
    _make_session(db, status="draft", session_no=16, task_id=task_id)

    with pytest.raises(ValueError):
        start_session(session_id=draft["id"], operator_id=1)


def test_legacy_current_session_endpoint_rejects_duplicate_session_no(db_and_app, teacher_login):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    db.execute("DROP INDEX IF EXISTS idx_experiment_sessions_session_no_unique")
    _make_session(db, status="draft", session_no=17)
    _make_session(db, status="ended", session_no=17)

    response = client.post(
        "/api/teacher/tasks/current-session",
        json={"session_no": 17},
        headers=headers,
    )

    assert response.status_code == 409


def test_current_running_session_context_prefers_current_session_id(db_and_app):
    db, _app, _client = db_and_app
    first_id = _make_session(db, status="running", session_no=18)
    second_id = _make_session(db, status="running", session_no=19)
    db.set_setting("current_session_id", str(first_id))

    ctx = db.get_current_running_session_context()

    assert ctx["session_id"] == first_id
    assert ctx["session_id"] != second_id


def test_current_running_session_context_does_not_guess_when_ambiguous(db_and_app):
    db, _app, _client = db_and_app
    _make_session(db, status="running", session_no=20)
    _make_session(db, status="running", session_no=21)
    db.set_setting("current_session_id", "")

    assert db.get_current_running_session_context() is None


def test_session_no_unique_index_exists_on_clean_database(db_and_app):
    db, _app, _client = db_and_app

    indexes = db.query_all("PRAGMA index_list('experiment_sessions')")

    assert any(row["name"] == "idx_experiment_sessions_session_no_unique" for row in indexes)


def test_group_timeout_blocks_writes_but_keeps_session_open(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=12, member_count=1)
    user_id, _login_key = seeded["students"][0]
    expire_group_discussion(db, seeded["session_id"], seeded["group_id"], user_id)

    from services.session_lifecycle import (
        get_session_context,
        is_discussion_writable,
        is_document_writable,
        is_help_request_allowed,
    )

    session = db.query_one("SELECT status FROM experiment_sessions WHERE id=?", (seeded["session_id"],))
    assert session["status"] == "running"
    assert is_discussion_writable(seeded["session_id"], seeded["group_id"], user_id) is False
    assert is_help_request_allowed(seeded["session_id"], seeded["group_id"], user_id) is False
    assert is_document_writable(seeded["session_id"], seeded["group_id"], user_id=user_id) is False
    assert get_session_context(seeded["session_id"], group_id=seeded["group_id"])["posttest_available"] is False


def test_agent_gate_blocks_after_manual_end_or_group_timeout(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=13, member_count=1)
    user_id, _login_key = seeded["students"][0]

    from services.session_lifecycle import check_agent_allowed

    allowed, reason = check_agent_allowed(
        seeded["group_id"],
        session_id=seeded["session_id"],
        task_id=seeded["task_id"],
        session_no=seeded["session_no"],
    )
    assert (allowed, reason) == (True, "active")

    expire_group_discussion(db, seeded["session_id"], seeded["group_id"], user_id)
    allowed, reason = check_agent_allowed(
        seeded["group_id"],
        session_id=seeded["session_id"],
        task_id=seeded["task_id"],
        session_no=seeded["session_no"],
    )
    assert (allowed, reason) == (False, "group_discussion_closed")
