#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch 0: Authentication baseline diagnostic script.

READ-ONLY. Does not modify any business code, schema, or data.
Checks current system state for: Flask app, DB structure, routes,
login/tab-token flows, teacher/student endpoints, collab editor,
auto-create-group risks, and database size/growth.
"""

import os
import sys
import sqlite3
import importlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure we use the production DB, not test
os.environ.setdefault("SSRL_ESP_DB_PATH", str(ROOT / "ssrl_esp.db"))

_SEP = "-" * 60


def heading(title):
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(f"{_SEP}")


def ok(msg):
    print(f"  [OK] {msg}")


def warn(msg):
    print(f"  [WARN] {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")


def info(msg):
    print(f"  [INFO] {msg}")


# ========== 1. App startup check ==========
heading("1. Application startup check")

try:
    importlib.import_module("config")
    ok("config module imported")
except Exception as e:
    fail(f"config import error: {e}")

try:
    importlib.import_module("db")
    ok("db module imported")
except Exception as e:
    fail(f"db import error: {e}")

try:
    importlib.import_module("auth")
    ok("auth module imported")
except Exception as e:
    fail(f"auth import error: {e}")

try:
    core_mod = importlib.import_module("core")
    app = core_mod.app
    ok("core.app Flask instance created")
except Exception as e:
    fail(f"core import error: {e}")
    app = None

route_modules = [
    "routes.pages", "routes.api", "routes.collab_pages",
    "routes.collaborative_api", "routes.teacher_api", "routes.export",
]
for mod_name in route_modules:
    try:
        importlib.import_module(mod_name)
        ok(f"{mod_name} imported")
    except Exception as e:
        fail(f"{mod_name} import error: {e}")

if app:
    rules = sorted([str(r) for r in app.url_map.iter_rules()])
    info(f"Total registered routes: {len(rules)}")
else:
    rules = []
    warn("Cannot inspect routes (app not created)")


# ========== 2. Database structure ==========
heading("2. Database structure check")

required_tables = [
    "users", "groups", "group_members", "client_sessions",
    "learning_tasks", "experiment_sessions", "messages", "emotion_checkins",
    "help_requests", "collaborative_documents",
    "collaborative_document_checkpoints", "submissions", "process_events",
]

db_path = str(pathlib.Path(ROOT / "ssrl_esp.db").resolve())
info(f"DB path: {db_path}")

try:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    existing_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    info(f"Existing tables ({len(existing_tables)}): "
         f"{', '.join(sorted(existing_tables))}")

    for tbl in required_tables:
        if tbl in existing_tables:
            ok(f"Table '{tbl}' exists")
        else:
            fail(f"Table '{tbl}' MISSING")
    conn.close()
except sqlite3.Error as e:
    fail(f"Database error: {e}")


# ========== 3. Key fields check ==========
heading("3. Key field check")

field_checks = {
    "users": ["id", "username", "password_hash", "real_name",
              "participant_code", "role"],
    "groups": ["id", "name", "group_code"],
    "group_members": ["user_id", "group_id"],
    "client_sessions": ["user_id", "role", "login_method"],
    "messages": ["user_id", "group_id", "task_id", "session_no", "session_id"],
    "collaborative_documents": ["group_id", "task_id", "session_no", "created_by"],
}

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
existing_tables = {
    row["name"]
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
}
for tbl, cols in field_checks.items():
    if tbl not in existing_tables:
        fail(f"Cannot check '{tbl}' -- table missing")
        continue
    table_cols = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
    }
    for col in cols:
        if col in table_cols:
            ok(f"  {tbl}.{col} exists")
        else:
            fail(f"  {tbl}.{col} MISSING")
conn.close()


# ========== 4. Login flow check ==========
heading("4. Login flow check")

if rules:
    if "/login" in rules:
        ok("/login route registered")
    else:
        fail("/login route NOT registered")
else:
    warn("Cannot check login route")

auth_src = (ROOT / "auth.py").read_text(encoding="utf-8")
if 'session.get("user_id")' in auth_src:
    ok("current_user() depends on session['user_id']")
else:
    fail("current_user() does NOT check session['user_id']")

if 'user["role"]' in auth_src:
    ok("login_required() depends on user['role']")
else:
    fail("login_required() does NOT check user['role']")

if "create_client_session" in auth_src:
    ok("create_client_session() defined")
else:
    fail("create_client_session() NOT defined")

pages_src = (ROOT / "routes" / "pages.py").read_text(encoding="utf-8")
if "DELETE FROM client_sessions" in pages_src:
    ok("logout cleans client_sessions")
else:
    fail("logout does NOT clean client_sessions")

if "session.clear()" in pages_src:
    ok("logout calls session.clear()")
else:
    fail("logout does NOT call session.clear()")


# ========== 5. Teacher endpoints ==========
heading("5. Teacher endpoints")

teacher_routes = [
    "/teacher", "/teacher/session/control", "/teacher/statistics",
    "/teacher/emotion-trend", "/teacher/history", "/teacher/export",
    "/teacher/audit", "/teacher/roster", "/teacher/interventions",
    "/teacher/questionnaire-admin",
]

for route in teacher_routes:
    if route in rules:
        ok(f"{route} registered")
    else:
        found = [r for r in rules if route in r]
        if found:
            ok(f"{route} registered via {found[0]}")
        else:
            fail(f"{route} NOT registered")

for lineno, line in enumerate(pages_src.split("\n"), 1):
    if '@login_required("teacher")' in line:
        ok(f"@login_required('teacher') found at line ~{lineno}")
        break
else:
    fail("@login_required('teacher') NOT found in pages.py")


# ========== 6. Student endpoints ==========
heading("6. Student endpoints")

if "/student/collab" in rules:
    ok("/student/collab registered")
else:
    fail("/student/collab NOT registered")

if "current_user()" in pages_src:
    ok("student page uses current_user()")
else:
    fail("student page does NOT use current_user()")

collab_src = (ROOT / "routes" / "collab_pages.py").read_text(encoding="utf-8")
if "get_user_group_id" in collab_src:
    ok("student page uses get_user_group_id()")
else:
    fail("student page does NOT use get_user_group_id()")

if "get_active_task" in collab_src or "get_current_learning_task" in collab_src:
    ok("student page uses active task")
else:
    fail("student page does NOT use active task")

if "get_current_session_no" in collab_src:
    ok("student page uses active session")
else:
    fail("student page does NOT use active session")

if "check_password_hash" in pages_src:
    ok("student login depends on password_hash verification (old logic)")
else:
    warn("student login may not depend on password_hash")


# ========== 7. Collaborative editor check ==========
heading("7. Collaborative editor check")

collab_api_src = (ROOT / "routes" / "collaborative_api.py").read_text(encoding="utf-8")

if "/api/collaborative-documents/current" in collab_api_src:
    ok("GET /api/collaborative-documents/current registered")
else:
    fail("GET /api/collaborative-documents/current NOT found")

if "/api/collaborative-documents/<int:document_id>/ticket" in collab_api_src:
    ok("POST /api/collaborative-documents/<id>/ticket registered")
else:
    fail("POST /api/collaborative-documents/<id>/ticket NOT found")

token_src = (ROOT / "services" / "collaborative_token.py").read_text(encoding="utf-8")
if all(kw in token_src for kw in ["user_id", "document_id", "group_id", "permission"]):
    ok("Ticket payload: user_id, document_id, group_id, permission")
else:
    fail("Ticket payload MISSING required fields")

collab_secret_src = (ROOT / "services" / "collaboration_secret.py").read_text(encoding="utf-8")
if "SSRL_ESP_SECRET" in collab_secret_src or "COLLAB_INTERNAL_SECRET" in collab_secret_src:
    ok("WebSocket secret from SSRL_ESP_SECRET or COLLAB_INTERNAL_SECRET")
else:
    fail("WebSocket secret mechanism unclear")

conn2 = sqlite3.connect(db_path)
conn2.row_factory = sqlite3.Row
idxs = conn2.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='collaborative_documents'"
).fetchall()
has_unique = False
for name, sql in idxs:
    if sql and "UNIQUE" in sql.upper():
        has_unique = True
        ok(f"UNIQUE index: {name}: {sql}")
if not has_unique:
    table_ddl = conn2.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='collaborative_documents'"
    ).fetchone()
    if table_ddl and "UNIQUE" in (table_ddl["sql"] or "").upper():
        ok("UNIQUE constraint in table DDL (inline)")
    else:
        fail("No UNIQUE constraint on collaborative_documents")
conn2.close()


# ========== 8. Auto-create group risk check ==========
heading("8. Auto-create group risk check")

risk_keywords = [
    "get_or_create_group_by_number", "INSERT INTO groups",
    "Default Group", "group_number", "register",
]
risk_files = [ROOT / "auth.py", ROOT / "db.py", ROOT / "routes" / "pages.py"]
found_risks = []

for fpath in risk_files:
    if not fpath.exists():
        continue
    content = fpath.read_text(encoding="utf-8")
    lines = content.split("\n")
    for lineno, line in enumerate(lines, 1):
        for kw in risk_keywords:
            if kw.lower() in line.lower().replace(" ", ""):
                tag = f"{fpath.name}:{lineno} -> {line.strip()[:120]}"
                if tag not in found_risks:
                    found_risks.append(tag)

for r in found_risks:
    warn(r)

if found_risks:
    info(f"Total {len(found_risks)} risk locations identified")
else:
    ok("No auto-create group risks found")


# ========== 9. Database size check ==========
heading("9. Database size check")

try:
    db_path_real = str(ROOT / "ssrl_esp.db")
    db_size = os.path.getsize(db_path_real)
    info(f"Database file size: {db_size} bytes "
         f"({db_size/1024:.1f} KB, {db_size/1024/1024:.2f} MB)")
except OSError as e:
    fail(f"Cannot get database file size: {e}")

conn3 = sqlite3.connect(db_path)
count_tables = [
    "users", "groups", "group_members", "client_sessions",
    "collaborative_documents", "collaborative_document_checkpoints",
    "messages", "emotion_checkins", "help_requests", "submissions",
    "process_events", "learning_tasks", "experiment_sessions",
]
info("Table row counts:")
for tbl in count_tables:
    try:
        count = conn3.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()[0]
        ok(f"  {tbl}: {count} rows")
    except sqlite3.Error:
        warn(f"  {tbl}: could not query")
conn3.close()

wal_p = str(ROOT / "ssrl_esp.db-wal")
shm_p = str(ROOT / "ssrl_esp.db-shm")
if os.path.exists(wal_p):
    info(f"WAL file: {os.path.getsize(wal_p)} bytes")
if os.path.exists(shm_p):
    info(f"SHM file: {os.path.getsize(shm_p)} bytes")
if not os.path.exists(wal_p) and not os.path.exists(shm_p):
    ok("No WAL/SHM files (clean checkpointed state)")


# ========== Summary ==========
heading("SUMMARY")
info("Batch 0 authentication baseline diagnostic complete.")
info("All checks are READ-ONLY -- no business code, schema, or data modified.")
print()
