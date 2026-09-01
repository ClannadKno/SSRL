# -*- coding: utf-8 -*-
"""Contract and structure tests for UI batch 2.

These tests use the existing isolated Flask/SQLite fixtures. They verify that
the visual migration does not alter login fields, route methods, redirects, or
teacher dashboard destinations.
"""

import re


BATCH_2_STYLESHEET = "/static/ui/auth-dashboard.css"
DASHBOARD_DESTINATIONS = (
    "/teacher/session/control",
    "/teacher/statistics",
    "/teacher/emotion-trend",
    "/teacher/export",
    "/teacher/questionnaire-admin",
    "/teacher/roster",
)


def test_batch_2_stylesheet_is_local_namespaced_and_resilient(db_and_app):
    _, _, client = db_and_app
    response = client.get(BATCH_2_STYLESHEET)

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    css = response.get_data(as_text=True)

    assert ".login-page" in css
    assert ".teacher-dashboard-page" in css
    assert ".login-main" in css
    assert ".dashboard-workspace" in css
    assert "var(--ui-radius-workspace)" not in css or ".ui-workspace" in css
    assert "@supports not" in css
    assert "rgba(255, 255, 255, 0.94)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert 'url("/static/pic/back.png")' in css
    assert "@media (max-width: 1023px)" in css
    for token in (
        "--ui-brand-700",
        "--ui-text-secondary",
        "--ui-border-soft",
        "--ui-radius-card",
        "--ui-shadow-card",
        "--ui-focus-ring",
    ):
        assert f"var({token})" in css
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)
    assert not re.search(r"(?m)^\s*--(?!ui-)[\w-]+\s*:", css)
    assert not re.search(r"(?m)^\s*(button|input|select|textarea|table)\s*[{,]", css)


def test_login_keeps_post_contract_and_uses_full_background_layout(db_and_app):
    _, app_module, client = db_and_app
    response = client.get("/login")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    login_rule = next(rule for rule in app_module.app.url_map.iter_rules() if rule.rule == "/login")

    assert {"GET", "POST"}.issubset(login_rule.methods)
    assert f'href="{BATCH_2_STYLESHEET}"' in html
    assert '<main class="login-main">' in html
    assert 'class="login-brand__icon"' in html
    assert 'src="/static/pic/icon.png"' in html
    assert 'class="login-panel"' in html
    assert 'd3190ff6a32a49e18d2ac4d4910b4416.png' not in html
    assert 'class="login-form" method="post"' in html
    assert 'id="login-key"' in html
    assert 'name="login_key"' in html
    assert 'placeholder="请输入专属实验密钥"' in html
    assert 'data-login-submit' in html
    assert 'aria-busy' in html
    assert "系统将自动识别你的实验课次、小组及成员身份" in html


def test_login_error_is_visible_without_changing_field_name(db_and_app):
    _, _, client = db_and_app
    response = client.post("/login", data={"login_key": "NOT-A-VALID-KEY"})

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="login-field__message login-field__message--error"' in html
    assert "密钥无效或已停用" in html
    assert 'name="login_key"' in html
    assert 'aria-invalid="true"' in html


def test_teacher_key_redirect_contract_remains_intact(teacher_login):
    client, headers = teacher_login
    teacher_page = client.get("/teacher", headers=headers)

    assert teacher_page.status_code == 200
    assert "teacher-status-shell ui-page-background" in teacher_page.get_data(as_text=True)


def test_student_key_redirect_contract_remains_intact(student_login):
    client, headers, _, _ = student_login
    student_page = client.get("/student/collab", headers=headers)

    assert student_page.status_code == 200
    assert "collab-body ui-page-background" in student_page.get_data(as_text=True)


def test_dashboard_preserves_all_entry_names_urls_and_navigation(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'href="{BATCH_2_STYLESHEET}"' in html
    assert '<main class="container teacher-dashboard-page">' in html
    assert 'class="dashboard-workspace ui-workspace"' in html
    assert html.count('class="module-card') == len(DASHBOARD_DESTINATIONS)
    assert html.count('class="module-entry"') == len(DASHBOARD_DESTINATIONS)
    assert html.count('role="link" tabindex="0"') == len(DASHBOARD_DESTINATIONS)
    assert html.count("onkeydown=") == len(DASHBOARD_DESTINATIONS)

    for destination in DASHBOARD_DESTINATIONS:
        assert f"location.href='{destination}'" in html

    for label in (
        "实验控制",
        "参与度统计",
        "情绪趋势",
        "历史查询与导出",
        "问卷管理",
        "花名册",
    ):
        assert label in html

    for icon_label in ("实验", "参与", "情绪", "导出", "问卷", "花名"):
        assert f'<div class="module-icon">{icon_label}</div>' in html

    assert "Agent 审计" not in html
    assert "location.href='/teacher/audit'" not in html
    assert not re.search(r'<div class="module-icon">T\d+</div>', html)

    assert "活跃学生数" not in html
    assert "今日讨论数" not in html
    assert "AI 得分" not in html


def test_dashboard_destinations_still_resolve_for_teacher(teacher_login):
    client, headers = teacher_login

    for destination in DASHBOARD_DESTINATIONS:
        response = client.get(destination, headers=headers)
        assert response.status_code == 200, destination
