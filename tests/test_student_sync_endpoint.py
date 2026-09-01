# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from tests.helpers import login_with_key, seed_running_session


def _token(headers):
    return headers["X-Tab-Token"]


def _set_last_seen(db, headers, value):
    db.execute(
        "UPDATE client_sessions SET last_seen=? WHERE token=?",
        (value, _token(headers)),
    )


def _last_seen(db, headers):
    row = db.query_one(
        "SELECT last_seen FROM client_sessions WHERE token=?",
        (_token(headers),),
    )
    return row["last_seen"]


def test_student_sync_waits_safely_without_running_session(student_login):
    client, headers, _user_id, group_id = student_login

    response = client.get("/api/student/sync?after_message_id=0&after_sequence=0", headers=headers)

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["status"] == "waiting_session"
    assert data["session_open"] is False
    assert data["group_id"] == group_id
    assert data["session"] is None
    assert data["chat"] == {"messages": [], "latest_id": 0}
    assert data["events"]["messages"] == []
    assert data["document"] is None
    assert data["permission"] is None
    assert data["navigation"]["posttest_available"] is False
    assert data["heartbeat"]["ok"] is True


def test_student_sync_returns_running_state_document_and_throttled_touch(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=31, member_count=1, limit_minutes=20)
    user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)

    from services.group_discussion_runtime_service import enter_group_discussion_stage
    from services.collaborative_service import get_or_create_document

    runtime = enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], user_id)
    first_message = db.create_message(seeded["group_id"], user_id, "first message", role="student")
    second_message = db.create_message(seeded["group_id"], user_id, "second message", role="student")
    first_sequence = db.query_one("SELECT sequence FROM messages WHERE id=?", (first_message["id"],))["sequence"]
    document = get_or_create_document(
        seeded["group_id"],
        seeded["task_id"],
        seeded["session_no"],
        user_id,
    )
    stale_last_seen = "2000-01-01 00:00:00"
    _set_last_seen(db, headers, stale_last_seen)

    response = client.get(
        f"/api/student/sync?after_message_id={first_message['id']}&after_sequence={first_sequence}",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["session"]["session_id"] == seeded["session_id"]
    assert data["room"]["state"] == "OPEN"
    assert data["ai_lock"]["locked"] is False
    assert [m["id"] for m in data["messages"]] == [second_message["id"]]
    assert data["latest_message_id"] == second_message["id"]
    assert [m["id"] for m in data["events"]["messages"]] == [second_message["id"]]
    assert data["event_sequence"] == second_message["sequence"]
    assert data["discussion"]["status"] == "running"
    assert data["discussion"]["ready_count"] == runtime["ready_student_count"]
    assert data["discussion"]["expected_count"] == runtime["expected_student_count"]
    assert data["discussion"]["remaining_seconds"] is not None
    assert data["document"]["id"] == document["id"]
    assert data["permission"] == "edit"
    assert data["navigation"]["session_open"] is True
    assert data["navigation"]["posttest_available"] is False
    assert data["heartbeat"]["touched"] is True
    assert _last_seen(db, headers) != stale_last_seen

    recent_last_seen = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    _set_last_seen(db, headers, recent_last_seen)

    throttled = client.get("/api/student/sync", headers=headers)

    assert throttled.status_code == 200
    assert throttled.get_json()["heartbeat"]["touched"] is False
    assert _last_seen(db, headers) == recent_last_seen
