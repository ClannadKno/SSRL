# -*- coding: utf-8 -*-

from tests.helpers import login_with_key, seed_running_session


def test_student_discussion_page_uses_unified_sync_loop(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=41, member_count=1, limit_minutes=10)
    user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)

    from services.group_discussion_runtime_service import enter_group_discussion_stage

    enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], user_id)

    response = client.get("/student/collab?phase=discussion", headers=headers)

    assert response.status_code == 200
    text = response.data.decode("utf-8")
    assert "/api/student/sync?" in text
    assert "/api/discussion/enter" in text
    assert "/api/group/" not in text
    assert "/api/rooms/" not in text
    assert "/api/heartbeat" not in text
    assert "/api/student/status" not in text
    assert "/api/collaborative-documents/current" not in text
    assert "setInterval(" not in text


def test_waiting_discussion_page_redirects_when_enter_returns_document(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=42, member_count=4, limit_minutes=10)
    _user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)

    response = client.get("/student/collab?phase=discussion", headers=headers)

    assert response.status_code == 200
    text = response.data.decode("utf-8")
    assert "if (data && !data.waiting && data.document)" in text
    gate_start = text.index("function handleDiscussionGateSync")
    waiting_guard = text.index("if (!discussionWaitingForPeers) return;", gate_start)
    ready_redirect = text.index('if (discussionStatus && discussionStatus !== "waiting" && data.document)', gate_start)
    assert waiting_guard < ready_redirect
