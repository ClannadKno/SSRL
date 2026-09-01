# -*- coding: utf-8 -*-
"""
pytest shared fixtures for SSRL-ESP.

Provides:
- tmp_db_dir: temporary SQLite database directory (auto-cleaned)
- purge_modules(): clears app modules so env vars take effect on re-import
- test_env: temporary env with isolated DB, HUEY, upload paths
- db_and_app: initialized DB + Flask app + test client
- block_network (autouse): prevents accidental real network calls

Test databases are fully isolated: app.db and tasks.db are in separate temp paths.
"""

import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Safe defaults for all tests
_DEFAULT_ENV = {
    "EXPERIMENT_MODE": "0",
    "RESET_DEMO_PASSWORDS_ON_START": "0",
    "USE_LLM_ANALYSIS": "0",
    "SERA_LLM_ENABLED": "0",
    "SERA_LLM_API_KEY": "",
    "SERA_LLM_BASE_URL": "https://llm-test.invalid/v1/chat/completions",
    "SERA_LLM_MODEL": "gpt-test",
    "HUEY_ENABLED": "1",
    "HUEY_IMMEDIATE": "1",
    "DISCUSSION_PIPELINE_V2_ENABLED": "0",
    "AUTO_INTERVENTION_V2_ENABLED": "0",
    "ENABLE_BACKGROUND_SCHEDULER": "0",
}
for _key, _value in _DEFAULT_ENV.items():
    os.environ.setdefault(_key, _value)

MODULE_PREFIXES = ("agent", "routes", "views", "services")
MODULE_NAMES = {"app", "auth", "config", "core", "db", "knowledge_base", "huey_instance", "migrations", "startup_check"}


def purge_modules():
    """Remove cached app modules so next import picks up fresh env vars."""
    for name in list(sys.modules.keys()):
        if name in MODULE_NAMES or name.startswith(MODULE_PREFIXES):
            sys.modules.pop(name, None)


import pytest


# ---------------------------------------------------------------------------
# Network isolation -- prevent accidental real HTTP calls
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Block any socket-based network access during tests.

    Tests that need real network must explicitly mock/override.
    httpx uses socket.connect internally, so this catches LLM calls too.
    """
    import socket as _socket_mod
    
    _real_connect = _socket_mod.socket.connect
    _real_create_conn = _socket_mod.create_connection

    def _blocking_connect(self, *args, **kwargs):
        raise RuntimeError(
            "Network access blocked by test isolation. "
            "If your test needs real network, mock the external service explicitly."
        )

    def _blocking_create_connection(*args, **kwargs):
        raise RuntimeError(
            "Network access blocked by test isolation. "
            "If your test needs real network, mock the external service explicitly."
        )

    monkeypatch.setattr(_socket_mod.socket, "connect", _blocking_connect)
    monkeypatch.setattr(_socket_mod, "create_connection", _blocking_create_connection)
    
    yield

    # monkeypatch undo is automatic


# ---------------------------------------------------------------------------
# Temp directory fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_db_dir():
    """Create a temporary directory for test database files."""
    import tempfile
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="ssrl_test_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Full test environment fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def test_env(tmp_db_dir):
    """
    Set isolated env vars (DB, upload, Huey) and purge cached modules.

    Returns: {db_path, tasks_db_path, upload_dir, tmpdir}
    """
    old = {}
    keys = [
        "SSRL_ESP_DB_PATH",
        "SSRL_ESP_UPLOAD_DIR",
        "SSRL_ESP_SECRET",
        "EXPERIMENT_MODE",
        "RESET_DEMO_PASSWORDS_ON_START",
        "USE_LLM_ANALYSIS",
        "SERA_LLM_ENABLED",
        "SERA_LLM_API_KEY",
        "SERA_LLM_BASE_URL",
        "SERA_LLM_MODEL",
        "HUEY_DB_PATH",
        "HUEY_IMMEDIATE",
        "HUEY_ENABLED",
    ]
    for key in keys:
        old[key] = os.environ.get(key)

    db_path = os.path.join(tmp_db_dir, "test.db")
    tasks_db_path = os.path.join(tmp_db_dir, "tasks.db")
    upload_dir = os.path.join(tmp_db_dir, "uploads")

    os.environ["SSRL_ESP_DB_PATH"] = db_path
    os.environ["SSRL_ESP_UPLOAD_DIR"] = upload_dir
    os.environ["HUEY_DB_PATH"] = tasks_db_path
    os.environ["SSRL_ESP_SECRET"] = "test-secret-for-protection-tests"
    os.environ["EXPERIMENT_MODE"] = "0"
    os.environ["RESET_DEMO_PASSWORDS_ON_START"] = "0"
    os.environ["USE_LLM_ANALYSIS"] = "0"
    os.environ["SERA_LLM_ENABLED"] = "0"
    os.environ["HUEY_ENABLED"] = "1"
    os.environ["HUEY_IMMEDIATE"] = "1"

    purge_modules()

    yield {
        "db_path": db_path,
        "tasks_db_path": tasks_db_path,
        "upload_dir": upload_dir,
        "tmpdir": tmp_db_dir,
    }

    # Restore original env vars
    for key in keys:
        if old[key] is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old[key]

    purge_modules()


# ---------------------------------------------------------------------------
# Flask app + DB fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def db_and_app(test_env):
    """
    Initialize database and Flask application.

    Returns: (db_module, app_module, client)
    """
    import importlib
    db = importlib.import_module("db")
    db.ensure_database_ready()
    app_module = importlib.import_module("app")
    client = app_module.app.test_client()
    with app_module.app.app_context():
        yield db, app_module, client


# ---------------------------------------------------------------------------
# Student login fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def student_login(db_and_app):
    """Create a student participant and login via experiment key.

    Returns: (client, headers, user_id, group_id)
    """
    from urllib.parse import urlparse, parse_qs
    from werkzeug.security import generate_password_hash
    from db import now_str
    import os
    db, app_module, client = db_and_app

    # Create test group
    gid = db.execute(
        "INSERT INTO groups(name, group_code, condition, state, created_at) VALUES(?,?,?,?,?)",
        ("TestGroup", "G99", "experiment", "OPEN", now_str()),
    )

    # Create test user
    random_hash = generate_password_hash(os.urandom(24).hex())
    uid = db.execute(
        "INSERT INTO users(username, password_hash, real_name, participant_code, role, created_at) VALUES(?,?,?,?,?,?)",
        ("test_student_key", random_hash, "TestStudent", "P9999", "student", now_str()),
    )

    # Create group member
    db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (gid, uid))

    # Create experiment participant with known key
    plaintext = "TEST-STUDENT-KEY-4F3A2"
    key_hash = generate_password_hash(plaintext)
    db.execute(
        """INSERT INTO experiment_participants (participant_code, login_key_hash, group_no, member_no, group_id, user_id, display_name, is_active, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("P9999", key_hash, 99, 1, gid, uid, "G99-M1", 1, now_str()),
    )

    # Login with experiment key
    response = client.post("/login", data={"login_key": plaintext}, follow_redirects=False)
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    tab_token = parse_qs(parsed.query)["tab_token"][0]

    yield client, {"X-Tab-Token": tab_token}, uid, gid


# ---------------------------------------------------------------------------
# Teacher login fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def teacher_login(db_and_app):
    """Create a teacher access key and login via experiment key.

    Returns: (client, headers)
    """
    from urllib.parse import urlparse, parse_qs
    from werkzeug.security import generate_password_hash
    from db import now_str
    import os
    db, app_module, client = db_and_app

    # Get or create teacher user
    teacher_row = db.query_one("SELECT id FROM users WHERE role='teacher' ORDER BY id LIMIT 1")
    if not teacher_row:
        random_hash = generate_password_hash(os.urandom(24).hex())
        teacher_user_id = db.execute(
            "INSERT INTO users(username, password_hash, real_name, role, created_at) VALUES(?,?,?,?,?)",
            ("test_teacher", random_hash, "TestTeacher", "teacher", now_str()),
        )
    else:
        teacher_user_id = teacher_row["id"]

    # Create teacher access key with known plaintext
    plaintext = "TEST-TEACHER-KEY-7B5C1"
    key_hash = generate_password_hash(plaintext)
    db.execute(
        "DELETE FROM teacher_access_keys WHERE key_name='test_teacher_key'"
    )
    db.execute(
        """INSERT INTO teacher_access_keys (key_name, key_hash, teacher_user_id, is_active, created_at) VALUES(?,?,?,?,?)""",
        ("test_teacher_key", key_hash, teacher_user_id, 1, now_str()),
    )

    # Login with teacher key
    response = client.post("/login", data={"login_key": plaintext}, follow_redirects=False)
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    tab_token = parse_qs(parsed.query)["tab_token"][0]
    yield client, {"X-Tab-Token": tab_token}


# ---------------------------------------------------------------------------
# Pre-seeded DB fixture (teacher + student + message)
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded_db(db_and_app):
    """
    Provide DB pre-populated with teacher (key login), student (key login), and a test message.

    Returns: (db, app, client, student_headers, group_id, msg_id, teacher_headers)
    """
    from urllib.parse import urlparse, parse_qs
    from werkzeug.security import generate_password_hash
    from db import now_str
    import os
    db, app_module, client = db_and_app

    # --- Teacher key login ---
    teacher_row = db.query_one("SELECT id FROM users WHERE role='teacher' ORDER BY id LIMIT 1")
    teacher_user_id = teacher_row["id"]

    teacher_plaintext = "TEST-TEACHER-KEY-SEED99"
    teacher_key_hash = generate_password_hash(teacher_plaintext)
    db.execute("DELETE FROM teacher_access_keys WHERE key_name='test_teacher_key'")
    db.execute(
        """INSERT INTO teacher_access_keys (key_name, key_hash, teacher_user_id, is_active, created_at) VALUES(?,?,?,?,?)""",
        ("test_teacher_key", teacher_key_hash, teacher_user_id, 1, now_str()),
    )
    resp = client.post("/login", data={"login_key": teacher_plaintext}, follow_redirects=False)
    teacher_headers = {
        "X-Tab-Token": parse_qs(urlparse(resp.headers["Location"]).query)["tab_token"][0]
    }

    # --- Student key login ---
    # Create group
    gid = db.execute(
        "INSERT INTO groups(name, group_code, condition, state, created_at) VALUES(?,?,?,?,?)",
        ("SeedGroup", "G99", "experiment", "OPEN", now_str()),
    )
    # Create user
    random_hash = generate_password_hash(os.urandom(24).hex())
    uid = db.execute(
        "INSERT INTO users(username, password_hash, real_name, participant_code, role, created_at) VALUES(?,?,?,?,?,?)",
        ("seed_student_key", random_hash, "SeedStudent", "P9999", "student", now_str()),
    )
    # Create group member
    db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (gid, uid))

    student_plaintext = "TEST-STUDENT-KEY-SEED99"
    student_key_hash = generate_password_hash(student_plaintext)
    db.execute(
        """INSERT INTO experiment_participants (participant_code, login_key_hash, group_no, member_no, group_id, user_id, display_name, is_active, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("P9999", student_key_hash, 99, 1, gid, uid, "G99-M1", 1, now_str()),
    )
    resp = client.post("/login", data={"login_key": student_plaintext}, follow_redirects=False)
    assert resp.status_code == 302
    student_token = parse_qs(urlparse(resp.headers["Location"]).query)["tab_token"][0]
    student_headers = {"X-Tab-Token": student_token}

    # Create a test message
    msg = db.create_message(gid, uid, "This is a test message", role="student")
    msg_id = msg["id"] if msg else None

    yield db, app_module, client, student_headers, gid, msg_id, teacher_headers

