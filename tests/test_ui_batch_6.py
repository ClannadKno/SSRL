# -*- coding: utf-8 -*-
"""Contract coverage for UI batch 6 collaborative editor theme bridge."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
EDITOR_SRC = ROOT / "frontend" / "collaborative-editor" / "src"


def _read_source(relative_path):
    return (EDITOR_SRC / relative_path).read_text(encoding="utf-8")


def test_batch_6_editor_css_defines_namespace_tokens_and_fallback_bridge():
    css = _read_source("styles/editor.css")

    for token in (
        "--editor-bg",
        "--editor-surface",
        "--editor-surface-toolbar",
        "--editor-border",
        "--editor-text",
        "--editor-text-muted",
        "--editor-focus",
        "--editor-shadow",
        "--editor-radius",
        "--editor-user-1",
        "--editor-user-6",
    ):
        assert token in css

    assert "--editor-bg: var(--ui-surface-soft, #f8faff)" in css
    assert "--editor-focus: var(--ui-brand-600, #246da5)" in css
    assert "--paper-bg: var(--editor-bg)" in css
    assert "--ink: var(--editor-text)" in css
    assert "--accent: var(--editor-focus)" in css


def test_batch_6_editor_surface_keeps_readable_solid_content_and_glass_outer_layer():
    css = _read_source("styles/editor.css")

    assert ".collab-editor-surface" in css
    assert "border-radius: var(--editor-radius)" in css
    assert "box-shadow: var(--editor-shadow)" in css
    assert re.search(r"#tiptap-editor\s*{[^}]*background:\s*var\(--editor-surface\)", css, re.S)
    assert re.search(r"\.tiptap-content\s*{[^}]*background:\s*var\(--editor-surface\)", css, re.S)
    assert "caret-color: var(--editor-focus)" in css
    assert "backdrop-filter: blur(14px)" in css
    assert "@supports not ((backdrop-filter: blur(1px))" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_batch_6_toolbar_status_and_dialogs_use_editor_tokens():
    css = _read_source("styles/editor.css")
    connection = _read_source("components/ConnectionStatus.vue")

    for selector in (
        ".tiptap-toolbar",
        ".tiptap-btn.active",
        ".umo-link-overlay",
        ".umo-toast",
        ".online-members",
        ".editor-top-bar",
        ".editor-statusbar",
        ".collab-offline-banner",
        ".collab-local-warning",
    ):
        assert selector in css

    assert "linear-gradient(135deg, var(--ui-brand-900" in css
    assert "var(--editor-danger)" in css
    assert "var(--editor-warning)" in css
    assert "var(--editor-success)" in css
    assert "color: var(--editor-text-muted" in connection
    assert "background-color: var(--editor-success" in connection
    assert "background-color: var(--editor-danger" in connection


def test_batch_6_collaboration_contracts_and_multicolor_presence_are_preserved():
    app_vue = _read_source("components/CollaborativeEditor.vue")
    main_js = _read_source("main.js")
    session_js = _read_source("services/createCollaborationSession.js")

    assert 'class="collab-editor-surface tiptap-editor-wrapper"' in app_vue
    assert 'id="tiptap-editor"' in app_vue
    assert 'app.mount("#collaborative-editor-app")' in main_js
    assert "Collaboration.configure({ fragment })" in main_js
    assert "CollaborationCaret.configure" in main_js
    assert "provider: providerInstance" in main_js
    assert re.search(r"var COLOR_PALETTE = \[[^\]]{80,}\]", session_js, re.S)
    assert "color: userColor" in session_js
    assert "role: user.role || \"student\"" in session_js


def test_batch_6_student_page_keeps_editor_mount_contract(student_login):
    client, headers, _, _ = student_login
    response = client.get("/student/collab", headers=headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'id="collaborativeEditorArea"' in html
    assert 'configScript.id = "collab-editor-config"' in html
    assert 'editorApp.id = "collaborative-editor-app"' in html
    assert 'link.href = "/static/collaborative-editor/editor.css"' in html
    assert 'script.src = "/static/collaborative-editor/editor.js"' in html
    assert "/api/collaborative-documents" in html
