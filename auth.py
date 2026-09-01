# -*- coding: utf-8 -*-
"""Authentication and session helpers for SSRL-ESP."""
import re
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from config import RESEARCH_GROUP_MAX, RESEARCH_GROUP_MIN
from db import ensure_database_ready, execute, now_str, query_all, query_one
from services.login_key_lookup import compute_login_key_lookup_hash


def _is_api_request():
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def _unauthorized_response():
    if _is_api_request():
        return jsonify({"error": "请先登录"}), 401
    return redirect(url_for("login"))


def _forbidden_response():
    if _is_api_request():
        return jsonify({"error": "无权访问该资源"}), 403
    return redirect(url_for("index"))


def get_tab_token_from_request():
    token = (
        request.headers.get("X-Tab-Token")
        or request.args.get("tab_token")
        or request.form.get("tab_token")
    )
    return (token or "").strip()


def create_client_session(user_id, role, login_method=None):
    token = uuid.uuid4().hex
    now = now_str()
    execute(
        """
        INSERT OR REPLACE INTO client_sessions(token, user_id, role, login_method, created_at, last_seen)
        VALUES(?,?,?,?,?,?)
        """,
        (token, user_id, role, login_method, now, now),
    )
    return token


def touch_client_session(tab_token=None, min_interval_seconds=30):
    token = (tab_token or get_tab_token_from_request()).strip()
    if not token:
        return False

    row = query_one(
        "SELECT token, user_id, last_seen FROM client_sessions WHERE token=?",
        (token,),
    )
    if not row:
        return False

    session_user_id = session.get("user_id")
    if session_user_id and row["user_id"] != session_user_id:
        return False

    should_touch = True
    if min_interval_seconds and min_interval_seconds > 0:
        try:
            last_seen = datetime.strptime(row["last_seen"], "%Y-%m-%d %H:%M:%S")
            should_touch = datetime.now() - last_seen >= timedelta(seconds=min_interval_seconds)
        except (TypeError, ValueError):
            should_touch = True

    if not should_touch:
        return False

    execute("UPDATE client_sessions SET last_seen=? WHERE token=?", (now_str(), token))
    return True


def _backfill_key_lookup_hash(table, row_id, lookup_hash):
    if not lookup_hash:
        return
    execute(
        f"""
        UPDATE {table}
        SET key_lookup_hash=?
        WHERE id=? AND (key_lookup_hash IS NULL OR key_lookup_hash='')
        """,
        (lookup_hash, row_id),
    )


def verify_participant_login_key(input_key):
    """Verify a student participant login key against experiment_participants.

    Uses key_lookup_hash to locate a single active candidate when available.
    Legacy rows without lookup hashes are still verified and backfilled on
    successful login.
    Returns participant dict or None.
    Never reveals whether the key exists or is inactive.
    """
    from werkzeug.security import check_password_hash

    ensure_database_ready()
    login_key = (input_key or "").strip()
    lookup_hash = compute_login_key_lookup_hash(login_key)
    if not login_key or not lookup_hash:
        return None

    candidate = query_one(
        """
        SELECT * FROM experiment_participants
        WHERE is_active=1 AND key_lookup_hash=?
        LIMIT 1
        """,
        (lookup_hash,),
    )
    if candidate:
        if check_password_hash(candidate["login_key_hash"], login_key):
            return dict(candidate)
        return None

    participants = query_all(
        """
        SELECT * FROM experiment_participants
        WHERE is_active=1 AND (key_lookup_hash IS NULL OR key_lookup_hash='')
        """
    )
    for p in participants:
        if check_password_hash(p["login_key_hash"], login_key):
            _backfill_key_lookup_hash("experiment_participants", p["id"], lookup_hash)
            return dict(p)
    return None


def verify_teacher_login_key(input_key):
    """Verify a teacher login key against teacher_access_keys.

    Uses key_lookup_hash to locate a single active candidate when available.
    Legacy rows without lookup hashes are still verified and backfilled on
    successful login.
    Returns key record dict or None.
    Never reveals whether the key exists or is inactive.
    """
    from werkzeug.security import check_password_hash

    ensure_database_ready()
    login_key = (input_key or "").strip()
    lookup_hash = compute_login_key_lookup_hash(login_key)
    if not login_key or not lookup_hash:
        return None

    candidate = query_one(
        """
        SELECT * FROM teacher_access_keys
        WHERE is_active=1 AND key_lookup_hash=?
        LIMIT 1
        """,
        (lookup_hash,),
    )
    if candidate:
        if check_password_hash(candidate["key_hash"], login_key):
            return dict(candidate)
        return None

    keys = query_all(
        """
        SELECT * FROM teacher_access_keys
        WHERE is_active=1 AND (key_lookup_hash IS NULL OR key_lookup_hash='')
        """
    )
    for k in keys:
        if check_password_hash(k["key_hash"], login_key):
            _backfill_key_lookup_hash("teacher_access_keys", k["id"], lookup_hash)
            return dict(k)
    return None


def login_as_participant(participant):
    """Log in as a student participant using experiment_participants record.

    Sets session fields, creates client_session with login_method="participant_key",
    and updates last_login_at.
    Returns tab_token or None if validation fails.
    """
    user = query_one("SELECT id, role FROM users WHERE id=? AND role=?", (participant["user_id"], "student"))
    if not user:
        return None

    gm = query_one(
        "SELECT id FROM group_members WHERE user_id=? AND group_id=?", (participant["user_id"], participant["group_id"]),
    )
    if not gm:
        return None

    session["user_id"] = participant["user_id"]
    session["role"] = "student"
    session["participant_code"] = participant["participant_code"]
    session["group_id"] = participant["group_id"]
    session["display_name"] = participant["display_name"]
    session["login_method"] = "participant_key"

    tab_token = create_client_session(participant["user_id"], "student", "participant_key")

    execute("UPDATE experiment_participants SET last_login_at=? WHERE id=?", (now_str(), participant["id"]))

    return tab_token


def login_as_teacher_by_key(key_record):
    """Log in as a teacher using a teacher_access_keys record.

    Sets session fields, creates client_session with login_method="teacher_key",
    and updates last_used_at.
    Returns tab_token or None if validation fails.
    """
    teacher_user_id = key_record["teacher_user_id"]
    user = query_one("SELECT id, role FROM users WHERE id=? AND role=?", (teacher_user_id, "teacher"))
    if not user:
        return None

    session["user_id"] = teacher_user_id
    session["role"] = "teacher"
    session["display_name"] = "Teacher"
    session["login_method"] = "teacher_key"

    tab_token = create_client_session(teacher_user_id, "teacher", "teacher_key")

    execute("UPDATE teacher_access_keys SET last_used_at=? WHERE id=?", (now_str(), key_record["id"]))

    return tab_token
def current_user():
    ensure_database_ready()
    user_id = session.get("user_id")
    if not user_id:
        return None

    user = query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not user:
        session.clear()
        return None

    tab_token = get_tab_token_from_request()
    if tab_token:
        tab_row = query_one(
            "SELECT token, user_id, role FROM client_sessions WHERE token=?",
            (tab_token,),
        )
        if not tab_row or tab_row["user_id"] != user["id"]:
            return None

    user = dict(user)
    session["role"] = user["role"]
    # Merge session extra fields from key-based login (if present)
    for _field in ("participant_code", "display_name", "group_id", "login_method"):
        if _field in session:
            user[_field] = session[_field]

    return user


def login_required(required_role=None):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return _unauthorized_response()
            if required_role and user["role"] != required_role:
                return _forbidden_response()
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def url_with_tab(endpoint, **values):
    user_id = session.get("user_id")
    role = session.get("role")
    if not user_id or not role:
        return url_for(endpoint, **values)
    values.setdefault("tab_token", create_client_session(user_id, role))
    return url_for(endpoint, **values)


def get_user_group_id(user_id):
    row = query_one(
        "SELECT group_id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    return row["group_id"] if row else None


def add_user_to_group(user_id, group_id):
    existing = query_one(
        "SELECT id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if existing:
        execute("UPDATE group_members SET group_id=? WHERE id=?", (group_id, existing["id"]))
        return existing["id"]
    return execute(
        "INSERT INTO group_members(group_id, user_id) VALUES(?,?)",
        (group_id, user_id),
    )


def infer_condition_by_group_name(group_name):
    text = (group_name or "").strip().lower()
    if "control" in text:
        return "control"
    if "experiment" in text:
        return "experiment"
    match = re.search(r"(\d+)", text)
    if not match:
        return "experiment"
    return "experiment" if int(match.group(1)) <= 6 else "control"


def get_or_create_group_by_number(group_number):
    """Legacy: kept for compatibility but REFUSES to create new groups.
    Raises ValueError if the group does not already exist.
    Use get_group_by_number() for a read-only lookup instead.
    """
    group = get_group_by_number(group_number)
    if group is None:
        raise ValueError(
            "Experiment group does not exist; runtime group creation is disabled. "
            "Please contact the administrator."
        )
    return group["id"]


def get_group_by_number(group_number):
    """Read-only lookup of a group by its number (1-15 -> G01-G15).
    Returns the group dict (containing at least id, name, group_code) or None.
    NEVER creates a new group.
    """
    group_no = int(group_number)
    # Out-of-range groups simply don't exist (return None), no error raised
    if group_no < RESEARCH_GROUP_MIN or group_no > RESEARCH_GROUP_MAX:
        return None

    group_code = f"G{group_no:02d}"
    # Primary lookup by group_code
    row = query_one("SELECT * FROM groups WHERE group_code=? ORDER BY id LIMIT 1", (group_code,))
    if row:
        return dict(row)

    # Fallback: lookup by name (legacy groups that may lack group_code)
    group_name = f"第{group_no}组"
    row = query_one("SELECT * FROM groups WHERE name=? AND group_code IS NULL ORDER BY id LIMIT 1", (group_name,))
    if row:
        return dict(row)

    return None

def get_group_condition(group_id):
    row = query_one("SELECT condition, name FROM groups WHERE id=?", (group_id,))
    if not row:
        return "experiment"
    return row["condition"] or infer_condition_by_group_name(row["name"])


def get_sera_user_id():
    row = query_one("SELECT id FROM users WHERE username=?", ("sera",))
    return row["id"] if row else None
