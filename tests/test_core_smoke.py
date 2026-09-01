# -*- coding: utf-8 -*-


def test_app_bootstraps_with_isolated_database(db_and_app, test_env):
    db, app_module, client = db_and_app

    assert db.DB_PATH == test_env["db_path"]
    assert app_module.app is not None
    assert client.get("/login").status_code == 200

    routes = {rule.rule for rule in app_module.app.url_map.iter_rules()}
    assert "/api/message" in routes
    assert "/api/discussion/enter" in routes
    assert "/api/collaborative-documents/current" in routes
    assert "/api/student/published-questionnaires" in routes
    assert "/api/teacher/status/current" in routes


def test_student_key_login_can_use_student_api(student_login):
    client, headers, _user_id, group_id = student_login

    response = client.post("/api/heartbeat", headers=headers)
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "group_id": group_id}


def test_student_collab_waits_when_teacher_has_not_published_task(student_login):
    client, headers, _user_id, _group_id = student_login

    response = client.get("/student/collab?phase=discussion", headers=headers)

    assert response.status_code == 200
    text = response.data.decode("utf-8")
    assert "等待教师发布任务" in text
    assert 'const MODE = "waiting_task";' in text


def test_current_document_waits_when_teacher_has_not_published_task(student_login):
    client, headers, _user_id, _group_id = student_login

    response = client.get("/api/collaborative-documents/current", headers=headers)

    assert response.status_code == 200
    data = response.get_json()
    assert data["waiting"] is True
    assert data["waiting_reason"] == "waiting_task"
    assert data["message"] == "等待教师发布任务"
    assert data["document"] is None


def test_teacher_key_login_can_read_current_status(teacher_login):
    client, headers = teacher_login

    response = client.get("/api/teacher/status/current", headers=headers)
    assert response.status_code == 200
    data = response.get_json()
    assert "current_session" in data
    assert "group_discussions" in data


def test_removed_unified_events_export_is_not_available_to_teacher(teacher_login):
    client, headers = teacher_login

    response = client.get("/export/unified-events.csv", headers=headers)
    assert response.status_code == 404

    supported = client.get("/export/messages", headers=headers)
    assert supported.status_code == 200
    assert "application/zip" in supported.headers.get("Content-Type", "")
    assert supported.headers.get("X-Export-Version") == "4.0"
