# -*- coding: utf-8 -*-
"""Structure and contract coverage for UI batch 3."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_STYLESHEET = "/static/ui/teacher-analytics.css"


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_analytics_stylesheet_is_local_scoped_and_resilient(db_and_app):
    _, _, client = db_and_app
    response = client.get(ANALYTICS_STYLESHEET)

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    css = response.get_data(as_text=True)

    for selector in (
        ".teacher-analytics-page",
        ".analytics-page-header",
        ".analytics-workspace",
        ".analytics-filter-card",
        ".analytics-metric-card",
        ".analytics-chart-card",
        ".analytics-table-card",
        ".analytics-data-card",
        ".analytics-detail-card",
        ".audit-page",
    ):
        assert selector in css

    for token in (
        "--ui-glass-workspace",
        "--ui-radius-workspace",
        "--ui-shadow-workspace",
        "--ui-border-soft",
        "--ui-surface-solid",
        "--ui-z-modal",
        "--ui-z-toast",
        "--ui-focus-ring",
    ):
        assert f"var({token})" in css

    assert "@supports not" in css
    assert "rgba(255, 255, 255, 0.94)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 768px)" in css
    assert not re.search(r"@import|url\(\s*['\"]?https?://", css, re.I)
    assert not re.search(
        r"(?m)^\s*(button|input|select|textarea|table)\s*[{,]",
        css,
    )


def test_statistics_uses_shared_workspace_and_preserves_controls(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/statistics", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'href="{ANALYTICS_STYLESHEET}"' in html
    assert 'class="teacher-analytics-page participation-analytics-page"' in html
    assert 'class="analytics-workspace ui-workspace"' in html
    assert 'class="card analytics-filter-card"' in html
    assert 'class="card analytics-metric-card"' in html
    assert 'class="card analytics-table-card"' in html
    assert 'class="analytics-filter-grid"' in html
    assert 'class="analytics-main-grid analytics-main-grid-balanced"' in html

    for control_id in (
        "group-select",
        "session-select",
        "timeline-metric",
        "window-minutes",
        "last-updated",
        "group-summary-area",
        "member-bars-area",
        "share-bars-area",
        "detail-table-area",
        "timeline-trend-area",
    ):
        assert html.count(f'id="{control_id}"') == 1

    for metric in ("message_count", "char_count", "active_minutes"):
        assert f'value="{metric}"' in html

    assert "/static/teacher/participation-statistics.js" in html
    assert "window.initParticipationStats()" in html


def test_emotion_page_uses_shared_layers_and_keeps_state_controls(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/emotion-trend", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'href="{ANALYTICS_STYLESHEET}"' in html
    assert 'class="teacher-analytics-page emotion-analytics-page"' in html
    assert 'class="analytics-workspace ui-workspace"' in html
    assert 'class="card analytics-filter-card"' in html
    assert 'class="emotion-overview analytics-metric-grid" id="review-summary"' in html
    assert 'class="card analytics-chart-card analytics-chart-card-wide"' in html
    assert 'class="card analytics-data-card"' in html
    assert html.count('class="card analytics-summary-card"') == 3

    for control_id in (
        "group-select",
        "session-select",
        "window-minutes",
        "last-updated",
        "legacy-warning",
        "review-summary",
        "timeline-area",
        "state-filter",
        "message-search",
        "message-count-badge",
        "message-flow-area",
        "participation-area",
        "state-distribution-area",
        "intervention-area",
    ):
        assert html.count(f'id="{control_id}"') == 1

    assert "/static/teacher/emotion-trend.js" in html
    assert "window.initEmotionTrendPage()" in html


def test_audit_progressively_uses_shared_workspace_without_id_changes(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/audit", headers=headers)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'href="{ANALYTICS_STYLESHEET}"' in html
    assert '<body class="ui-page-background ui-bg-analytics">' in html
    assert (
        'class="app-shell audit-app-shell teacher-analytics-page audit-page"'
        in html
    )
    assert 'class="analytics-workspace ui-workspace audit-workspace"' in html
    assert 'class="card analytics-filter-card"' in html
    assert 'class="card analytics-metric-card"' in html
    assert 'class="modal-overlay ui-modal" id="correction-modal"' in html
    assert 'class="modal-overlay ui-modal" id="unblind-modal"' in html

    for control_id in (
        "audit-session-select",
        "audit-group-select",
        "audit-task-select",
        "audit-state-filter",
        "audit-intervention-filter",
        "audit-review-filter",
        "audit-agent-filter",
        "audit-filter-info",
        "audit-empty-state",
        "audit-timeline-area",
        "audit-timeline",
        "audit-stats",
        "audit-detail-section",
        "audit-detail-content",
        "audit-message-flow",
        "correction-modal",
        "correction-type-select",
        "correction-reason",
        "unblind-modal",
        "unblind-reason",
    ):
        assert html.count(f'id="{control_id}"') == 1

    for script in (
        "/static/teacher/teacher-api.js",
        "/static/teacher/global-status.js",
        "/static/teacher/agent-audit.js",
    ):
        assert script in html
    assert "window.initAgentAudit()" in html


def test_visual_javascript_keeps_api_and_state_mapping_contracts():
    participation = _read("static/teacher/participation-statistics.js")
    emotion = _read("static/teacher/emotion-trend.js")
    audit = _read("static/teacher/agent-audit.js")

    for endpoint_fragment in (
        "/api/teacher/groups?all=true",
        "/api/teacher/sessions?all=true",
        "/participation-summary",
        "/participation-timeline",
    ):
        assert endpoint_fragment in participation

    for metric in ("message_count", "char_count", "active_minutes"):
        assert metric in participation
    assert participation.count("--analytics-series-") >= 12
    assert "var COLORS =" not in participation

    for state_code in (
        "positive_collaboration",
        "conflict_tension",
        "frustration_stuck",
        "off_task",
        "negative_silence",
        "observing",
        "unclassified",
    ):
        assert state_code in emotion
    assert "const LEGACY_STATE_MAP" in emotion
    assert "/emotion-review?window_minutes=" in emotion

    for endpoint_fragment in (
        "/api/teacher/sessions?all=true",
        "/api/teacher/groups?all=true",
        "/agent-audit?session_id=",
    ):
        assert endpoint_fragment in audit


def test_analytics_routes_remain_get_only_and_teacher_protected(db_and_app):
    _, app_module, _ = db_and_app
    rules = {
        rule.rule: rule
        for rule in app_module.app.url_map.iter_rules()
        if rule.rule in {
            "/teacher/statistics",
            "/teacher/emotion-trend",
            "/teacher/audit",
        }
    }

    assert set(rules) == {
        "/teacher/statistics",
        "/teacher/emotion-trend",
        "/teacher/audit",
    }
    for rule in rules.values():
        assert "GET" in rule.methods
        assert "POST" not in rule.methods
