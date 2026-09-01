# -*- coding: utf-8 -*-
"""Batch 1 regression tests for the shared Soft Glass design foundation."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "static" / "ui"
UI_LINKS = (
    "/static/ui/design-tokens.css",
    "/static/ui/ui-primitives.css",
    "/static/ui/ui-motion.css",
)


def _read(name):
    return (UI_DIR / name).read_text(encoding="utf-8")


def _assert_ui_links(html):
    positions = [html.index(f'href="{href}"') for href in UI_LINKS]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(UI_LINKS)


def _relative_luminance(hex_color):
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]

    def linearize(channel):
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linearize(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first, second):
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_design_tokens_include_required_layers_and_legacy_bridge():
    css = _read("design-tokens.css")
    required_tokens = {
        "--ui-bg-base",
        "--ui-bg-soft",
        "--ui-bg-cool",
        "--ui-bg-warm",
        "--ui-text-strong",
        "--ui-text-primary",
        "--ui-text-secondary",
        "--ui-text-muted",
        "--ui-brand-900",
        "--ui-brand-800",
        "--ui-brand-700",
        "--ui-brand-600",
        "--ui-glass-workspace",
        "--ui-glass-card",
        "--ui-glass-control",
        "--ui-surface-solid",
        "--ui-surface-soft",
        "--ui-border-glass",
        "--ui-border-soft",
        "--ui-border-strong",
        "--ui-radius-workspace",
        "--ui-radius-card",
        "--ui-radius-panel",
        "--ui-radius-control",
        "--ui-shadow-workspace",
        "--ui-shadow-card",
        "--ui-shadow-control",
        "--ui-shadow-hover",
        "--ui-blur-workspace",
        "--ui-blur-card",
        "--ui-blur-control",
        "--ui-space-48",
        "--ui-space-56",
        "--ui-z-base",
        "--ui-z-sticky",
        "--ui-z-dropdown",
        "--ui-z-overlay",
        "--ui-z-modal",
        "--ui-z-toast",
    }
    assert not [token for token in required_tokens if token not in css]

    for legacy in (
        "--primary",
        "--primary-hover",
        "--bg",
        "--text",
        "--text-secondary",
        "--text-muted",
        "--radius",
        "--radius-sm",
        "--radius-lg",
        "--shadow",
        "--paper-bg",
        "--ink",
        "--accent",
    ):
        assert re.search(rf"{re.escape(legacy)}:\s*var\(--ui-", css)


def test_primitives_are_namespaced_and_accessible():
    css = _read("ui-primitives.css")
    for class_name in (
        "ui-page-background",
        "ui-workspace",
        "ui-glass-card",
        "ui-solid-card",
        "ui-panel",
        "ui-toolbar",
        "ui-field",
        "ui-input",
        "ui-select",
        "ui-button",
        "ui-button-primary",
        "ui-button-secondary",
        "ui-button-danger",
        "ui-badge",
        "ui-status-badge",
        "ui-modal",
        "ui-empty-state",
        "ui-error-state",
        "ui-loading-state",
    ):
        assert f".{class_name}" in css

    assert "@supports not" in css
    assert "rgba(255, 255, 255, 0.94)" in css
    assert ":focus-visible" in css
    assert "pointer-events: none" in css
    assert not re.search(r"(?m)^\s*(button|input|select|textarea|table)\s*[{,]", css)


def test_motion_has_reduced_motion_fallback():
    css = _read("ui-motion.css")
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transform: none" in css
    assert "animation-duration: 0.01ms !important" in css


def test_small_text_tokens_meet_contrast_target():
    for text_color in ("#5e6f87", "#62738c"):
        assert _contrast_ratio(text_color, "#f7f9ff") >= 4.5
        assert _contrast_ratio(text_color, "#ffffff") >= 4.5


def test_public_login_and_static_stylesheets_are_connected(db_and_app):
    _, _, client = db_and_app
    response = client.get("/login")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    _assert_ui_links(html)
    assert '<body class="ui-page-background ui-bg-auth">' in html

    for href in UI_LINKS:
        css_response = client.get(href)
        assert css_response.status_code == 200
        assert css_response.mimetype == "text/css"
        assert css_response.get_data(as_text=True).strip()


def test_teacher_shell_routes_and_standalone_audit_are_connected(teacher_login):
    client, headers = teacher_login
    for route in (
        "/teacher",
        "/teacher/session/control",
        "/teacher/statistics",
        "/teacher/emotion-trend",
    ):
        response = client.get(route, headers=headers)
        assert response.status_code == 200, route
        html = response.get_data(as_text=True)
        _assert_ui_links(html)
        assert "teacher-status-shell ui-page-background" in html

    audit = client.get("/teacher/audit", headers=headers)
    assert audit.status_code == 200
    audit_html = audit.get_data(as_text=True)
    _assert_ui_links(audit_html)
    assert '<body class="ui-page-background ui-bg-analytics">' in audit_html


def test_student_shell_is_connected_without_changing_mount_contract(student_login):
    client, headers, _, _ = student_login
    response = client.get("/student/collab", headers=headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    _assert_ui_links(html)
    assert "collab-body ui-page-background ui-bg-workspace" in html
    assert 'class="collab-shell' in html
    assert "collaborativeEditorArea" in html
