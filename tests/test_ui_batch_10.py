# -*- coding: utf-8 -*-
"""Contract coverage for UI batch 10 final teacher/restricted pages."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FINAL_STYLESHEET = "/static/ui/teacher-final.css"
QM_JS = "/static/teacher/questionnaire-management.js"


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_batch_10_stylesheet_is_scoped_local_and_resilient(db_and_app):
    _, _, client = db_and_app
    response = client.get(FINAL_STYLESHEET)

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    css = response.get_data(as_text=True)

    for selector in (
        ".teacher-final-page",
        ".final-page-header",
        ".final-workspace",
        ".final-tabs",
        ".final-table-scroll",
        ".qm-detail-modal",
        ".restricted-state-shell",
        ".restricted-state-card",
    ):
        assert selector in css

    for token in (
        "--ui-glass-workspace",
        "--ui-radius-workspace",
        "--ui-radius-card",
        "--ui-shadow-workspace",
        "--ui-border-soft",
        "--ui-text-strong",
        "--ui-brand-600",
    ):
        assert f"var({token}" in css

    assert "@supports not" in css
    assert "rgba(255, 255, 255, 0.94)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 768px)" in css
    assert "overflow-x: auto" in css
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)
    assert not re.search(r"(?m)^\s*(button|input|select|textarea|table)\s*[{,]", css)


def test_register_disabled_uses_restricted_state_without_enabling_registration(db_and_app):
    _, app_module, client = db_and_app
    response = client.get("/register")

    assert response.status_code == 404
    html = response.get_data(as_text=True)
    register_rule = next(rule for rule in app_module.app.url_map.iter_rules() if rule.rule == "/register")

    assert {"GET", "POST"}.issubset(register_rule.methods)
    assert '<body class="ui-page-background ui-bg-auth">' in html
    assert 'class="restricted-state-shell"' in html
    assert 'class="restricted-state-card"' in html
    assert f'href="{FINAL_STYLESHEET}"' in html
    assert "学生注册已禁用" in html
    assert "返回登录" in html


def test_legacy_questionnaire_admin_uses_final_workspace_and_keeps_contract(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/questionnaire-admin", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f'href="{FINAL_STYLESHEET}"' in html
    assert 'class="container teacher-final-page questionnaire-admin-page"' in html
    assert 'class="final-workspace ui-workspace"' in html
    assert html.count('id="questionnaireFormTitle"') == 1
    assert html.count('id="questionnaireCards"') == 1
    assert html.count('id="questionnaireSetCards"') == 1
    assert "window.fetchJSON('/api/teacher/questionnaires')" in html
    assert "window.fetchJSON('/api/teacher/questionnaire-sets?include_inactive=1')" in html
    assert "createQuestionnaire()" in html
    assert "saveQuestionnaireEdit()" in html
    assert "deleteQ(" in html


def test_questionnaire_management_uses_external_fixed_js_without_api_changes(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/questionnaire-management", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f'href="{FINAL_STYLESHEET}"' in html
    assert f'src="{QM_JS}"' in html
    assert 'class="container teacher-final-page questionnaire-management-page"' in html
    assert 'role="tablist" aria-label="问卷管理视图"' in html
    assert 'id="qmTabFixed"' in html
    assert 'id="pubQSelect"' in html
    assert 'id="pubSessionSelect"' in html
    assert 'id="pubStageSelect"' in html
    assert 'id="exportSessionSelect"' in html
    assert 'id="legacy-questionnaire-management-script-disabled"' in html
    assert '<script>\nlet qmData' not in html


def test_questionnaire_management_js_preserves_endpoints_and_global_handlers():
    js = _read("static/teacher/questionnaire-management.js")

    for endpoint in (
        "/api/teacher/questionnaires/fixed",
        "/api/teacher/sessions",
        "/api/teacher/questionnaire-publications",
        "/api/teacher/questionnaire-completion",
        "/api/teacher/questionnaire-raw-export?",
    ):
        assert endpoint in js

    for symbol in (
        "switchQmTab",
        "createPublication",
        "togglePub",
        "deletePub",
        "loadCompletion",
        "doExportQuestionnaireRaw",
        "viewFixedDetail",
    ):
        assert f"window.{symbol} =" in js

    assert "ui-error-state" in js
    assert "renderError" in js
    assert "fetch(" in js
    assert not re.search(r"@import|https?://", js, re.I)


def test_interventions_page_uses_shared_fetch_tool_and_truthful_error_state(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/interventions", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f'href="{FINAL_STYLESHEET}"' in html
    assert 'src="/static/teacher/teacher-api.js"' in html
    assert 'class="container teacher-final-page interventions-page"' in html
    assert 'id="interventionList"' in html
    assert "fetchJSON('/api/teacher/interventions')" in html
    assert "ui-empty-state" in html
    assert "ui-error-state" in html
    assert "加载失败：" in html


def test_batch_10_does_not_expose_hidden_pages_on_dashboard_or_add_backend_api(db_and_app):
    _, app_module, _ = db_and_app
    rules = {
        rule.rule: rule
        for rule in app_module.app.url_map.iter_rules()
        if rule.rule in {
            "/teacher/questionnaire-admin",
            "/teacher/questionnaire-management",
            "/teacher/interventions",
            "/api/teacher/interventions",
        }
    }

    assert "/teacher/questionnaire-admin" in rules
    assert "/teacher/questionnaire-management" in rules
    assert "/teacher/interventions" in rules
    assert "/api/teacher/interventions" not in rules

    for route in ("/teacher/questionnaire-admin", "/teacher/questionnaire-management", "/teacher/interventions"):
        assert "GET" in rules[route].methods
        assert "POST" not in rules[route].methods


def test_dashboard_still_links_only_existing_questionnaire_entry(teacher_login):
    client, headers = teacher_login
    html = client.get("/teacher", headers=headers).get_data(as_text=True)

    assert "location.href='/teacher/questionnaire-admin'" in html
    assert "location.href='/teacher/questionnaire-management'" not in html
    assert "location.href='/teacher/interventions'" not in html
