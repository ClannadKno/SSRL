# -*- coding: utf-8 -*-
"""Contract coverage for UI background batch."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE_GLOBS = (
    "static/ui/*.css",
    "static/*.css",
    "static/student/*.css",
    "templates/teacher/*.html",
    "routes/*.py",
    "views/base.py",
)


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _source_texts():
    for pattern in SOURCE_GLOBS:
        for path in ROOT.glob(pattern):
            if path.match("static/collaborative-editor/editor.*"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-16")
            yield path, text


def test_batch_9_background_tokens_define_continuous_multilayer_environment():
    tokens = _read("static/ui/design-tokens.css")
    primitives = _read("static/ui/ui-primitives.css")

    for token in (
        "--ui-page-gradient-base",
        "--ui-page-gradient-veil",
        "--ui-page-glow-cool",
        "--ui-page-glow-warm",
        "--ui-page-glow-cool-auth",
        "--ui-page-glow-warm-auth",
        "--ui-page-glow-cool-analytics",
        "--ui-page-glow-warm-analytics",
    ):
        assert token in tokens

    assert "body.ui-page-background" in primitives
    assert "body.ui-bg-auth" in primitives
    assert "body.ui-bg-workspace" in primitives
    assert "body.ui-bg-analytics" in primitives
    assert "background-repeat: no-repeat, no-repeat, no-repeat, no-repeat" in primitives
    assert "background-size: cover, cover, cover, cover" in primitives
    assert "background-attachment: fixed" not in primitives


def test_batch_9_removes_grid_background_generators_from_runtime_sources():
    forbidden_patterns = (
        re.compile(r"repeating-(?:linear|radial)-gradient", re.I),
        re.compile(r"background-size\s*:\s*(?:20px|40px|42px|48px|64px|[0-9]+px\s+[0-9]+px)", re.I),
        re.compile(r"linear-gradient\([^;\n\r]+1px\s*,\s*transparent\s+1px", re.I),
    )

    offenders = []
    for path, text in _source_texts():
        for pattern in forbidden_patterns:
            if pattern.search(text):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_batch_9_public_login_uses_auth_background_scene(db_and_app):
    _, _, public_client = db_and_app
    login_html = public_client.get("/login").get_data(as_text=True)
    assert '<body class="ui-page-background ui-bg-auth">' in login_html


def test_batch_9_teacher_pages_use_workspace_and_analytics_scenes(teacher_login):
    teacher_client, teacher_headers = teacher_login

    for route in ("/teacher", "/teacher/session/control", "/teacher/roster", "/teacher/export"):
        response = teacher_client.get(route, headers=teacher_headers, follow_redirects=True)
        assert response.status_code == 200, route
        assert "teacher-status-shell ui-page-background ui-bg-workspace" in response.get_data(as_text=True)

    for route in ("/teacher/statistics", "/teacher/emotion-trend"):
        response = teacher_client.get(route, headers=teacher_headers, follow_redirects=True)
        assert response.status_code == 200, route
        assert "teacher-status-shell ui-page-background ui-bg-analytics" in response.get_data(as_text=True)

    audit_html = teacher_client.get("/teacher/audit", headers=teacher_headers).get_data(as_text=True)
    assert '<body class="ui-page-background ui-bg-analytics">' in audit_html


def test_batch_9_student_pages_use_workspace_scene(student_login):
    student_client, student_headers, _, _ = student_login

    for route in ("/student/collab", "/student/collab?phase=pretest", "/student/collab?phase=posttest"):
        response = student_client.get(route, headers=student_headers)
        assert response.status_code == 200, route
        assert "collab-body ui-page-background ui-bg-workspace" in response.get_data(as_text=True)


def test_batch_9_uses_css_gradients_without_background_image_404_contract(db_and_app):
    _, _, client = db_and_app
    primitives = client.get("/static/ui/ui-primitives.css")
    assert primitives.status_code == 200
    css = primitives.get_data(as_text=True)

    assert "url(" not in css
    assert "var(--ui-page-glow-cool)" in css
    assert "var(--ui-page-gradient-base)" in css

    existing_background = ROOT / "static" / "background.png"
    assert existing_background.exists()
    assert client.get("/static/background.png").status_code == 200
