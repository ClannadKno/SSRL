# -*- coding: utf-8 -*-
"""Structure and contract coverage for UI batch 5."""

import re

from tests.helpers import login_with_key, seed_running_session


def _discussion_page(db, client, *, member_count=1):
    seeded = seed_running_session(db, session_no=55, member_count=member_count, limit_minutes=10)
    user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)

    from services.group_discussion_runtime_service import enter_group_discussion_stage

    enter_group_discussion_stage(seeded["session_id"], seeded["group_id"], user_id)
    response = client.get("/student/collab?phase=discussion", headers=headers)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_batch_5_discussion_page_keeps_chat_layers_without_mid_checkin(db_and_app):
    db, _app, client = db_and_app
    html = _discussion_page(db, client)

    for fixed in (
        'id="rightPanel"',
        'id="chatBox"',
        'id="messageInput"',
        'id="helpStatus"',
        'id="aiLockHint"',
        'id="groupTimerBadge"',
        'id="collaborativeEditorArea"',
        'id="editorStatusBadge"',
    ):
        assert html.count(fixed) == 1

    assert 'class="collab-right discussion-panel" id="rightPanel"' in html
    assert 'class="ai-assistant-card"' in html
    assert 'class="messages-area conversation-card" id="chatBox"' in html
    assert '<details open>' in html
    assert '向学习助手求助' in html
    assert 'id="checkinToggle"' not in html
    assert 'id="checkinBody"' not in html
    assert 'id="emotionOptions"' not in html
    assert 'id="checkinNote"' not in html
    assert '提交打卡' not in html


def test_discussion_header_omits_duplicate_timer_but_task_details_keep_it(db_and_app):
    db, _app, client = db_and_app
    html = _discussion_page(db, client)

    header = re.search(
        r'<header class="student-workspace-header">(.*?)</header>', html, re.S
    )
    assert header is not None
    assert "讨论剩余" not in header.group(1)
    assert "讨论计时" not in header.group(1)
    assert 'id="groupTimerBadge"' in html


def test_batch_5_discussion_script_keeps_chat_api_contracts(db_and_app):
    db, _app, client = db_and_app
    html = _discussion_page(db, client)

    for api in (
        "'/api/message'",
        "'/api/student/help'",
        '"/api/student/sync?"',
        "'/api/discussion/enter'",
    ):
        assert api in html

    assert "window.sendMessage = sendMessage" in html
    assert "window.requestHelp = requestHelp" in html
    assert "checkin_type: 'mid'" not in html
    assert "window.submitCheckin = submitCheckin" not in html
    assert "input.readOnly = locked" in html
    assert "/api/group/" not in html
    assert "/api/rooms/" not in html
    assert "setInterval(" not in html


def test_batch_5_post_checkin_keeps_emotion_icons_and_post_contract(db_and_app):
    _db, _app, client = db_and_app
    script = client.get("/static/student/questionnaire.js").get_data(as_text=True)

    for option in ("🙂 平稳", "🧭 卡住", "⚡ 分歧", "… 沉默", "△ 挫败"):
        assert option in script
    assert "checkin_type: 'post'" in script
    assert "fetch('/api/checkin'" in script


def test_batch_5_message_rendering_keeps_message_classes_and_agent_marker(db_and_app):
    db, _app, client = db_and_app
    html = _discussion_page(db, client)

    assert "const isSelf = m.user_id === CURRENT_USER_ID && m.role !== 'agent';" in html
    assert "const isAgent = m.role === 'agent';" in html
    assert "const isSystem = m.role === 'system' || m.role === 'teacher';" in html
    assert "const cls = isSelf ? 'self' : (isAgent ? 'agent' : (isSystem ? 'system' : ''));" in html
    assert "const roleLabel = isAgent ? 'SERA助手'" in html
    assert "'<div class=\"msg-item ' + cls + '\" data-mid=\"' + m.id + '\">'" in html
    assert "escapeHtml(m.content)" in html


def test_batch_5_css_scopes_discussion_components_and_reduced_motion(db_and_app):
    _db, _app, client = db_and_app
    response = client.get("/static/collab.css")

    assert response.status_code == 200
    css = response.get_data(as_text=True)

    for selector in (
        ".student-workspace.mode-discussion .task-brief-panel",
        ".discussion-panel.collab-right",
        ".ai-assistant-card",
        ".conversation-card.messages-area",
        ".msg-item.agent .msg-bubble",
        ".chat-input-row textarea.ai-locked",
        ".checkin-panel",
        ".ci-emotion-btn.active",
        ".usage-tips summary",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert selector in css

    assert "overflow-wrap: anywhere" in css
    assert "var(--ui-brand-900" in css
    assert "var(--ui-text-strong" in css
    assert "var(--student-lock-surface)" in css
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)


def test_batch_5_waiting_and_questionnaire_pages_do_not_render_discussion_right_panel(db_and_app, student_login):
    db, _app, _client = db_and_app
    client, headers, _user_id, group_id = student_login
    waiting = client.get("/student/collab", headers=headers).get_data(as_text=True)
    assert 'id="rightPanel"' not in waiting
    assert 'id="checkinToggle"' not in waiting

    seed_running_session(db, session_no=56, member_count=1, limit_minutes=10)
    pretest = client.get("/student/collab?phase=pretest", headers=headers).get_data(as_text=True)
    assert 'id="rightPanel"' not in pretest
    assert 'id="messageInput"' not in pretest
    assert 'id="checkinToggle"' not in pretest
    assert str(group_id) in pretest or "questionnaireArea" in pretest
