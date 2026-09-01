# -*- coding: utf-8 -*-

from tests.helpers import expire_group_discussion, login_with_key, seed_running_session


def test_waiting_gate_starts_group_timer_only_after_all_members_are_ready(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=21, member_count=2, limit_minutes=8)
    first_user, _first_login_key = seeded["students"][0]
    second_user, _second_login_key = seeded["students"][1]

    from services.group_discussion_runtime_service import enter_group_discussion_stage

    waiting = enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], first_user)
    assert waiting["status"] == "waiting"
    assert waiting["started_at"] is None
    assert waiting["deadline"] is None

    running = enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], second_user)
    assert running["status"] == "running"
    assert running["ready_student_ids"] == [first_user, second_user]
    assert running["started_at"] is not None
    assert running["deadline"] is not None


def test_discussion_enter_waits_before_creating_document(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=22, member_count=2)
    first_user, first_login_key = seeded["students"][0]
    second_user, second_login_key = seeded["students"][1]
    first_headers = login_with_key(client, first_login_key)

    current_before_enter = client.get("/api/collaborative-documents/current", headers=first_headers)
    assert current_before_enter.status_code == 200
    current_data = current_before_enter.get_json()
    assert current_data["waiting"] is True
    assert current_data["document"] is None
    assert current_data["group_discussion"] is None

    waiting_response = client.post("/api/discussion/enter", headers=first_headers)
    assert waiting_response.status_code == 200
    waiting_data = waiting_response.get_json()
    assert waiting_data["waiting"] is True
    assert waiting_data["document"] is None
    assert waiting_data["group_discussion_status"] == "waiting"
    assert "deadline" not in waiting_data["session"]
    assert db.query_one("SELECT id FROM collaborative_documents WHERE group_id=?", (seeded["group_id"],)) is None
    entry_count = db.query_one("SELECT COUNT(*) AS c FROM group_discussion_entries")["c"]

    current_after_enter = client.get("/api/collaborative-documents/current", headers=first_headers)
    assert current_after_enter.status_code == 200
    assert current_after_enter.get_json()["group_discussion_status"] == "waiting"
    assert db.query_one("SELECT COUNT(*) AS c FROM group_discussion_entries")["c"] == entry_count

    second_headers = login_with_key(client, second_login_key)
    running_response = client.post("/api/discussion/enter", headers=second_headers)
    assert running_response.status_code == 200
    running_data = running_response.get_json()
    assert running_data["waiting"] is False
    assert running_data["document"]["group_id"] == seeded["group_id"]
    assert running_data["group_discussion_status"] == "running"
    assert running_data["group_remaining_seconds"] is not None


def test_single_member_group_can_create_document_and_send_message(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=25, member_count=1)
    user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)

    document_response = client.post("/api/discussion/enter", headers=headers)
    assert document_response.status_code == 200
    document_data = document_response.get_json()
    assert document_data["waiting"] is False
    assert document_data["document"]["status"] == "editing"
    assert document_data["permission"] == "edit"

    current_response = client.get("/api/collaborative-documents/current", headers=headers)
    assert current_response.status_code == 200
    assert current_response.get_json()["document"]["id"] == document_data["document"]["id"]

    message_response = client.post(
        "/api/message",
        json={"group_id": seeded["group_id"], "content": "hello team"},
        headers=headers,
    )
    assert message_response.status_code == 200
    assert message_response.get_json()["ok"] is True


def test_group_timeout_rejects_mid_discussion_writes_but_allows_post_checkin(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=23, member_count=1)
    user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)
    expire_group_discussion(db, seeded["session_id"], seeded["group_id"], user_id)

    message = client.post(
        "/api/message",
        json={"group_id": seeded["group_id"], "content": "late message"},
        headers=headers,
    )
    assert message.status_code == 423
    assert message.get_json()["code"] == "GROUP_DISCUSSION_CLOSED"

    help_response = client.post(
        "/api/student/help",
        json={"group_id": seeded["group_id"], "request_text": "help after timeout"},
        headers=headers,
    )
    assert help_response.status_code == 423

    mid_checkin = client.post(
        "/api/checkin",
        json={"group_id": seeded["group_id"], "checkin_type": "mid", "emotion_option": "smooth"},
        headers=headers,
    )
    assert mid_checkin.status_code == 423

    post_checkin = client.post(
        "/api/checkin",
        json={"group_id": seeded["group_id"], "checkin_type": "post", "emotion_option": "smooth"},
        headers=headers,
    )
    assert post_checkin.status_code == 200


def test_teacher_status_reports_group_discussion_state(db_and_app, teacher_login):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=24, member_count=1)
    user_id, _login_key = seeded["students"][0]
    expire_group_discussion(db, seeded["session_id"], seeded["group_id"], user_id)
    _teacher_client, teacher_headers = teacher_login

    response = client.get("/api/teacher/status/current", headers=teacher_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["deadline"] is None
    assert data["remaining_seconds"] is None
    assert data["group_discussions"][0]["group_id"] == seeded["group_id"]
    assert data["group_discussions"][0]["group_discussion_status"] == "timed_out"
    assert data["group_discussions"][0]["ready_students"] == [
        {"student_id": user_id, "name": "Student 1"}
    ]
