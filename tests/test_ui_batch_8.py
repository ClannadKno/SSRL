# -*- coding: utf-8 -*-
"""Contract coverage for UI batch 8 teacher operations pages."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS_STYLESHEET = "/static/ui/teacher-operations.css"
EXPORT_LINKS = (
    "/export/messages",
    "/export/state-assessments",
    "/export/strategy-pipeline",
    "/export/interventions",
    "/export/participation",
    "/export/emotion-checkins",
    "/export/emotion-feedback",
    "/export/help-requests",
    "/export/deliverables",
    "/export/questionnaires",
    "/export/all",
)


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_batch_8_stylesheet_is_local_scoped_and_resilient(db_and_app):
    _, _, client = db_and_app
    response = client.get(OPERATIONS_STYLESHEET)

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    css = response.get_data(as_text=True)

    for selector in (
        ".teacher-operations-page",
        ".ops-page-header",
        ".ops-workspace",
        ".ops-section-grid-two",
        ".ops-section-card",
        ".ops-toolbar-card",
        ".ops-table-scroll",
        ".ops-data-table",
        ".ops-export-grid",
        ".ops-export-links",
    ):
        assert selector in css

    for token in (
        "--ui-glass-workspace",
        "--ui-radius-workspace",
        "--ui-radius-card",
        "--ui-shadow-card",
        "--ui-border-soft",
        "--ui-text-strong",
        "--ui-brand-700",
    ):
        assert f"var({token}" in css

    assert "@supports not" in css
    assert "rgba(255, 255, 255, 0.94)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 768px)" in css
    assert "overflow-x: auto" in css
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)
    assert not re.search(r"(?m)^\s*(button|input|select|textarea|table)\s*[{,]", css)


def test_session_control_uses_operations_workspace_and_keeps_fixed_ids(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/session/control", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f'href="{OPERATIONS_STYLESHEET}"' in html
    assert '<main class="container teacher-operations-page session-control-page">' in html
    assert 'class="ops-workspace ui-workspace"' in html
    assert 'class="card ops-section-card ops-status-card"' in html
    assert 'class="ops-section-grid ops-section-grid-two"' in html
    assert "/static/teacher/session-control.js?v=20260804_1" in html
    assert html.count('class="topbar teacher-page-nav"') == 1

    for control_id in (
        "currentStatusBar",
        "currentTaskSummary",
        "createSessionTitle",
        "createSessionDescription",
        "createSessionNo",
        "createTaskId",
        "createQuestionnaireSetId",
        "createAgentModeNone",
        "createAgentModeStrategy",
        "createAgentModeEmotion",
        "createSessionStatus",
        "taskFormTitle",
        "taskTitle",
        "taskExperimentPhaseId",
        "taskTimeLimitMinutes",
        "taskBackground",
        "taskBrief",
        "taskSurveyItems",
        "taskSurveyNote",
        "taskOptions",
        "taskBudgetTotal",
        "taskBudgetUnit",
        "taskBudgetMinSelected",
        "taskConstraints",
        "taskDiscussionQuestions",
        "taskSubmissionRequirements",
        "taskPreSubmitChecklist",
        "taskAdminStatus",
        "sessionList",
        "taskListSummary",
        "taskCards",
    ):
        assert html.count(f'id="{control_id}"') == 1


def test_session_control_javascript_keeps_api_and_action_contracts():
    js = _read("static/teacher/session-control.js")

    for endpoint in (
        "/api/teacher/session/status",
        "/api/teacher/sessions",
        "/api/teacher/session/create",
        "/api/teacher/session/start",
        "/api/teacher/session/end",
        "/api/teacher/session/archive",
        "/api/teacher/tasks",
        "/api/teacher/task/assign",
        "/api/teacher/questionnaire-sets",
        "/api/teacher/experiment-phases",
    ):
        assert endpoint in js

    for symbol in (
        "loadCurrentStatus",
        "createSession",
        "startSession",
        "endSession",
        "archiveSession",
        "deleteSession",
        "editSession",
        "saveSessionEdit",
        "createTask",
        "saveTaskEdit",
        "deleteTask",
    ):
        assert f"function {symbol}" in js


def test_roster_uses_table_workspace_and_preserves_api_export_contract(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/roster", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f'href="{OPERATIONS_STYLESHEET}"' in html
    assert '<main class="container teacher-operations-page roster-management-page">' in html
    assert 'class="ops-toolbar-card"' in html
    assert 'class="card ops-table-card"' in html
    assert 'class="ops-table-scroll" style="display:none"' in html
    assert 'class="ops-data-table roster-table"' in html
    assert html.count('id="rosterLoading"') == 1
    assert html.count('id="rosterTable"') == 1
    assert html.count('id="rosterBody"') == 1
    assert "fetchJSON('/api/teacher/roster')" in html
    assert 'onclick="loadRoster()"' in html
    assert 'href="/export/roster.csv"' in html
    assert "用户名" in html and "真实姓名" in html and "实验条件" in html

    api_response = client.get("/api/teacher/roster", headers=headers)
    assert api_response.status_code == 200
    assert "users" in api_response.get_json()


def test_export_page_uses_full_scope_research_links(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/export", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f'href="{OPERATIONS_STYLESHEET}"' in html
    assert '<main class="container teacher-operations-page export-center-page">' in html
    assert 'class="card ops-export-card"' in html
    assert 'class="ops-export-grid"' in html
    assert 'class="export-links ops-export-links"' in html
    assert 'class="btn small ops-primary-download" href="/export/all"' in html
    assert "全部课次和全部小组" in html
    assert "全量、非盲化研究数据" in html

    for href in EXPORT_LINKS:
        assert f'href="{href}"' in html


def test_batch_8_routes_remain_get_only_and_teacher_protected(db_and_app):
    _, app_module, _ = db_and_app
    rules = {
        rule.rule: rule
        for rule in app_module.app.url_map.iter_rules()
        if rule.rule in {"/teacher/session/control", "/teacher/roster", "/teacher/export"}
    }

    assert set(rules) == {"/teacher/session/control", "/teacher/roster", "/teacher/export"}
    for rule in rules.values():
        assert "GET" in rule.methods
        assert "POST" not in rule.methods
