# -*- coding: utf-8 -*-

from datetime import datetime, timedelta

from tests.helpers import create_questionnaire, expire_group_discussion


def _seed_questionnaire_session(db, group_id, *, session_no=41, limit_minutes=5):
    now = db.now_str()
    old_start = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE status='running'")
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Questionnaire Task", "Discuss then answer", limit_minutes, now),
    )
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (session_no, "discussion", task_id, "running", old_start, limit_minutes, now, now),
    )
    db.set_setting("current_session_id", str(session_id))
    db.set_setting("current_session_no", str(session_no))
    db.set_setting("current_task_id", str(task_id))

    pre_qid, pre_item_id = create_questionnaire(db, "PRE_CORE", "pre")
    post_qid, post_item_id = create_questionnaire(db, "POST_CORE", "post")
    db.create_questionnaire_publication(pre_qid, session_id, session_no, "pre", group_id=group_id)
    db.create_questionnaire_publication(post_qid, session_id, session_no, "post", group_id=group_id)
    return {
        "session_id": session_id,
        "session_no": session_no,
        "task_id": task_id,
        "pre_qid": pre_qid,
        "pre_item_id": pre_item_id,
        "post_qid": post_qid,
        "post_item_id": post_item_id,
    }


def test_pretest_is_available_before_group_timer_starts(db_and_app, student_login):
    db, _app, client = db_and_app
    _client, headers, _uid, group_id = student_login
    seeded = _seed_questionnaire_session(db, group_id)

    response = client.get("/api/student/published-questionnaires?stage=pre", headers=headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["session"]["session_id"] == seeded["session_id"]
    assert [q["id"] for q in data["questionnaires"]] == [seeded["pre_qid"]]
    assert db.query_one(
        "SELECT id FROM group_session_discussions WHERE session_id=? AND group_id=?",
        (seeded["session_id"], group_id),
    ) is None


def test_posttest_uses_running_session_after_group_timeout(db_and_app, student_login):
    db, _app, client = db_and_app
    _client, headers, user_id, group_id = student_login
    seeded = _seed_questionnaire_session(db, group_id)
    expire_group_discussion(db, seeded["session_id"], group_id, user_id)

    current = client.get("/api/student/current-session", headers=headers)
    assert current.status_code == 200
    current_data = current.get_json()
    assert current_data["session_id"] == seeded["session_id"]
    assert current_data["session_open"] is True
    assert current_data["group_discussion_status"] == "timed_out"
    assert current_data["posttest_available"] is True

    listed = client.get("/api/student/published-questionnaires?stage=post", headers=headers)
    assert listed.status_code == 200
    assert [q["id"] for q in listed.get_json()["questionnaires"]] == [seeded["post_qid"]]

    submitted = client.post(
        f"/api/student/questionnaires/{seeded['post_qid']}/submit",
        headers=headers,
        json={"response_stage": "post", "responses": {str(seeded["post_item_id"]): 5}},
    )
    assert submitted.status_code == 200
    row = db.query_one(
        """
        SELECT session_id, session_no, response_stage, status
        FROM questionnaire_submissions
        WHERE questionnaire_id=? AND user_id=?
        """,
        (seeded["post_qid"], user_id),
    )
    assert dict(row) == {
        "session_id": seeded["session_id"],
        "session_no": seeded["session_no"],
        "response_stage": "post",
        "status": "submitted",
    }
    assert db.query_one("SELECT status FROM experiment_sessions WHERE id=?", (seeded["session_id"],))["status"] == "running"


def test_manual_teacher_stop_closes_questionnaire_entry(db_and_app, student_login):
    db, _app, client = db_and_app
    _client, headers, _user_id, group_id = student_login
    seeded = _seed_questionnaire_session(db, group_id)

    from services.teacher_session_service import end_session

    end_session(session_id=seeded["session_id"], operator_id=1)

    listed = client.get("/api/student/published-questionnaires?stage=post", headers=headers)
    assert listed.status_code == 200
    assert listed.get_json()["status"] == "waiting_session"

    submitted = client.post(
        f"/api/student/questionnaires/{seeded['post_qid']}/submit",
        headers=headers,
        json={"response_stage": "post", "responses": {str(seeded["post_item_id"]): 5}},
    )
    assert submitted.status_code == 409
    assert submitted.get_json()["code"] == "waiting_session"
