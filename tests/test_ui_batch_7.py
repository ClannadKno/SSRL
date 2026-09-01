# -*- coding: utf-8 -*-
"""Contract coverage for UI batch 7 student questionnaire workspace."""

from datetime import datetime
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _seed_questionnaire_lesson(db, group_id, *, session_no=47):
    now = db.now_str()
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE status='running'")
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Batch 7 questionnaire task", "Answer the questionnaire.", 12, now),
    )
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (session_no, "discussion", task_id, "running", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 12, now, now),
    )
    db.set_setting("current_session_id", str(session_id))
    db.set_setting("current_session_no", str(session_no))
    db.set_setting("current_task_id", str(task_id))
    qid = db.create_questionnaire(
        {
            "code": "BATCH7_PRE",
            "title": "Batch 7 mixed questionnaire",
            "timing": "pre",
            "scale_max": 7,
            "active": True,
            "instruction_pre": "Complete every required item.",
        },
        items=[
            {
                "item_code": "SINGLE",
                "prompt_text": "Pick one option",
                "question_type": "single_choice",
                "options": [{"key": "A", "label": "Alpha"}, {"key": "B", "label": "Beta"}],
                "section_no": 1,
                "section_title": "Choice",
                "sort_order": 1,
            },
            {
                "item_code": "MULTI",
                "prompt_text": "Pick multiple options",
                "question_type": "multiple_choice",
                "options": [{"key": "M1", "label": "One"}, {"key": "M2", "label": "Two"}],
                "section_no": 1,
                "section_title": "Choice",
                "sort_order": 2,
            },
            {
                "item_code": "TEXT",
                "prompt_text": "Explain your answer",
                "question_type": "text",
                "max_value": 120,
                "section_no": 1,
                "section_title": "Choice",
                "sort_order": 3,
            },
            {
                "item_code": "LIKERT5",
                "prompt_text": "Rate from one to five",
                "question_type": "likert_5",
                "max_value": 5,
                "section_no": 1,
                "section_title": "Choice",
                "sort_order": 4,
            },
            {
                "item_code": "LIKERT7",
                "prompt_text": "Rate from one to seven",
                "question_type": "likert_7",
                "max_value": 7,
                "scale_labels": ["1", "2", "3", "4", "5", "6", "7"],
                "section_no": 1,
                "section_title": "Choice",
                "sort_order": 5,
            },
            {
                "item_code": "MATRIX1",
                "prompt_text": "Matrix row one",
                "question_type": "matrix_likert",
                "max_value": 5,
                "scale_labels": ["Strongly disagree", "Disagree", "Neutral", "Agree", "Strongly agree"],
                "section_no": 2,
                "section_title": "Scale Header",
                "sort_order": 6,
            },
        ],
    )
    db.create_questionnaire_publication(qid, session_id, session_no, "pre", group_id=group_id)
    rows = db.query_all("SELECT item_code, id FROM questionnaire_items WHERE questionnaire_id=?", (qid,))
    return {
        "qid": qid,
        "session_id": session_id,
        "item_ids": {row["item_code"]: row["id"] for row in rows},
    }


def test_batch_7_questionnaire_html_keeps_fixed_ids_and_uses_two_column_workspace(db_and_app, student_login):
    db, _, _ = db_and_app
    client, headers, _, group_id = student_login
    _seed_questionnaire_lesson(db, group_id)

    response = client.get("/student/collab?phase=pretest", headers=headers)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'class="questionnaire-area" id="questionnaireArea"' in html
    assert 'class="questionnaire-workspace"' in html
    assert 'class="qa-nav-region" aria-label="问卷导航"' in html
    assert 'class="qa-content-region" aria-label="问卷题目"' in html
    assert html.count('id="questionnaireSummary"') == 1
    assert html.count('id="questionnaireFormBox"') == 1
    assert html.count('id="questionnaireCompletionBox"') == 1
    assert 'id="rightPanel"' not in html
    assert 'id="messageInput"' not in html


def test_batch_7_css_scopes_soft_glass_questionnaire_without_global_controls():
    css = _read("static/student/questionnaire.css")

    for selector in (
        ".questionnaire-workspace",
        ".qa-nav-region",
        ".qa-content-region",
        ".qa-progress-card",
        ".qa-list-btn.active",
        ".qa-paper",
        ".qa-matrix-scroll",
        ".qa-submit-area",
    ):
        assert selector in css

    assert "grid-template-columns: minmax(240px, 280px) minmax(0, 1fr)" in css
    assert "overflow-x: auto" in css
    assert "@media (max-width: 900px)" in css
    assert "@supports not ((-webkit-backdrop-filter: blur(1px))" in css
    assert "var(--ui-brand-900" in css
    assert "var(--ui-border-soft" in css
    assert not re.search(r"(^|\n)\s*(button|input|textarea|select|table)\s*\{", css)
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)


def test_batch_7_js_preserves_submission_contract_and_supports_required_question_types():
    js = _read("static/student/questionnaire.js")

    for symbol in (
        "textQuestionHTML",
        "singleChoiceHTML",
        "multipleChoiceHTML",
        "likertHTML",
        "matrixLikertSectionHTML",
        "renderPostCheckinQuestionnaire",
        "renderQuestionnaireNav",
    ):
        assert f"function {symbol}" in js

    assert "qtype === 'multiple_choice'" in js
    assert "itemType === 'matrix_likert'" in js
    assert "response_stage: currentStage" in js
    assert "responses: responses" in js
    assert "/api/student/questionnaires/' + q.id + '/responses" in js
    assert "fetch('/api/checkin'" in js
    assert "checkin_type: 'post'" in js
    assert "option_keys: selectedOptions" in js


def test_batch_7_questionnaire_api_request_body_shape_remains_accepted(db_and_app, student_login):
    db, _, _ = db_and_app
    client, headers, _, group_id = student_login
    seeded = _seed_questionnaire_lesson(db, group_id)
    item_ids = seeded["item_ids"]

    listed = client.get("/api/student/questionnaires?stage=pre", headers=headers)
    assert listed.status_code == 200
    payload = listed.get_json()
    assert payload["status"] == "ok"
    assert payload["questionnaires"][0]["id"] == seeded["qid"]

    response = client.post(
        f"/api/student/questionnaires/{seeded['qid']}/responses",
        headers=headers,
        json={
            "response_stage": "pre",
            "responses": {
                str(item_ids["SINGLE"]): {"option_key": "A"},
                str(item_ids["MULTI"]): {"option_keys": ["M1", "M2"]},
                str(item_ids["TEXT"]): {"text": "Short answer"},
                str(item_ids["LIKERT5"]): 4,
                str(item_ids["LIKERT7"]): 6,
                str(item_ids["MATRIX1"]): 5,
            },
        },
    )

    assert response.status_code == 200
    rows = db.query_all(
        "SELECT item_id, response_value, response_text, response_option_key FROM questionnaire_responses WHERE questionnaire_id=?",
        (seeded["qid"],),
    )
    assert len(rows) == 6
    assert db.query_one(
        "SELECT status FROM questionnaire_submissions WHERE questionnaire_id=?",
        (seeded["qid"],),
    )["status"] == "submitted"
