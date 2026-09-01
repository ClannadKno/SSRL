# -*- coding: utf-8 -*-
"""Scenario test for one Week 1 group discussion with four student accounts."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.helpers import login_with_key, seed_running_session


def _create_questionnaire(db, code: str, timing: str, item_count: int = 4) -> tuple[int, list[int]]:
    qid = db.create_questionnaire(
        {
            "code": code,
            "title": code,
            "timing": timing,
            "scale_max": 5,
            "active": True,
        },
        items=[
            {
                "item_code": f"{code}_Q{i}",
                "prompt_text": f"{code} item {i}",
                "dimension_label": "Week 1",
                "sort_order": i,
            }
            for i in range(1, item_count + 1)
        ],
    )
    rows = db.query_all(
        "SELECT id FROM questionnaire_items WHERE questionnaire_id=? ORDER BY sort_order ASC, id ASC",
        (qid,),
    )
    return qid, [row["id"] for row in rows]


def _seed_week_one_group(db):
    seeded = seed_running_session(db, session_no=1, member_count=4, limit_minutes=60)
    db.execute(
        """
        UPDATE learning_tasks
        SET title=?, question=?, time_limit_minutes=?
        WHERE id=?
        """,
        (
            "Week 1 collaborative planning task",
            "Discuss the first week plan, divide responsibilities, resolve disagreements, and submit one group document.",
            60,
            seeded["task_id"],
        ),
    )
    pre_qid, pre_item_ids = _create_questionnaire(db, "WEEK1_PRE", "pre")
    post_qid, post_item_ids = _create_questionnaire(db, "WEEK1_POST", "post")
    db.create_questionnaire_publication(
        pre_qid,
        seeded["session_id"],
        seeded["session_no"],
        "pre",
        group_id=seeded["group_id"],
    )
    db.create_questionnaire_publication(
        post_qid,
        seeded["session_id"],
        seeded["session_no"],
        "post",
        group_id=seeded["group_id"],
    )
    seeded.update(
        {
            "pre_qid": pre_qid,
            "pre_item_ids": pre_item_ids,
            "post_qid": post_qid,
            "post_item_ids": post_item_ids,
        }
    )
    return seeded


def _random_responses(item_ids: list[int], rng: random.Random) -> dict[str, int]:
    return {str(item_id): rng.randint(1, 5) for item_id in item_ids}


def _submit_questionnaire(client, headers, qid: int, stage: str, item_ids: list[int], rng: random.Random):
    response = client.post(
        f"/api/student/questionnaires/{qid}/submit",
        headers=headers,
        json={"response_stage": stage, "responses": _random_responses(item_ids, rng)},
    )
    assert response.status_code == 200
    assert response.get_json()["submitted"] is True
    return response


def _post_message(client, db, headers, group_id: int, base_time: datetime, minute: int, content: str, cmid: str):
    response = client.post(
        "/api/message",
        headers=headers,
        json={"group_id": group_id, "content": content, "client_message_id": cmid},
    )
    assert response.status_code == 200
    data = response.get_json()
    created_at = (base_time + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE messages SET created_at=? WHERE id=?", (created_at, data["message_id"]))
    return data


def _expire_discussion(db, session_id: int, group_id: int):
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """
        UPDATE group_session_discussions
        SET deadline=?, updated_at=?
        WHERE session_id=? AND group_id=?
        """,
        (past, db.now_str(), session_id, group_id),
    )


def test_week_one_four_account_group_discussion_random_questionnaires_and_timeout_submit(
    db_and_app,
    teacher_login,
):
    db, app_module, _client = db_and_app
    teacher_client, teacher_headers = teacher_login
    seeded = _seed_week_one_group(db)
    group_id = seeded["group_id"]
    rng = random.Random(20260714)
    student_sessions = []
    for _user_id, login_key in seeded["students"]:
        student_client = app_module.app.test_client()
        student_sessions.append((student_client, login_with_key(student_client, login_key)))
    student_clients = [student_client for student_client, _headers in student_sessions]
    headers = [student_headers for _student_client, student_headers in student_sessions]

    for student_client, student_headers in student_sessions:
        listed = student_client.get("/api/student/published-questionnaires?stage=pre", headers=student_headers)
        assert listed.status_code == 200
        listed_data = listed.get_json()
        assert listed_data["status"] == "ok"
        assert [q["id"] for q in listed_data["questionnaires"]] == [seeded["pre_qid"]]
        assert listed_data["questionnaires"][0]["submitted"] is False
        _submit_questionnaire(
            student_client,
            student_headers,
            seeded["pre_qid"],
            "pre",
            seeded["pre_item_ids"],
            rng,
        )

    duplicate_pre = student_clients[0].post(
        f"/api/student/questionnaires/{seeded['pre_qid']}/submit",
        headers=headers[0],
        json={"response_stage": "pre", "responses": _random_responses(seeded["pre_item_ids"], rng)},
    )
    assert duplicate_pre.status_code == 409
    assert "Already submitted" in duplicate_pre.get_json()["error"]

    for index, (student_client, student_headers) in enumerate(student_sessions[:3], start=1):
        waiting = student_client.post("/api/discussion/enter", headers=student_headers)
        assert waiting.status_code == 200
        data = waiting.get_json()
        assert data["waiting"] is True
        assert data["document"] is None
        assert data["group_discussion_status"] == "waiting"
        assert data["group_discussion"]["ready_student_count"] == index

    running = student_clients[3].post("/api/discussion/enter", headers=headers[3])
    assert running.status_code == 200
    running_data = running.get_json()
    assert running_data["waiting"] is False
    assert running_data["permission"] == "edit"
    assert running_data["group_discussion_status"] == "running"
    assert running_data["group_discussion"]["ready_student_count"] == 4
    started_at = datetime.strptime(running_data["group_discussion_started_at"], "%Y-%m-%d %H:%M:%S")
    deadline = datetime.strptime(running_data["group_discussion_deadline"], "%Y-%m-%d %H:%M:%S")
    assert deadline - started_at == timedelta(minutes=60)
    document_id = running_data["document"]["id"]

    for student_client, student_headers in student_sessions:
        current = student_client.get("/api/collaborative-documents/current", headers=student_headers)
        assert current.status_code == 200
        data = current.get_json()
        assert data["waiting"] is False
        assert data["document"]["id"] == document_id
        assert data["permission"] == "edit"

    empty_message = student_clients[0].post(
        "/api/message",
        headers=headers[0],
        json={"group_id": group_id, "content": "   "},
    )
    assert empty_message.status_code == 400

    wrong_group = student_clients[0].post(
        "/api/message",
        headers=headers[0],
        json={"group_id": group_id + 9999, "content": "wrong room"},
    )
    assert wrong_group.status_code == 403

    db.execute("UPDATE groups SET state='AI_INTERVENING' WHERE id=?", (group_id,))
    locked_by_ai = student_clients[0].post(
        "/api/message",
        headers=headers[0],
        json={"group_id": group_id, "content": "Can we keep typing?"},
    )
    assert locked_by_ai.status_code == 423
    assert locked_by_ai.get_json()["code"] == "ROOM_AI_INTERVENING"
    db.execute("UPDATE groups SET state='OPEN' WHERE id=?", (group_id,))

    base_time = datetime.now() - timedelta(hours=1)
    script = [
        (0, 0, "Let's confirm the Week 1 goal and what the document must answer.", "w1-m1"),
        (1, 6, "I can own the evidence summary and make sure the final answer is specific.", "w1-m2"),
        (2, 12, "I am confused about how to connect emotion check-ins with the group plan.", "w1-m3"),
        (3, 18, "Let's split roles: summary, examples, risks, and final editing.", "w1-m4"),
        (1, 34, "I disagree with putting risks last; the plan reads clearer if risks shape the actions.", "w1-m5"),
        (0, 45, "Good point. We'll frame risks first, then map actions to them.", "w1-m6"),
        (2, 55, "Final structure looks ready: goal, roles, risks, actions, and evidence.", "w1-m7"),
    ]
    for student_index, minute, content, cmid in script:
        _post_message(
            student_clients[student_index],
            db,
            headers[student_index],
            group_id,
            base_time,
            minute,
            content,
            cmid,
        )

    retried = student_clients[3].post(
        "/api/message",
        headers=headers[3],
        json={"group_id": group_id, "content": script[3][2], "client_message_id": script[3][3]},
    )
    assert retried.status_code == 200
    assert retried.get_json()["message_id"] == db.query_one(
        "SELECT id FROM messages WHERE client_message_id=?",
        (script[3][3],),
    )["id"]

    mid_checkin = student_clients[2].post(
        "/api/checkin",
        headers=headers[2],
        json={
            "group_id": group_id,
            "checkin_type": "mid",
            "emotion_option": "stuck",
            "positivity": 2,
            "engagement": 4,
            "atmosphere": 3,
            "expression_willingness": 3,
            "note": "Need a hint before finalizing.",
        },
    )
    assert mid_checkin.status_code == 200

    help_response = student_clients[2].post(
        "/api/student/help",
        headers=headers[2],
        json={
            "group_id": group_id,
            "request_text": "We need help turning the disagreement into a concrete first-week plan.",
            "client_message_id": "w1-help",
        },
    )
    assert help_response.status_code == 202
    assert help_response.get_json()["accepted"] is True
    help_row = db.query_one(
        "SELECT status, request_text FROM help_requests WHERE group_id=? ORDER BY id DESC LIMIT 1",
        (group_id,),
    )
    assert help_row["status"] in {"COMPLETED", "COMPLETED_WITH_FALLBACK", "FAILED"}
    assert "disagreement" in help_row["request_text"]

    final_text = (
        "Week 1 submission\n"
        "Goal: agree on the problem and learning plan.\n"
        "Roles: evidence lead, example lead, risk checker, final editor.\n"
        "Risks: unclear criteria and uneven participation.\n"
        "Actions: check in mid-way, resolve disagreement with evidence, and submit one shared plan."
    )
    snapshot = student_clients[3].post(
        f"/api/collaborative-documents/{document_id}/snapshot",
        headers=headers[3],
        json={
            "content_json": '{"type":"doc","scenario":"week1"}',
            "content_html": "<p>Week 1 submission</p>",
            "content_text": final_text,
        },
    )
    assert snapshot.status_code == 200

    _expire_discussion(db, seeded["session_id"], group_id)

    late_message = student_clients[0].post(
        "/api/message",
        headers=headers[0],
        json={"group_id": group_id, "content": "late change"},
    )
    assert late_message.status_code == 423
    assert late_message.get_json()["code"] == "GROUP_DISCUSSION_CLOSED"

    late_mid_checkin = student_clients[1].post(
        "/api/checkin",
        headers=headers[1],
        json={"group_id": group_id, "checkin_type": "mid", "emotion_option": "late"},
    )
    assert late_mid_checkin.status_code == 423

    late_help = student_clients[2].post(
        "/api/student/help",
        headers=headers[2],
        json={"group_id": group_id, "request_text": "help after time"},
    )
    assert late_help.status_code == 423

    late_snapshot = student_clients[3].post(
        f"/api/collaborative-documents/{document_id}/snapshot",
        headers=headers[3],
        json={"content_text": "late edit"},
    )
    assert late_snapshot.status_code == 403

    with patch("services.collaborative_internal.freeze_document", return_value={"ok": True}), patch(
        "services.collaborative_internal.flush_document",
        return_value={"ok": True, "state_revision": 2},
    ), patch("services.collaborative_internal.close_document", return_value={"ok": True}):
        auto_submit = student_clients[0].post(
            f"/api/collaborative-documents/{document_id}/submit/auto-timeout",
            headers=headers[0],
            json={},
        )
    assert auto_submit.status_code == 200
    assert auto_submit.get_json()["submitted"] is True
    assert db.query_one("SELECT status, content_text FROM collaborative_documents WHERE id=?", (document_id,))[
        "status"
    ] == "submitted"
    assert db.query_one(
        "SELECT id FROM submissions WHERE group_id=? AND submitted_by='auto_timeout'",
        (group_id,),
    )
    assert db.query_one(
        "SELECT id FROM process_events WHERE group_id=? AND event_type='auto_timeout_submission'",
        (group_id,),
    )

    for student_client, student_headers in student_sessions:
        post_checkin = student_client.post(
            "/api/checkin",
            headers=student_headers,
            json={
                "group_id": group_id,
                "checkin_type": "post",
                "emotion_option": "done",
                "positivity": rng.randint(2, 5),
                "engagement": rng.randint(2, 5),
                "atmosphere": rng.randint(2, 5),
                "expression_willingness": rng.randint(2, 5),
            },
        )
        assert post_checkin.status_code == 200
        listed = student_client.get("/api/student/published-questionnaires?stage=post", headers=student_headers)
        assert listed.status_code == 200
        assert [q["id"] for q in listed.get_json()["questionnaires"]] == [seeded["post_qid"]]
        _submit_questionnaire(
            student_client,
            student_headers,
            seeded["post_qid"],
            "post",
            seeded["post_item_ids"],
            rng,
        )

    duplicate_post = student_clients[0].post(
        f"/api/student/questionnaires/{seeded['post_qid']}/submit",
        headers=headers[0],
        json={"response_stage": "post", "responses": _random_responses(seeded["post_item_ids"], rng)},
    )
    assert duplicate_post.status_code == 409

    teacher_status = teacher_client.get("/api/teacher/status/current", headers=teacher_headers)
    assert teacher_status.status_code == 200
    teacher_data = teacher_status.get_json()
    group_statuses = {
        row["group_id"]: row["group_discussion_status"]
        for row in teacher_data["group_discussions"]
    }
    assert group_statuses[group_id] in {"timed_out", "submitted"}

    pre_count = db.query_one(
        """
        SELECT COUNT(*) AS c FROM questionnaire_submissions
        WHERE questionnaire_id=? AND response_stage='pre' AND status='submitted'
        """,
        (seeded["pre_qid"],),
    )["c"]
    post_count = db.query_one(
        """
        SELECT COUNT(*) AS c FROM questionnaire_submissions
        WHERE questionnaire_id=? AND response_stage='post' AND status='submitted'
        """,
        (seeded["post_qid"],),
    )["c"]
    assert pre_count == 4
    assert post_count == 4
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_checkins WHERE group_id=?", (group_id,))["c"] == 5
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='student'", (group_id,))[
        "c"
    ] >= 8
