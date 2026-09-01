# -*- coding: utf-8 -*-
"""Structure and contract coverage for UI batch 4."""

import re


def _seed_running_lesson(db, *, group_id, session_no=44):
    now = db.now_str()
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE status='running'")
    task_id = db.execute(
        """
        INSERT INTO learning_tasks(
            title, question, task_goal, output_requirement,
            time_limit_minutes, created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            "Batch 4 task",
            "Discuss the best plan together.",
            "Reach a shared decision.",
            "Submit one group answer.",
            15,
            now,
        ),
    )
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (session_no, "discussion", task_id, "running", now, 15, now, now),
    )
    db.set_setting("current_session_id", str(session_id))
    db.set_setting("current_session_no", str(session_no))
    db.set_setting("current_task_id", str(task_id))
    return {"session_id": session_id, "task_id": task_id, "group_id": group_id}


def test_batch_4_collab_css_uses_student_workspace_layers(db_and_app):
    _, _, client = db_and_app
    response = client.get("/static/collab.css")

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    css = response.get_data(as_text=True)

    for selector in (
        ".student-workspace.collab-shell",
        ".student-workspace-header",
        ".student-header-meta",
        ".student-workspace.has-chat-panel",
        ".student-workspace.no-chat-panel",
        ".student-workspace.phase-pretest",
        ".student-workspace.phase-posttest",
        ".student-workspace.mode-waiting-task",
    ):
        assert selector in css

    for token in (
        "--ui-bg-soft",
        "--ui-bg-base",
        "--ui-bg-cool",
        "--ui-brand-900",
        "--ui-brand-600",
    ):
        assert f"var({token}" in css

    assert ".collab-body .bg-grid" in css
    assert "display: none" in css
    assert "@media (max-width: 1180px)" in css
    assert "@media (max-width: 1024px)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 768px)" in css
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)


def test_waiting_task_uses_single_main_area_without_empty_right_column(student_login):
    client, headers, _, _ = student_login
    response = client.get("/student/collab", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "collab-shell student-workspace ui-workspace phase-discussion mode-waiting-task no-chat-panel" in html
    assert 'class="student-workspace-header"' in html
    assert "等待教师发布任务" in html
    assert 'id="waitingTaskStatus"' in html
    assert 'id="collaborativeEditorArea"' in html
    assert 'id="editorStatusBadge"' in html
    assert 'id="rightPanel"' not in html
    assert 'id="messageInput"' not in html


def test_discussion_waiting_keeps_editor_mount_and_no_chat_gap(db_and_app, student_login):
    db, _, _ = db_and_app
    client, headers, user_id, group_id = student_login
    _seed_running_lesson(db, group_id=group_id)

    response = client.get("/student/collab?phase=discussion", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "mode-discussion-waiting no-chat-panel" in html
    assert "等待小组成员进入讨论" in html
    assert f"第 44 课次" in html
    assert 'id="collaborativeEditorArea"' in html
    assert 'id="editorStatusBadge"' in html
    assert 'id="rightPanel"' not in html
    assert "ensureDiscussionEntered()" in html


def test_pretest_uses_questionnaire_workspace_without_chat_contract(db_and_app, student_login):
    db, _, _ = db_and_app
    client, headers, _, group_id = student_login
    _seed_running_lesson(db, group_id=group_id)

    response = client.get("/student/collab?phase=pretest", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "phase-pretest mode-pre no-chat-panel" in html
    assert 'class="questionnaire-area" id="questionnaireArea"' in html
    assert html.count('id="questionnaireSummary"') == 1
    assert html.count('id="questionnaireFormBox"') == 1
    assert html.count('id="questionnaireCompletionBox"') == 1
    assert "/static/student/questionnaire.js" in html
    assert 'id="rightPanel"' not in html
    assert 'id="messageInput"' not in html


def test_phase_nav_marks_current_step_and_completed_shape(db_and_app, student_login):
    db, _, _ = db_and_app
    client, headers, _, group_id = student_login
    _seed_running_lesson(db, group_id=group_id)

    response = client.get("/student/collab?phase=pretest", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'aria-current="step"' in html
    assert '<span class="phase-indicator">' in html
    assert "phase-step active" in html
    assert 'class="phase-step completed"' not in html
    assert html.count('<span class="phase-indicator">1</span>') == 1
    assert html.count('<span class="phase-indicator">2</span>') == 1
    assert html.count('<span class="phase-indicator">3</span>') == 1
