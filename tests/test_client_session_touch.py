# -*- coding: utf-8 -*-
from datetime import datetime, timedelta


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


def test_read_only_student_api_does_not_touch_client_session(student_login, db_and_app):
    db, _app_module, _client = db_and_app
    client, headers, _user_id, group_id = student_login
    old_last_seen = "2000-01-01 00:00:00"
    _set_last_seen(db, headers, old_last_seen)

    response = client.get(f"/api/group/{group_id}/messages", headers=headers)

    assert response.status_code == 200
    assert _last_seen(db, headers) == old_last_seen


def test_student_heartbeat_touches_stale_client_session(student_login, db_and_app):
    db, _app_module, _client = db_and_app
    client, headers, _user_id, _group_id = student_login
    old_last_seen = "2000-01-01 00:00:00"
    _set_last_seen(db, headers, old_last_seen)

    response = client.post("/api/heartbeat", headers=headers)

    assert response.status_code == 200
    assert _last_seen(db, headers) != old_last_seen


def test_student_heartbeat_throttles_recent_client_session_touch(student_login, db_and_app):
    db, _app_module, _client = db_and_app
    client, headers, _user_id, _group_id = student_login
    recent_last_seen = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    _set_last_seen(db, headers, recent_last_seen)

    response = client.post("/api/heartbeat", headers=headers)

    assert response.status_code == 200
    assert _last_seen(db, headers) == recent_last_seen
