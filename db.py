# -*- coding: utf-8 -*-
"""/ seed"""
import os
import json
import uuid
import sqlite3
import logging
from datetime import datetime, timedelta
from werkzeug.security import check_password_hash, generate_password_hash
from config import *
from knowledge_base import STATE_META


logger = logging.getLogger(__name__)

DB_READY = False
LEARNING_TASK_JSON_FIELDS = (
    "expected_dimensions_json",
    "key_concepts_json", 
    "common_misconceptions_json",
    "acceptable_paths_json",
)
MESSAGE_ROLE_VALUES = {"student", "agent", "teacher", "system"}
ROOM_STATE_VALUES = {"OPEN", "AI_INTERVENING", "CLOSED"}
SENDER_TYPE_VALUES = {"student", "teacher", "agent", "system"}
MONITOR_RUN_STATUS_VALUES = {"pending", "running", "completed", "failed"}
HELP_REQUEST_STATUS_VALUES = {"QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_FALLBACK", "FAILED"}
QUESTIONNAIRE_TIMING_VALUES = {"pre", "post", "both"}
QUESTIONNAIRE_STAGE_VALUES = {"pre", "post"}
QUESTIONNAIRE_SCALE_VALUES = {5, 7}

# Functions for settings


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_dt(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

def db():
    """Create a new thread-safe SQLite connection.

    Each call creates a fresh connection with WAL mode and busy timeout.
    ``check_same_thread=False`` allows the connection to be safely
    passed to or used from any thread (required when Flask runs with
    ``threaded=True``).  Callers **must** close the connection after
    use (e.g. via ``try/finally`` or the convenience helpers
    ``query_all`` / ``query_one`` / ``execute``).
    """
    conn = sqlite3.connect(
        DB_PATH,
        timeout=max(5, SQLITE_BUSY_TIMEOUT_MS / 1000),
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=%d;" % max(SQLITE_BUSY_TIMEOUT_MS, 5000))
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn

def query_all(sql, params=()):
    conn = db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def query_one(sql, params=()):
    conn = db()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row

def execute(sql, params=()):
    conn = db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
def ensure_settings_table(conn):

    conn.execute("""

        CREATE TABLE IF NOT EXISTS settings (

            key TEXT PRIMARY KEY, value TEXT

        );

    """)



def get_setting(key, default=None):

    row = query_one("SELECT value FROM settings WHERE key=?", (key,))

    if not row:

        return default

    value = row["value"]

    return default if value is None else value



def set_setting(key, value):

    execute("""

        INSERT INTO settings(key, value) VALUES(?, ?)

        ON CONFLICT(key) DO UPDATE SET value=excluded.value

    """, (key, "" if value is None else str(value)))



def upsert_setting(conn, key, value):

    conn.execute("""

        INSERT INTO settings(key, value) VALUES(?, ?)

        ON CONFLICT(key) DO UPDATE SET value=excluded.value

    """, (key, "" if value is None else str(value)))



# Normalize helpers

def normalize_message_role(role, fallback="student"):

    if role in MESSAGE_ROLE_VALUES:

        return role

    return fallback



def _normalize_client_message_id(cmid):

    if not cmid:

        return None

    return str(cmid).strip()[:100] or None



# Runtime context

def get_runtime_message_context():
    """Return task_id, session_no, and server_time.

    Prefers the currently running experiment session; falls back to cached
    settings for backward compatibility during tests or setup.
    """
    ctx = get_current_running_session_context()
    if ctx:
        return {
            "task_id": ctx["task_id"],
            "session_no": ctx["session_no"],
            "server_time": now_str(),
        }
    return {
        "task_id": get_setting("current_task_id"),
        "session_no": int(get_setting("current_session_no", "0")),
        "server_time": now_str(),
    }


def get_current_running_session_context():
    """Return a dict of the currently running experiment session + its task.

    Prefer the explicit `current_session_id` setting. If the setting is absent
    or stale, only fall back when exactly one running session exists.

    Returns dict with keys:
        session_id, session_no, session_role, task_id,
        task_title, task_question, task_goal, output_requirement,
        expected_dimensions (list), key_concepts (list),
        remaining_minutes (int or None), status (str)
    or None when no session is running.
    """
    select_sql = """SELECT s.*, t.title AS task_title, t.question AS task_question,
                          t.task_goal, t.output_requirement,
                          t.expected_dimensions_json, t.key_concepts_json,
                          t.time_limit_minutes AS task_time_limit_minutes
                   FROM experiment_sessions s
                   LEFT JOIN learning_tasks t ON s.task_id = t.id"""

    def _build_context(row):
        data = dict(row)
        return {
            "session_id": data["id"],
            "session_no": data["session_no"],
            "session_role": data["session_role"],
            "task_id": data.get("task_id"),
            "task_title": data.get("task_title"),
            "task_question": data.get("task_question"),
            "task_goal": data.get("task_goal"),
            "output_requirement": data.get("output_requirement"),
            "expected_dimensions": json.loads(data["expected_dimensions_json"]) if isinstance(data.get("expected_dimensions_json"), str) else (data.get("expected_dimensions_json") or []),
            "key_concepts": json.loads(data["key_concepts_json"]) if isinstance(data.get("key_concepts_json"), str) else (data.get("key_concepts_json") or []),
            "deadline": None,
            "time_limit_minutes": data.get("time_limit_minutes"),
            "remaining_minutes": None,
            "remaining_seconds": None,
            "is_timeout": False,
            "server_time": now_str(),
            "status": data["status"],
        }

    current_session_id = get_setting("current_session_id")
    if current_session_id:
        try:
            row = query_one(
                select_sql + " WHERE s.id=? AND s.status='running' LIMIT 1",
                (int(current_session_id),),
            )
        except (TypeError, ValueError):
            row = None
        if row:
            return _build_context(row)

    rows = query_all(
        select_sql + " WHERE s.status='running' ORDER BY s.id DESC LIMIT 2"
    )
    if not rows or len(rows) > 1:
        return None
    return _build_context(rows[0])



# Process event helpers

def _normalize_process_event_value(value):

    if value is None:

        return None

    if isinstance(value, bool):

        return bool(value)

    if isinstance(value, (int, float)):

        return value

    text = str(value).strip()

    return text[:4000] if text else None



def _normalize_process_event_payload(payload):

    if not payload:

        return None

    if isinstance(payload, str):

        value = payload.strip()

        return value[:4000] if value else None

    if not isinstance(payload, dict):

        return json.dumps(payload, ensure_ascii=False)[:4000]

    normalized = {}

    for key, value in payload.items():

        normalized[str(key)] = _normalize_process_event_value(value)

    return json.dumps(normalized, ensure_ascii=False)[:4000]



def _resolve_process_event_context(conn, group_id=None, user_id=None, source=None):

    group_code = None

    condition = None

    participant_code = None

    actor_role = None

    if group_id:

        row = conn.execute(

            "SELECT group_code, condition FROM groups WHERE id=?",

            (group_id,),

        ).fetchone()

        if row:

            group_code = row["group_code"]

            condition = row["condition"]

    if user_id:

        row = conn.execute(

            "SELECT participant_code, role FROM users WHERE id=?",

            (user_id,),

        ).fetchone()

        if row:

            participant_code = row["participant_code"]

            actor_role = row["role"]

    if not actor_role and source in MESSAGE_ROLE_VALUES:

        actor_role = source

    return {

        "group_code": group_code,

        "condition": condition,

        "participant_code": participant_code,

        "actor_role": actor_role or "system",

    }



def _insert_process_event(conn, event_type, *, source="system", group_id=None,

                           user_id=None, session_no=None, task_id=None,

                           related_table=None, related_id=None, event_key=None,

                           payload=None, created_at=None):

    snapshot = _resolve_process_event_context(conn, group_id=group_id, user_id=user_id, source=source)

    conn.execute("""

        INSERT INTO process_events(

            event_key, event_type, source, actor_role, group_id, user_id,

            participant_code, group_code, condition, session_no, task_id,

            related_table, related_id, payload_json, created_at

        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)

        ON CONFLICT(event_key) DO UPDATE SET

            source=excluded.source, actor_role=excluded.actor_role,

            group_id=excluded.group_id, user_id=excluded.user_id,

            participant_code=excluded.participant_code,

            group_code=excluded.group_code, condition=excluded.condition,

            session_no=excluded.session_no, task_id=excluded.task_id,

            related_table=excluded.related_table,

            related_id=excluded.related_id,

            payload_json=excluded.payload_json,

            created_at=excluded.created_at

    """, (

        event_key, event_type, (source or "system")[:40],

        snapshot["actor_role"], group_id, user_id,

        snapshot["participant_code"], snapshot["group_code"],

        snapshot["condition"], session_no, task_id,

        related_table, related_id,

        _normalize_process_event_payload(payload),

        created_at or now_str(),

    ))



def record_process_event(event_type, *, source="system", group_id=None,

                          user_id=None, session_no=None, task_id=None,

                          related_table=None, related_id=None, event_key=None,

                          payload=None, created_at=None):

    conn = db()

    try:

        _insert_process_event(conn, event_type, source=source, group_id=group_id,

                              user_id=user_id, session_no=session_no,

                              task_id=task_id, related_table=related_table,

                              related_id=related_id, event_key=event_key,

                              payload=payload, created_at=created_at)

        conn.commit()

    finally:

        conn.close()

# Message functions

def begin_discussion_observation(
    conn,
    *,
    group_id,
    session_id,
    intervention_sequence,
    discussion_id=None,
    updated_at=None,
):
    """Start post-strategy observation in the publisher transaction.

    ``observation_started_sequence`` deliberately remains NULL until the first
    later student message is written.  Replaying an older or duplicate publish
    cannot reset a newer observation interval.
    """
    if session_id is None or intervention_sequence is None:
        return None
    if discussion_id is not None:
        discussion = conn.execute(
            """
            SELECT id
            FROM group_session_discussions
            WHERE id=? AND group_id=? AND session_id=?
            LIMIT 1
            """,
            (discussion_id, group_id, session_id),
        ).fetchone()
    else:
        discussion = conn.execute(
            """
            SELECT id
            FROM group_session_discussions
            WHERE group_id=? AND session_id=?
            ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (group_id, session_id),
        ).fetchone()
    if not discussion:
        return None
    timestamp = updated_at or now_str()
    conn.execute(
        """
        INSERT OR IGNORE INTO discussion_assessment_cursors(
            group_id, session_id, discussion_id,
            last_finalized_student_sequence, observation_status, updated_at
        ) VALUES(?,?,?,0,'inactive',?)
        """,
        (group_id, session_id, discussion["id"], timestamp),
    )
    conn.execute(
        """
        UPDATE discussion_assessment_cursors
        SET observation_status='observing',
            observation_started_sequence=NULL,
            last_intervention_sequence=?,
            updated_at=?
        WHERE group_id=? AND session_id=? AND discussion_id=?
          AND (
              last_intervention_sequence IS NULL
              OR last_intervention_sequence<?
          )
        """,
        (
            intervention_sequence,
            timestamp,
            group_id,
            session_id,
            discussion["id"],
            intervention_sequence,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (group_id, session_id, discussion["id"]),
    ).fetchone()
    return dict(row) if row else None


def record_observation_student_sequence(
    conn,
    *,
    group_id,
    session_id,
    student_sequence,
    discussion_id=None,
    updated_at=None,
):
    """Atomically retain the first student sequence after an intervention."""
    if session_id is None or student_sequence is None:
        return None
    timestamp = updated_at or now_str()
    conn.execute(
        """
        UPDATE discussion_assessment_cursors
        SET observation_started_sequence=CASE
                WHEN observation_started_sequence IS NULL THEN ?
                WHEN observation_started_sequence>? THEN ?
                ELSE observation_started_sequence
            END,
            updated_at=CASE
                WHEN observation_started_sequence IS NULL
                     OR observation_started_sequence>? THEN ?
                ELSE updated_at
            END
        WHERE group_id=? AND session_id=?
          AND (? IS NULL OR discussion_id=?)
          AND observation_status='observing'
          AND (
              last_intervention_sequence IS NULL
              OR ? > last_intervention_sequence
          )
        """,
        (
            student_sequence,
            student_sequence,
            student_sequence,
            student_sequence,
            timestamp,
            group_id,
            session_id,
            discussion_id,
            discussion_id,
            student_sequence,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=?
          AND (? IS NULL OR discussion_id=?)
        ORDER BY id DESC LIMIT 1
        """,
        (group_id, session_id, discussion_id, discussion_id),
    ).fetchone()
    return dict(row) if row else None

def create_message(group_id, user_id, content, *, role=None, strategy_id=None,

                    reply_to_message_id=None, client_message_id=None,

                    linked_log_id=None, session_no=None, task_id=None,

                    created_at=None, sender_type=None,

                    intervention_run_id=None, metadata_json=None,

                    trigger_source=None, session_id=None, discussion_id=None):

    runtime = get_runtime_message_context()
    if session_no is not None:
        session_no = max(1, int(session_no))
    else:
        session_no = max(1, int(runtime["session_no"] or 1))

    try:

        task_id = int(task_id if task_id is not None else runtime["task_id"])

    except Exception:

        task_id = None

    try:

        reply_to_message_id = int(reply_to_message_id) if reply_to_message_id else None

    except Exception:

        reply_to_message_id = None

    created = created_at or now_str()

    role_value = normalize_message_role(role, fallback="student")

    client_message_id = _normalize_client_message_id(client_message_id)



    conn = db()

    duplicate = False

    try:

        existing_id = None

        if client_message_id:

            existing = conn.execute(

                "SELECT id FROM messages WHERE group_id=? AND user_id=? AND client_message_id=? ORDER BY id DESC LIMIT 1",

                (group_id, user_id, client_message_id),

            ).fetchone()

            if existing:

                existing_id = existing["id"]

                duplicate = True



        if existing_id is None:

            conn.execute(

                """
                UPDATE groups
                SET last_message_sequence =
                    MAX(
                        COALESCE(last_message_sequence, 0),
                        COALESCE(
                            (SELECT MAX(sequence) FROM messages WHERE group_id = ?),
                            0
                        )
                    ) + 1
                WHERE id = ?
                """,

                (group_id, group_id),

            )

            seq_row = conn.execute(

                "SELECT last_message_sequence FROM groups WHERE id = ?",

                (group_id,),

            ).fetchone()

            sequence = seq_row["last_message_sequence"] if seq_row else None

            sender_type_value = normalize_message_role(sender_type, fallback=role_value)



            from services.discussion_scope import resolve_discussion_scope

            scope = resolve_discussion_scope(
                conn,
                group_id=group_id,
                session_id=session_id,
                session_no=session_no,
                task_id=task_id,
                discussion_id=discussion_id,
                allow_legacy_fallback=False,
            )
            session_id = scope.session_id
            session_no = scope.session_no if scope.session_no is not None else session_no
            task_id = scope.task_id if scope.task_id is not None else task_id
            discussion_id = scope.discussion_id
            cur = conn.execute("""

                INSERT INTO messages(group_id, user_id, content, created_at, session_no, task_id,

                    role, strategy_id, reply_to_message_id, client_message_id, linked_log_id,

                    sequence, sender_type, intervention_run_id, metadata_json, session_id, trigger_source,
                    discussion_id, scope_resolved_from, legacy_scope_fallback, scope_fallback_reason)

                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)

            """, (group_id, user_id, content, created, session_no, task_id,

                   role_value, strategy_id, reply_to_message_id,

                   client_message_id, linked_log_id, sequence,

                   sender_type_value, intervention_run_id, metadata_json, session_id, trigger_source,
                   discussion_id, scope.resolved_from, 1 if scope.is_legacy_fallback else 0,
                   scope.fallback_reason))

            msg_id = cur.lastrowid

            if role_value == "student" or sender_type_value == "student":
                record_observation_student_sequence(
                    conn,
                    group_id=group_id,
                    session_id=session_id,
                    discussion_id=discussion_id,
                    student_sequence=sequence,
                    updated_at=created,
                )

            conn.commit()



            return {

                "id": msg_id,

                "group_id": group_id,

                "user_id": user_id,

                "content": content,

                "created_at": created,

                "role": role_value,

                "sequence": sequence,

                "sender_type": sender_type_value,

                "session_no": session_no,

                "task_id": task_id,

                "session_id": session_id,

                "discussion_id": discussion_id,

                "scope_resolved_from": scope.resolved_from,

                "legacy_scope_fallback": scope.is_legacy_fallback,

                "scope_fallback_reason": scope.fallback_reason,

                "trigger_source": trigger_source,

                "client_message_id": client_message_id,

                "duplicate": False,

            }



        existing_msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (existing_id,)).fetchone())

        existing_msg["duplicate"] = True

        return existing_msg

    finally:

        conn.close()



def get_user_group_id(user_id):

    row = query_one(

        "SELECT group_id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",

        (user_id,),

    )

    return int(row["group_id"]) if row else None



def get_group_member_count(group_id):

    row = query_one("SELECT COUNT(*) AS c FROM group_members WHERE group_id=?", (group_id,))

    return int(row["c"]) if row else 0



def get_sera_user_id():

    row = query_one("SELECT id FROM users WHERE role='agent' ORDER BY id ASC LIMIT 1")

    return int(row["id"]) if row else None



# Task functions

def get_current_learning_task():
    """Return the task bound to the currently running experiment session."""
    ctx = get_current_running_session_context()
    if ctx and ctx.get("task_id"):
        task = get_learning_task(ctx["task_id"])
        if task:
            return task
    return None



def get_learning_task(task_id):

    if not task_id:

        return None

    row = query_one("SELECT * FROM learning_tasks WHERE id=?", (task_id,))
    if not row:

        return None

    data = dict(row)

    # Ensure compatibility fields

    data.setdefault("question", data.get("title") or "")

    data.setdefault("task_goal", data.get("description") or "")

    data.setdefault("output_requirement", "")

    for field in LEARNING_TASK_JSON_FIELDS:

        if data.get(field) and isinstance(data[field], str):

            try:

                data[field.replace("_json", "")] = json.loads(data[field])

            except Exception:

                data[field.replace("_json", "")] = []

    # Parse task_payload_json into task_payload
    raw = data.get("task_payload_json", "{}")
    if isinstance(raw, str):
        try:
            data["task_payload"] = json.loads(raw)
        except Exception:
            data["task_payload"] = {}
    else:
        data["task_payload"] = {}

    return data



def get_current_session_no():

    try:

        return int(get_setting("current_session_no", "1"))

    except Exception:

        return 1



def get_current_task_started_at():

    return get_setting("current_task_started_at")



def list_learning_tasks(include_inactive=True):

    rows = query_all("SELECT * FROM learning_tasks ORDER BY sort_order ASC, id ASC")

    result = []

    for row in rows:

        data = dict(row)

        for field in LEARNING_TASK_JSON_FIELDS:

            if data.get(field) and isinstance(data[field], str):

                try:

                    data[field.replace("_json", "")] = json.loads(data[field])

                except Exception:

                    data[field.replace("_json", "")] = []

        # Parse task_payload_json into task_payload
        raw = data.get("task_payload_json", "{}")
        if isinstance(raw, str):
            try:
                data["task_payload"] = json.loads(raw)
            except Exception:
                data["task_payload"] = {}
        else:
            data["task_payload"] = {}

        result.append(data)

    return result



def get_current_task_remaining_minutes(task=None):

    if not task:

        task = get_current_learning_task()

    if not task:

        return 30

    time_limit = int(task.get("time_limit_minutes", 30))

    started_at = get_current_task_started_at()

    if not started_at:

        return time_limit

    started = parse_dt(started_at)

    if not started:

        return time_limit

    elapsed = (datetime.now() - started).total_seconds() / 60

    return max(0, time_limit - int(elapsed))

# Questionnaire functions

def create_questionnaire(data, items=None):

    conn = db()

    try:

        timing = str(data.get("timing", "both")).strip().lower()

        if timing not in QUESTIONNAIRE_TIMING_VALUES:

            timing = "both"

        if items is None:

            items = data.get("items", [])

        created_by = data.get("created_by")

        cur = conn.execute("""

            INSERT INTO questionnaires(code, category_key, title, description,

                timing, scale_max, active, sort_order, created_by, created_at, updated_at)

            VALUES(?,?,?,?,?,?,?,?,?,?,?)

        """, (

            data.get("code"), data.get("category_key", "ssrl"),

            data.get("title"), data.get("description"),

            timing, int(data.get("scale_max", 5)),

            bool(data.get("active", False)),

            int(data.get("sort_order", 0)),

            created_by,

            now_str(), now_str(),

        ))

        qid = cur.lastrowid

        _replace_questionnaire_items(conn, qid, _normalize_questionnaire_items(items or []), now_str())

        conn.commit()

        return qid

    finally:

        conn.close()



def _normalize_questionnaire_items(items):

    import json as _json

    def _json_text(value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return value
        return _json.dumps(value, ensure_ascii=False)

    normalized = []

    for item in (items or []):

        if isinstance(item, dict):

            max_value = item.get("max_value")
            n = {

                "item_code": item.get("item_code") or str(item.get("id", "")),

                "prompt_text": item.get("prompt_text") or item.get("item_text", ""),

                "dimension_label": item.get("dimension_label") or item.get("dimension", ""),

                "sort_order": int(item.get("sort_order") or 0),

                "required": bool(item.get("required", True)),

                "question_type": item.get("question_type") or "likert_5",

                "dimension_key": item.get("dimension_key") or "",

                "reverse_scored": bool(item.get("reverse_scored", False)),

                "min_value": int(item.get("min_value") or 1),

                "max_value": int(max_value) if max_value not in (None, "") else None,

                "options_json": _json_text(item.get("options_json") or item.get("options")),

                "score_map_json": _json_text(item.get("score_map_json") or item.get("score_map")),

                "include_in_score": bool(item.get("include_in_score", True)),

                "help_text": item.get("help_text") or "",

                "section_no": int(item.get("section_no") or 1),

                "section_title": item.get("section_title") or "",

                "scale_labels_json": _json_text(item.get("scale_labels_json") or item.get("scale_labels")),

            }

            normalized.append(n)

    return normalized



def _replace_questionnaire_items(conn, questionnaire_id, items, created):

    conn.execute("DELETE FROM questionnaire_items WHERE questionnaire_id=?", (questionnaire_id,))
    qrow = conn.execute(
        "SELECT scale_max FROM questionnaires WHERE id=?", (questionnaire_id,)
    ).fetchone()
    default_max_value = int(qrow["scale_max"] if qrow and qrow["scale_max"] else 5)

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info('questionnaire_items')").fetchall()
    }

    insert_columns = [
        "questionnaire_id", "item_code", "prompt_text",
        "dimension_label", "sort_order", "required", "created_at",
    ]
    optional_columns = [
        "question_type", "dimension_key", "reverse_scored", "min_value",
        "max_value", "options_json", "score_map_json", "include_in_score",
        "help_text", "section_no", "section_title", "scale_labels_json",
    ]
    insert_columns.extend([c for c in optional_columns if c in columns])

    for item in items:
        row_values = {
            "questionnaire_id": questionnaire_id,
            "item_code": item.get("item_code", ""),
            "prompt_text": item.get("prompt_text", ""),
            "dimension_label": item.get("dimension_label", ""),
            "sort_order": int(item.get("sort_order", 0)),
            "required": 1 if item.get("required", True) else 0,
            "created_at": created,
            "question_type": item.get("question_type", "likert_5"),
            "dimension_key": item.get("dimension_key", ""),
            "reverse_scored": 1 if item.get("reverse_scored", False) else 0,
            "min_value": int(item.get("min_value") or 1),
            "max_value": item.get("max_value") or default_max_value,
            "options_json": item.get("options_json"),
            "score_map_json": item.get("score_map_json"),
            "include_in_score": 1 if item.get("include_in_score", True) else 0,
            "help_text": item.get("help_text", ""),
            "section_no": int(item.get("section_no") or 1),
            "section_title": item.get("section_title", ""),
            "scale_labels_json": item.get("scale_labels_json"),
        }
        placeholders = ",".join("?" for _ in insert_columns)
        conn.execute(
            "INSERT INTO questionnaire_items({}) VALUES({})".format(
                ",".join(insert_columns), placeholders
            ),
            tuple(row_values[c] for c in insert_columns),
        )



def _questionnaire_item_dict(item):
    d = dict(item)
    for field in ("reverse_scored", "required", "include_in_score"):
        if field in d:
            d[field] = bool(d[field])
    for field, parsed_field in (
        ("options_json", "options"),
        ("score_map_json", "score_map"),
        ("scale_labels_json", "scale_labels"),
    ):
        value = d.get(field)
        if isinstance(value, str) and value.strip():
            try:
                d[parsed_field] = json.loads(value)
            except Exception:
                d[parsed_field] = [] if parsed_field != "score_map" else {}
    return d


def list_questionnaires(include_inactive=False, include_items=False, include_summary=False):

    where = "" if include_inactive else "WHERE q.active=1"

    rows = query_all(f"""

        SELECT q.* FROM questionnaires q {where}

        ORDER BY q.sort_order ASC, q.id ASC

    """)

    result = []

    for row in rows:

        q = dict(row)

        if include_items:

            items = query_all(

                "SELECT * FROM questionnaire_items WHERE questionnaire_id=? ORDER BY sort_order ASC, id ASC",

                (q["id"],),

            )

            q["items"] = [_questionnaire_item_dict(it) for it in items]

        if include_summary:

            rc = query_one(
                "SELECT COUNT(*) AS c FROM questionnaire_responses WHERE questionnaire_id=?",

                (q["id"],),
            )
            pc = query_one(
                "SELECT COUNT(DISTINCT user_id) AS c FROM questionnaire_responses WHERE questionnaire_id=?",

                (q["id"],),
            )
            q["response_summary"] = {
                "response_count": int(rc["c"] or 0) if rc else 0,
                "participant_count": int(pc["c"] or 0) if pc else 0,
            }

        result.append(q)

    return result



def save_questionnaire_responses(questionnaire_id, user_id, group_id, response_stage, responses):

    stage = str(response_stage).strip().lower() if response_stage else "pre"

    if stage not in QUESTIONNAIRE_STAGE_VALUES:

        stage = "pre"

    conn = db()

    try:

        conn.execute(

            "DELETE FROM questionnaire_responses WHERE questionnaire_id=? AND user_id=? AND response_stage=?",

            (questionnaire_id, user_id, stage),

        )

        now = now_str()

        for item_id_str, value in (responses or {}).items():

            try:

                item_id = int(item_id_str)

            except (ValueError, TypeError):

                continue

            response_value = None
            response_text = None
            response_option_key = None
            if isinstance(value, dict):
                if value.get("option_key") not in (None, ""):
                    response_option_key = str(value.get("option_key"))
                if value.get("value") not in (None, ""):
                    try:
                        response_value = int(value.get("value"))
                    except (ValueError, TypeError):
                        response_text = str(value.get("value"))
                elif value.get("text") not in (None, ""):
                    response_text = str(value.get("text"))
                elif value.get("option_key") not in (None, ""):
                    response_value = 0
                    response_text = str(value.get("option_key"))
            else:
                try:
                    response_value = int(value)
                except (ValueError, TypeError):
                    response_text = None if value is None else str(value)

            if _conn_has_column(conn, "questionnaire_responses", "response_option_key"):
                conn.execute("""

                    INSERT INTO questionnaire_responses(questionnaire_id, user_id, item_id, response_value, response_text, response_option_key, response_stage, group_id, created_at)

                    VALUES(?,?,?,?,?,?,?,?,?)

                """, (questionnaire_id, user_id, item_id, response_value, response_text, response_option_key, stage, group_id, now))
            else:
                conn.execute("""

                    INSERT INTO questionnaire_responses(questionnaire_id, user_id, item_id, response_value, response_text, response_stage, group_id, created_at)

                    VALUES(?,?,?,?,?,?,?,?)

                """, (questionnaire_id, user_id, item_id, response_value, response_text, stage, group_id, now))

        conn.commit()

        return {"submitted": True, "updated": True}

    finally:

        conn.close()






def _sanitize_student_item(item):
    """Remove scoring-related fields from item dict for student API."""
    d = dict(item)
    # Parse JSON fields
    _json = __import__('json')
    if isinstance(d.get('options_json'), str) and d['options_json'].strip():
        try:
            d['options'] = _json.loads(d['options_json'])
        except Exception:
            d['options'] = []
    else:
        d['options'] = []
    if isinstance(d.get('scale_labels_json'), str) and d['scale_labels_json'].strip():
        try:
            d['scale_labels'] = _json.loads(d['scale_labels_json'])
        except Exception:
            d['scale_labels'] = []
    else:
        d['scale_labels'] = []
    # Remove raw JSON and scoring fields
    for field in ('score_map_json', 'options_json', 'scale_labels_json'):
        d.pop(field, None)
    return d

def _student_questionnaire_sections(items, scale_max=5):
    sections = []
    sec_map = {}
    for itm in items:
        sec_no = itm.get("section_no", 1)
        if sec_no not in sec_map:
            sec = {
                "section_key": "section_" + str(sec_no),
                "title": itm.get("section_title", ""),
                "description": "",
                "display_type": "standard",
                "scale_min": 1,
                "scale_max": scale_max,
                "scale_labels": itm.get("scale_labels", []),
                "sort_order": sec_no,
                "items": [],
            }
            sec_map[sec_no] = sec
            sections.append(sec)
        sec_map[sec_no]["items"].append(itm)
    return sections


def list_student_questionnaires(user_id, session_id=None, response_stage=None):
    if not session_id:
        session_ctx = get_current_running_session_context()
        if not session_ctx:
            return []
        session_id = session_ctx["session_id"]

    if response_stage is None and session_id:
        session = query_one("SELECT session_role FROM experiment_sessions WHERE id=?", (session_id,))
        response_stage = session["session_role"] if session and session["session_role"] in QUESTIONNAIRE_STAGE_VALUES else "pre"

    stage = str(response_stage or "pre").strip().lower()
    if stage not in QUESTIONNAIRE_STAGE_VALUES:
        stage = "pre"

    questionnaires = list_published_questionnaires_for_student(
        user_id=user_id,
        session_id=session_id,
        response_stage=stage,
    )

    result = []

    for q in questionnaires:

        q_dict = dict(q)

        qid = q_dict["id"]

        items = query_all(

            "SELECT * FROM questionnaire_items WHERE questionnaire_id=? ORDER BY section_no ASC, sort_order ASC, id ASC",

            (qid,),

        )

        q_dict["items"] = [_sanitize_student_item(it) for it in items]
        q_dict["sections"] = _student_questionnaire_sections(
            q_dict["items"], q_dict.get("scale_max", 5)
        )

        responses = query_all("""

            SELECT qr.item_id, qr.response_value, qr.response_stage

            FROM questionnaire_responses qr

            WHERE qr.questionnaire_id=? AND qr.user_id=? AND qr.session_id=?

        """, (qid, user_id, session_id))

        completed_stages = set()

        existing_responses = {}

        for r in responses:

            stage = r["response_stage"]

            completed_stages.add(stage)

            key = f"{stage}:{r['item_id']}"

            existing_responses[key] = r["response_value"]

        sub = query_one(
            "SELECT id, status FROM questionnaire_submissions "
            "WHERE questionnaire_id=? AND user_id=? AND session_id=? "
            "AND response_stage=?",
            (qid, user_id, session_id, stage),
        )
        if sub and sub["status"] == "submitted":
            completed_stages.add(stage)

        q_dict["allowed_stages"] = [stage]

        q_dict["completed_stages"] = list(completed_stages)

        q_dict["existing_responses"] = existing_responses
        q_dict["submitted"] = bool(sub and sub["status"] == "submitted")
        q_dict["submission_id"] = sub["id"] if sub else None

        result.append(q_dict)

    return result



def get_questionnaire_individual_responses(questionnaire_id, group_id):

    users = query_all("""

        SELECT u.id, u.real_name, u.participant_code

        FROM users u

        WHERE u.id IN (SELECT user_id FROM group_members WHERE group_id=?)

        ORDER BY u.id ASC

    """, (group_id,))

    sample_items = query_all(

        "SELECT * FROM questionnaire_items WHERE questionnaire_id=? ORDER BY sort_order ASC, id ASC",

        (questionnaire_id,),

    )

    sample_items = [dict(it) for it in sample_items]

    result = []

    for u in users:

        user_dict = dict(u)

        responses = query_all("""

            SELECT qi.item_code, qi.prompt_text, qi.dimension_label, qr.response_value, qr.response_stage

            FROM questionnaire_responses qr

            JOIN questionnaire_items qi ON qr.item_id=qi.id

            WHERE qr.questionnaire_id=? AND qr.user_id=?

            ORDER BY qi.sort_order ASC, qi.id ASC

        """, (questionnaire_id, u["id"]))

        user_dict["responses"] = [dict(r) for r in responses]

        user_dict["item_count"] = len(sample_items)

        user_dict["responded_count"] = len(responses)

        result.append(user_dict)

    return result

# Submission functions

def create_submission(group_id, user_id, content, *, file_name=None,

                       stored_file_name=None, file_path=None, file_size=0,

                       submission_mode="text", task_id=None, session_no=None):

    runtime = get_runtime_message_context()

    session_no = max(1, int(session_no if session_no is not None else runtime["session_no"]))

    task_id = int(task_id if task_id is not None else runtime["task_id"] or 0) or None

    sid = execute("""

        INSERT INTO submissions(group_id, user_id, task_id, session_no, content,

            file_name, stored_file_name, file_path, file_size, submission_mode,

            submitted_at, created_at)

        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)

    """, (group_id, user_id, task_id, session_no, content,

           file_name, stored_file_name, file_path, file_size, submission_mode,

           now_str(), now_str()))

    if sid:

        record_process_event(

            "submission_created", source="student",

            group_id=group_id, user_id=user_id,

            related_table="submissions", related_id=sid,

            payload={"task_id": task_id, "session_no": session_no,

                      "submission_mode": submission_mode,

                      "content_length": len(content or ""),

                      "has_file": bool(file_name)},

        )

    return {"id": sid, "group_id": group_id, "user_id": user_id,

            "content": content, "task_id": task_id, "session_no": session_no,

            "submission_mode": submission_mode}



def has_student_pending_questionnaires(timing, user_id, session_id=None, group_id=None):

    stage = "pre" if timing == "pre" else "post"

    if not session_id:
        session_ctx = get_current_running_session_context()
        if not session_ctx:
            return False
        session_id = session_ctx["session_id"]

    questionnaires = list_published_questionnaires_for_student(
        user_id=user_id,
        session_id=session_id,
        response_stage=stage,
        group_id=group_id,
    )
    for q in questionnaires:
        submitted = query_one(
            "SELECT id FROM questionnaire_submissions "
            "WHERE questionnaire_id=? AND user_id=? AND session_id=? "
            "AND response_stage=? AND status='submitted' LIMIT 1",
            (q["id"], user_id, session_id, stage),
        )
        if submitted:
            continue
        response = query_one(
            "SELECT id FROM questionnaire_responses "
            "WHERE questionnaire_id=? AND user_id=? AND session_id=? "
            "AND response_stage=? LIMIT 1",
            (q["id"], user_id, session_id, stage),
        )
        if not response:
            return True

    return False



# Scheduler functions

def list_schedulable_groups(now=None):

    now = now or datetime.now()

    active_cutoff = (now - timedelta(seconds=ONLINE_ACTIVE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")

    rows = query_all("""

        SELECT DISTINCT g.id FROM groups g

        JOIN group_members gm ON gm.group_id=g.id

        JOIN client_sessions cs ON cs.user_id=gm.user_id

        WHERE cs.last_seen>=?

        ORDER BY g.id ASC

    """, (active_cutoff,))

    return [int(r["id"]) for r in rows]



# Dashboard rollup function


# ============================================================
# Task CRUD - teacher API helpers
# ============================================================

def create_learning_task(payload):
    """Create a new learning task. Returns the new task id."""
    import json as _json
    now = now_str()
    # Build task_payload_json for structured_decision tasks
    task_payload = payload.get("task_payload") or payload.get("task_payload_json")
    if task_payload and isinstance(task_payload, dict):
        task_payload_json = _json.dumps(task_payload, ensure_ascii=False)
    elif task_payload and isinstance(task_payload, str):
        try:
            _json.loads(task_payload)
            task_payload_json = task_payload
        except Exception:
            task_payload_json = "{}"
    else:
        task_payload_json = "{}"
    # If experiment_phase_id is provided, store experiment_phase_name
    exp_phase_name = payload.get("experiment_phase_name", "")
    if not exp_phase_name and payload.get("experiment_phase_id"):
        row = query_one("SELECT name FROM experiment_phases WHERE id=?", (payload["experiment_phase_id"],))
        if row:
            exp_phase_name = row["name"]
    fields = {
        "title": payload.get("title", ""),
        "description": payload.get("task_goal", payload.get("description", "")),
        "question": payload.get("question", ""),
        "task_goal": payload.get("task_goal", ""),
        "output_requirement": payload.get("output_requirement", ""),
        "time_limit_minutes": int(payload.get("time_limit_minutes", 30)),
        "expected_dimensions_json": _json.dumps(payload.get("expected_dimensions", []), ensure_ascii=False),
        "key_concepts_json": _json.dumps(payload.get("key_concepts", []), ensure_ascii=False),
        "common_misconceptions_json": _json.dumps(payload.get("common_misconceptions", []), ensure_ascii=False),
        "acceptable_paths_json": _json.dumps(payload.get("acceptable_paths", []), ensure_ascii=False),
        "is_active": 1 if payload.get("active", False) else 0,
        "sort_order": int(payload.get("sort_order", 0)),
        "task_type": payload.get("task_type", "structured_decision"),
        "experiment_phase_id": payload.get("experiment_phase_id"),
        "experiment_phase_name": exp_phase_name,
        "task_schema_version": int(payload.get("task_schema_version", 2)),
        "task_payload_json": task_payload_json,
        "created_at": now,
    }
    return execute("""
        INSERT INTO learning_tasks(title, description, question, task_goal, output_requirement,
            time_limit_minutes, expected_dimensions_json, key_concepts_json,
            common_misconceptions_json, acceptable_paths_json, is_active, sort_order,
             task_type, experiment_phase_id, experiment_phase_name,
            task_schema_version, task_payload_json, created_at)
        VALUES(:title, :description, :question, :task_goal, :output_requirement,
            :time_limit_minutes, :expected_dimensions_json, :key_concepts_json,
            :common_misconceptions_json, :acceptable_paths_json, :is_active, :sort_order,
             :task_type, :experiment_phase_id, :experiment_phase_name,
            :task_schema_version, :task_payload_json, :created_at)
    """, fields)
def update_learning_task(task_id, payload):
    """Update an existing learning task."""
    import json as _json
    updates = []
    values = {}
    # Map JS-style fields to DB columns
    field_map = {
        "title": "title",
        "question": "question",
        "task_goal": "task_goal",
        "description": "description",
        "output_requirement": "output_requirement",
        "time_limit_minutes": "time_limit_minutes",
    }
    for js_key, db_col in field_map.items():
        if js_key in payload:
            updates.append(f"{db_col}=?")
            values.setdefault("params", []).append(payload[js_key])
    if "active" in payload:
        updates.append("is_active=?")
        values.setdefault("params", []).append(1 if payload["active"] else 0)
    if "expected_dimensions" in payload:
        updates.append("expected_dimensions_json=?")
        values.setdefault("params", []).append(_json.dumps(payload["expected_dimensions"], ensure_ascii=False))
    if "key_concepts" in payload:
        updates.append("key_concepts_json=?")
        values.setdefault("params", []).append(_json.dumps(payload["key_concepts"], ensure_ascii=False))
    if "common_misconceptions" in payload:
        updates.append("common_misconceptions_json=?")
        values.setdefault("params", []).append(_json.dumps(payload["common_misconceptions"], ensure_ascii=False))
    if "acceptable_paths" in payload:
        updates.append("acceptable_paths_json=?")
        values.setdefault("params", []).append(_json.dumps(payload["acceptable_paths"], ensure_ascii=False))
    # New structured task fields
    if "task_type" in payload:
        updates.append("task_type=?")
        values.setdefault("params", []).append(payload["task_type"])
    if "experiment_phase_id" in payload:
        updates.append("experiment_phase_id=?")
        values.setdefault("params", []).append(payload["experiment_phase_id"])
        if payload["experiment_phase_id"]:
            row = query_one("SELECT name FROM experiment_phases WHERE id=?", (payload["experiment_phase_id"],))
            if row:
                updates.append("experiment_phase_name=?")
                values.setdefault("params", []).append(row["name"])
    if "experiment_phase_name" in payload:
        updates.append("experiment_phase_name=?")
        values.setdefault("params", []).append(payload["experiment_phase_name"])
    if "task_schema_version" in payload:
        updates.append("task_schema_version=?")
        values.setdefault("params", []).append(int(payload["task_schema_version"]))
    if "task_payload" in payload or "task_payload_json" in payload:
        task_payload = payload.get("task_payload") or payload.get("task_payload_json")
        if task_payload and isinstance(task_payload, dict):
            task_payload_json = _json.dumps(task_payload, ensure_ascii=False)
        elif task_payload and isinstance(task_payload, str):
            try:
                _json.loads(task_payload)
                task_payload_json = task_payload
            except Exception:
                task_payload_json = "{}"
        else:
            task_payload_json = "{}"
        updates.append("task_payload_json=?")
        values.setdefault("params", []).append(task_payload_json)
    if not updates:
        return None
    updates.append("updated_at=?")
    values["params"].append(now_str())
    sep = ", "
    sql = f"UPDATE learning_tasks SET {sep.join(updates)} WHERE id=?"
    values["params"].append(task_id)
    execute(sql, tuple(values["params"]))
    return task_id
def delete_learning_task(task_id):
    """Delete a learning task.

    Raises ValueError if the task is referenced by collaborative documents
    or experiment sessions, to avoid silent data corruption.
    """
    # Check collaborative documents (has FK constraint)
    doc_count = query_one(
        "SELECT COUNT(*) AS c FROM collaborative_documents WHERE task_id=?",
        (task_id,),
    )
    if doc_count and int(doc_count["c"]) > 0:
        raise ValueError(
            f"任务 #{task_id} 已被 {int(doc_count['c'])} 个协作文档引用，无法删除。"
            "如需删除请先清理相关文档。"
        )

    # Check experiment sessions (for user-friendly message)
    session_count = query_one(
        "SELECT COUNT(*) AS c FROM experiment_sessions WHERE task_id=?",
        (task_id,),
    )
    if session_count and int(session_count["c"]) > 0:
        raise ValueError(
            f"任务 #{task_id} 已被 {int(session_count['c'])} 个课次引用，无法删除。"
            "请先在课次列表中解除任务分配后再删除。"
        )

    execute("DELETE FROM learning_tasks WHERE id=?", (task_id,))


# ---- Experiment Phases CRUD ----


def create_experiment_phase(name, description="", default_agent_intervention_enabled=1):
    """Create a new experiment phase."""
    now = now_str()
    return execute("""
        INSERT INTO experiment_phases(name, description, default_agent_intervention_enabled, is_active, sort_order, created_at, updated_at)
        VALUES(?,?,?,1,0,?,?)
    """, (name, description, 1 if default_agent_intervention_enabled else 0, now, now))


def list_experiment_phases(active_only=True):
    """List experiment phases."""
    if active_only:
        rows = query_all("SELECT * FROM experiment_phases WHERE is_active=1 ORDER BY sort_order ASC, id ASC")
    else:
        rows = query_all("SELECT * FROM experiment_phases ORDER BY sort_order ASC, id ASC")
    return [dict(r) for r in rows]


def get_experiment_phase(phase_id):
    """Get a single experiment phase by id."""
    row = query_one("SELECT * FROM experiment_phases WHERE id=?", (phase_id,))
    return dict(row) if row else None


def update_experiment_phase(phase_id, data):
    """Update an experiment phase."""
    updates = []
    params = []
    for key in ("name", "description", "default_agent_intervention_enabled", "is_active", "sort_order"):
        if key in data:
            updates.append(f"{key}=?")
            params.append(data[key])
    if not updates:
        return None
    params.append(now_str())
    params.append(phase_id)
    updates.append("updated_at=?")
    execute(f"UPDATE experiment_phases SET {', '.join(updates)} WHERE id=?", tuple(params))
    return phase_id




def set_current_session(session_no):
    """Set the current session number."""
    set_setting("current_session_no", str(int(session_no)))


def set_current_task(task_id):
    """Set the current active task."""
    set_setting("current_task_id", str(int(task_id)))
    set_setting("current_task_started_at", now_str())


def get_teacher_group_dashboard_rollup(group_id, *, session_no=None, task_id=None):

    return {

        "group_id": group_id,

        "questionnaire_progress": {},

        "submission_overview": {"count": 0, "latest": None},

        "latest_submission": None,

        "latest_decision": None,

    }



def get_latest_agent_suggestion(group_id):

    row = query_one("""

        SELECT * FROM agent_suggestions

        WHERE group_id=? ORDER BY id DESC LIMIT 1

    """, (group_id,))

    return dict(row) if row else None



def get_group_latest_intervention_log(group_id):

    row = query_one("""

        SELECT * FROM intervention_logs

        WHERE group_id=? ORDER BY id DESC LIMIT 1

    """, (group_id,))

    return dict(row) if row else None



def get_latest_pending_suggestion(group_id):

    row = query_one("""

        SELECT * FROM agent_suggestions

        WHERE group_id=? AND status='pending' ORDER BY id DESC LIMIT 1

    """, (group_id,))

    return dict(row) if row else None



# Intervention functions

def list_interventions():

    return query_all("SELECT * FROM interventions ORDER BY id ASC")



def get_group_condition(group_id):

    row = query_one("SELECT condition FROM groups WHERE id=?", (group_id,))

    return row["condition"] if row else "experiment"



def push_intervention(group_id, intervention_id, pushed_by_user_id, push_mode="teacher"):
    if not LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED:
        return None

    intervention = query_one("SELECT * FROM interventions WHERE id=?", (intervention_id,))

    if not intervention:

        return None

    log_id = execute("""

        INSERT INTO intervention_logs(group_id, intervention_id, pushed_by_user_id,

            push_mode, title, message, condition, trigger_source, created_at)

        VALUES(?,?,?,?,?,?,?,?,?)

    """, (group_id, intervention_id, pushed_by_user_id, push_mode,

           intervention["title"], intervention["message"],

           get_group_condition(group_id), push_mode, now_str()))

    sera_user_id = get_sera_user_id()

    if sera_user_id:

        create_message(group_id, sera_user_id,

            f"\n{intervention['message']}",

            client_message_id=f"intervention-{log_id}",

            role="agent", linked_log_id=log_id)

    return log_id



def reset_experiment_data(clear_files=False):

    tables = (
        "data_quality_items",
        "emotion_feedback_generations",
        "emotion_feedback_assessments",
        "emotion_reflection_slots",
        "group_discussion_entries",
        "submission_prepares",
        "collaborative_document_checkpoints",
        "questionnaire_responses",
        "questionnaire_submissions",
        "questionnaire_publications",
        "collaboration_state_finalizations",
        "collaboration_state_segments",
        "discussion_assessment_cursors",
        "state_assessment_batches",
        "strategy_pipeline_runs",
        "agent_research_events",
        "intervention_feedback",
        "intervention_logs",
        "intervention_runs",
        "intervention_decisions",
        "intervention_uptake",
        "monitor_runs",
        "state_assessments",
        "group_states",
        "help_requests",
        "agent_suggestions",
        "interventions",
        "messages",
        "emotion_checkins",
        "submissions",
        "manual_state_annotations",
        "process_events",
        "autonomous_regulation_events",
        "safety_signals",
        "deliverable_scores",
        "data_quality_checks",
        "group_session_controls",
        "group_session_discussions",
        "collaborative_documents",
    )

    conn = db()

    try:
        existing_tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        for table in tables:
            if table not in existing_tables:
                continue

            conn.execute(f'DELETE FROM "{table}"')

        conn.commit()

        return True

    except Exception:

        conn.rollback()

        return False

    finally:

        conn.close()



def copy_questionnaire(questionnaire_id, created_by=None):

    original = query_one("SELECT * FROM questionnaires WHERE id=?", (questionnaire_id,))

    if not original:

        return None

    items = query_all("SELECT * FROM questionnaire_items WHERE questionnaire_id=?", (questionnaire_id,))

    created = now_str()

    conn = db()

    try:

        cur = conn.execute("""

            INSERT INTO questionnaires(code, category_key, title, description,

                timing, scale_max, active, sort_order, created_by, created_at, updated_at)

            VALUES(?,?,?,?,?,?,?,?,?,?,?)

        """, (

            original["code"] + "_copy", original["category_key"],

            original["title"] + " ()", original["description"],

            original["timing"], original["scale_max"], 0,

            int(query_one("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM questionnaires WHERE code=?",

                          (original["code"],))["n"] or 0),

            created_by, created, created,

        ))

        new_id = cur.lastrowid

        _replace_questionnaire_items(conn, new_id,

            _normalize_questionnaire_items([dict(it) for it in items]), created)

        conn.commit()

        return dict(conn.execute("SELECT * FROM questionnaires WHERE id=?", (new_id,)).fetchone())

    finally:

        conn.close()



def set_questionnaire_active(questionnaire_id, active):

    execute("UPDATE questionnaires SET active=? WHERE id=?", (1 if active else 0, questionnaire_id,))

    return query_one("SELECT * FROM questionnaires WHERE id=?", (questionnaire_id,))



def set_questionnaire_sort_order(questionnaire_id, sort_order):

    execute("UPDATE questionnaires SET sort_order=? WHERE id=?", (sort_order, questionnaire_id,))

    return query_one("SELECT * FROM questionnaires WHERE id=?", (questionnaire_id,))



def batch_set_questionnaire_active(ids, active):

    for qid in ids:

        execute("UPDATE questionnaires SET active=? WHERE id=?", (1 if active else 0, qid,))

# Manual annotation functions

MANUAL_ANNOTATION_STATE_CODES = set()



def get_manual_annotation_state_label(state_code):

    return state_code or ""



def save_manual_state_annotation(assessment_id, user_id, manual_state_code, *, should_intervene=False, note=""):

    conn = db()

    try:

        cur = conn.execute("""

            INSERT INTO manual_state_annotations(assessment_id, user_id,

                manual_state_code, should_intervene, note, created_at)

            VALUES(?,?,?,?,?,?)

        """, (assessment_id, user_id, manual_state_code,

              1 if should_intervene else 0, note, now_str()))

        ann_id = cur.lastrowid

        conn.commit()

        return {"id": ann_id, "assessment_id": assessment_id, "user_id": user_id,

                "manual_state_code": manual_state_code,

                "should_intervene": bool(should_intervene), "note": note}

    except Exception:

        conn.rollback()

        return None

    finally:

        conn.close()



# Teacher dashboard detail

def get_teacher_group_detail_payload(group_id):

    group = query_one("SELECT * FROM groups WHERE id=?", (group_id,))

    if not group:

        return None

    return {

        "group": dict(group),

        "latest_suggestion": get_latest_agent_suggestion(group_id),

        "latest_decision": get_teacher_group_dashboard_rollup(group_id).get("latest_decision"),

    }



# Ensure database ready


# ============================================================
# Teacher Console v1: Helper functions
# ============================================================

def get_active_experiment_session():
    """Return the current active (running) experiment session row, or None."""
    try:
        sid = get_setting("current_session_id")
        if not sid:
            return None
        row = query_one(
            "SELECT * FROM experiment_sessions WHERE id=? AND status='running'",
            (int(sid),)
        )
        return dict(row) if row else None
    except Exception:
        return None


def get_active_session_id():
    """Return the current active experiment session id, or None."""
    session = get_active_experiment_session()
    return int(session["id"]) if session else None


def derive_agent_flags(session_role):
    """Return agent flags based on session role.
    S1_baseline: detection only, no agents.
    S2_intervention: full strategy + emotion agents.
    S3_withdrawal: detection only, no agents.
    Returns dict with agent_detection_enabled, agent_intervention_enabled,
    strategy_agent_enabled, emotion_agent_enabled."""
    flags = {
        "S1_baseline": {"agent_mode": "strategy", "agent_detection_enabled": True, "agent_intervention_enabled": True, "strategy_agent_enabled": True, "emotion_agent_enabled": False},
        "S2_intervention": {"agent_mode": "strategy", "agent_detection_enabled": True, "agent_intervention_enabled": True, "strategy_agent_enabled": True, "emotion_agent_enabled": False},
        "S3_withdrawal": {"agent_mode": "none", "agent_detection_enabled": True, "agent_intervention_enabled": False, "strategy_agent_enabled": False, "emotion_agent_enabled": False},
    }
    return flags.get(
        session_role,
        {"agent_mode": "strategy", "agent_detection_enabled": True, "agent_intervention_enabled": True, "strategy_agent_enabled": True, "emotion_agent_enabled": False},
    )


def write_audit_log(*, operator_id, action_type, target_type=None, target_id=None,
                     before_value=None, after_value=None, reason=None):
    """Write an audit log entry. All parameters are keyword-only."""
    execute("""INSERT INTO audit_logs(operator_id, action_type, target_type, target_id,
    before_value, after_value, reason, created_at)
    VALUES(?,?,?,?,?,?,?,?)""", (
        operator_id,
        action_type[:100] if action_type else "",
        target_type[:100] if target_type else None,
        target_id,
        str(before_value) if before_value is not None else None,
        str(after_value) if after_value is not None else None,
        reason[:1000] if reason else None,
        now_str(),
    ))


def stamp_session_fields(payload=None):
    """If an active experiment session exists, inject session_id and task_id
    into the payload dict. Returns updated dict. Does not overwrite existing keys.
    """
    session = get_active_experiment_session()
    if not session:
        return payload if payload is not None else {}
    result = dict(payload) if payload else {}
    if "session_id" not in result:
        result["session_id"] = session["id"]
    if "task_id" not in result:
        result["task_id"] = session.get("task_id")
    return result


def require_running_session():
    """Return current running session context or raise an API-appropriate error."""
    ctx = get_current_running_session_context()
    if not ctx:
        raise ValueError("当前没有正在进行的实验课次，请等待教师开始")
    return ctx


def database_is_ready():

    if not os.path.exists(DB_PATH):

        return False

    try:

        existing_tables = get_existing_tables()

        return REQUIRED_TABLES.issubset(existing_tables)

    except sqlite3.DatabaseError:

        return False



def get_existing_tables():

    conn = db()

    try:

        rows = conn.execute(

            "SELECT name FROM sqlite_master WHERE type='table'"

        ).fetchall()

        return {row["name"] for row in rows}

    finally:

        conn.close()



def ensure_database_ready():

    global DB_READY

    if DB_READY and database_is_ready():

        return

    try:

        init_db()

        DB_READY = True

    except sqlite3.DatabaseError:

        if os.path.exists(DB_PATH):

            broken_path = DB_PATH + ".broken_" + datetime.now().strftime("%Y%m%d%H%M%S")

            os.replace(DB_PATH, broken_path)

            print(f"{broken_path}")

        init_db()

        DB_READY = True



def init_db():

    logger.info(
        "database_paths business=%s queue=%s",
        os.path.realpath(os.path.abspath(DB_PATH)),
        os.path.realpath(os.path.abspath(HUEY_DB_PATH)),
    )
    conn = db()

    try:

        ensure_settings_table(conn)

        _create_core_tables(conn)

        _create_discussion_tables(conn)

        _create_questionnaire_tables(conn)

        _create_event_tables(conn)

        _create_help_tables(conn)

        _create_additional_tables(conn)

        ensure_experiment_identity_tables(conn)

        _seed_default_data(conn)

        conn.commit()

        from migrations import run_pending_migrations

        run_pending_migrations(conn)

    finally:

        conn.close()



def _create_core_tables(conn):

    conn.execute("""

        CREATE TABLE IF NOT EXISTS users (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            username TEXT UNIQUE NOT NULL,

            password_hash TEXT NOT NULL,

            real_name TEXT NOT NULL,

            participant_code TEXT,

            role TEXT NOT NULL DEFAULT 'student',

            created_at TEXT NOT NULL

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS groups (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL,

            group_code TEXT,

            condition TEXT DEFAULT 'experiment',

            state TEXT NOT NULL DEFAULT 'OPEN',

            version INTEGER NOT NULL DEFAULT 0,

            last_message_sequence INTEGER NOT NULL DEFAULT 0,

            last_analyzed_sequence INTEGER,

            last_llm_sequence INTEGER,

            last_intervention_at TEXT,

            lock_token TEXT,

            lock_expires_at TEXT,

            active_intervention_run_id INTEGER,

            cutoff_sequence INTEGER DEFAULT 0,

            last_v2_monitor_run_id INTEGER,

            auto_intervention_enabled INTEGER DEFAULT 1,

            created_at TEXT NOT NULL

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS group_members (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            group_id INTEGER NOT NULL,

            user_id INTEGER NOT NULL,

            FOREIGN KEY(group_id) REFERENCES groups(id),

            FOREIGN KEY(user_id) REFERENCES users(id)

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS client_sessions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            token TEXT UNIQUE NOT NULL,

            user_id INTEGER NOT NULL,

            role TEXT,

            login_method TEXT,

            created_at TEXT NOT NULL,

            last_seen TEXT NOT NULL,

            FOREIGN KEY(user_id) REFERENCES users(id)

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS tasks (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            title TEXT,

            description TEXT,

            is_active INTEGER DEFAULT 0,

            created_at TEXT NOT NULL

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS learning_tasks (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            title TEXT,

            description TEXT,

            question TEXT,

            keywords TEXT,

            is_active INTEGER DEFAULT 0,

            sort_order INTEGER DEFAULT 0,

            created_at TEXT NOT NULL

        )

    """)



def _create_discussion_tables(conn):

    conn.execute("""

        CREATE TABLE IF NOT EXISTS messages (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            group_id INTEGER NOT NULL,

            user_id INTEGER NOT NULL,

            content TEXT NOT NULL,

            sequence INTEGER,

            sender_type TEXT,

            role TEXT,

            session_no INTEGER,

            task_id INTEGER,

            client_message_id TEXT,

            reply_to_message_id INTEGER,

            strategy_id TEXT,

            linked_log_id INTEGER,

            intervention_run_id INTEGER,

            metadata_json TEXT,

            trigger_source TEXT,

            discussion_id INTEGER,

            scope_resolved_from TEXT,

            legacy_scope_fallback INTEGER NOT NULL DEFAULT 0,

            scope_fallback_reason TEXT,

            created_at TEXT NOT NULL,

            FOREIGN KEY(group_id) REFERENCES groups(id),

            FOREIGN KEY(user_id) REFERENCES users(id)

        )

    """)

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_group_sequence ON messages(group_id, sequence)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_user_client_msg_id ON messages(group_id, user_id, client_message_id)")
    conn.execute("")
    conn.execute("""

        CREATE TABLE IF NOT EXISTS group_states (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            group_id INTEGER NOT NULL,

            state_code TEXT,

            state_label TEXT,

            risk_level INTEGER DEFAULT 0,

            state_score REAL DEFAULT 0.0,

            evidence TEXT,

            context_json TEXT,

            feature_json TEXT,

            rule_assessment_json TEXT,

            session_no INTEGER,

            task_id INTEGER,

            session_id INTEGER,

            discussion_id INTEGER,

            created_at TEXT NOT NULL,

            FOREIGN KEY(group_id) REFERENCES groups(id)

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS state_assessments (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            group_id INTEGER NOT NULL,

            state_code TEXT,

            state_score REAL,

            rule_result_json TEXT,

            llm_result_json TEXT,

            fusion_json TEXT,

            evidence TEXT,

            session_no INTEGER,

            task_id INTEGER,

            discussion_id INTEGER,

            created_at TEXT NOT NULL

        )

    """)

    conn.execute("""

        CREATE TABLE IF NOT EXISTS monitor_runs (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            group_id INTEGER NOT NULL,

            cutoff_sequence INTEGER,

            trigger_type TEXT,

            rule_result_json TEXT,

            llm_result_json TEXT,

            final_state TEXT,

            confidence REAL,

            status TEXT NOT NULL DEFAULT 'pending',

            analyzer_version TEXT,

            shadow INTEGER DEFAULT 0,

            state_assessment_id INTEGER,

            session_id INTEGER,

            session_no INTEGER,

            discussion_id INTEGER,

            task_id INTEGER,

            scope_resolved_from TEXT,

            legacy_scope_fallback INTEGER NOT NULL DEFAULT 0,

            scope_fallback_reason TEXT,

            decision TEXT,

            teacher_reason TEXT,

            message_id INTEGER,

            lock_acquired INTEGER DEFAULT 0,

            cooldown_result TEXT,

            context_from_sequence INTEGER,

            context_to_sequence INTEGER,

            input_message_sequences_json TEXT,

            evidence_sequences_json TEXT,

            review_decision TEXT,

            review_final_state TEXT,

            review_confidence REAL,

            review_reason TEXT,

            selected_strategy_id TEXT,

            generated_message TEXT,

            prompt_version TEXT,

            review_started_at TEXT,

            review_completed_at TEXT,

            review_error TEXT,

            failure_reason TEXT,

            created_at TEXT NOT NULL,

            completed_at TEXT,

            FOREIGN KEY(group_id) REFERENCES groups(id)

        )

    """)

    _create_collaboration_state_segment_tables(conn)
    _create_incremental_state_assessment_tables(conn)
    _create_collaboration_state_finalization_tables(conn)


def _create_collaboration_state_segment_tables(conn):
    """Create normalized teacher-facing collaboration state segments.

    ``start_message_id``/``end_message_id`` are retained for compatibility,
    but historically contain ``messages.sequence`` rather than message PKs.
    New incremental assessment code writes the explicit sequence columns too.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_state_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER,
            session_no INTEGER,
            task_id INTEGER,
            discussion_id INTEGER,
            state_code TEXT NOT NULL,
            segment_kind TEXT NOT NULL CHECK(segment_kind IN ('message_range','time_range')),
            start_message_id INTEGER,
            end_message_id INTEGER,
            assessment_batch_id INTEGER,
            start_sequence INTEGER,
            end_sequence INTEGER,
            start_at TEXT,
            end_at TEXT,
            trigger_sequence INTEGER,
            raw_silence_started_at TEXT,
            threshold_reached_at TEXT,
            detected_at TEXT,
            last_observed_at TEXT,
            silent_seconds_at_detection INTEGER,
            is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
            resolution_reason TEXT,
            silence_event_key TEXT,
            intervention_scheduled_at TEXT,
            intervention_run_id INTEGER,
            intervention_published_at TEXT,
            intervention_disposition TEXT,
            evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_sequences TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            fallback_reason TEXT,
            source TEXT NOT NULL CHECK(source IN (
                'strategy_llm','silence_rule','session_finalizer','state_monitor',
                'rule','llm','legacy'
            )),
            assessment_status TEXT,
            segment_order INTEGER,
            is_active_at_batch_end INTEGER CHECK(
                is_active_at_batch_end IS NULL OR is_active_at_batch_end IN (0,1)
            ),
            trigger_type TEXT,
            source_run_id INTEGER,
            assessment_id INTEGER,
            analysis_anchor_message_id INTEGER,
            analysis_window_start_message_id INTEGER,
            analysis_window_end_message_id INTEGER,
            previous_student_message_id INTEGER,
            next_student_message_id INTEGER,
            gap_seconds INTEGER,
            prompt_version TEXT,
            is_finalized INTEGER NOT NULL DEFAULT 0 CHECK(is_finalized IN (0,1)),
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            CHECK(
                (
                    segment_kind='message_range'
                    AND start_message_id IS NOT NULL
                    AND end_message_id IS NOT NULL
                    AND start_at IS NULL
                    AND end_at IS NULL
                    AND previous_student_message_id IS NULL
                    AND next_student_message_id IS NULL
                    AND gap_seconds IS NULL
                )
                OR
                (
                    segment_kind='time_range'
                    AND start_message_id IS NULL
                    AND end_message_id IS NULL
                    AND start_at IS NOT NULL
                    AND (
                        (is_active=1 AND end_at IS NULL)
                        OR
                        (is_active=0 AND end_at IS NOT NULL)
                    )
                )
            ),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(assessment_batch_id) REFERENCES state_assessment_batches(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_group_session
        ON collaboration_state_segments(group_id, session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_group_session_no
        ON collaboration_state_segments(group_id, session_no)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_state
        ON collaboration_state_segments(state_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_message_range
        ON collaboration_state_segments(start_message_id, end_message_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_time_range
        ON collaboration_state_segments(start_at, end_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_source_run
        ON collaboration_state_segments(source_run_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_anchor
        ON collaboration_state_segments(group_id, session_id, analysis_anchor_message_id, is_finalized)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_segments_dedupe
        ON collaboration_state_segments(dedupe_key)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_segments_silence_event
        ON collaboration_state_segments(silence_event_key)
        WHERE silence_event_key IS NOT NULL
    """)


def _create_incremental_state_assessment_tables(conn):
    """Create batch and cursor storage for discussion-scoped LLM assessment."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state_assessment_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            session_no INTEGER,
            task_id INTEGER,
            discussion_id INTEGER NOT NULL,
            candidate_start_sequence INTEGER NOT NULL,
            candidate_end_sequence INTEGER NOT NULL,
            context_start_sequence INTEGER,
            context_end_sequence INTEGER,
            trigger_type TEXT NOT NULL,
            trigger_sequence INTEGER,
            window_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','succeeded','failed','superseded')),
            rerun_requested INTEGER NOT NULL DEFAULT 0
                CHECK(rerun_requested IN (0,1)),
            request_priority INTEGER NOT NULL DEFAULT 0,
            last_trigger_sequence INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            next_retry_at TEXT,
            enqueued_at TEXT,
            model TEXT,
            prompt_version TEXT,
            raw_response TEXT,
            parsed_response TEXT,
            error_code TEXT,
            error_detail TEXT,
            student_sequences_json TEXT,
            terminal_status TEXT,
            terminal_at TEXT,
            fallback_action TEXT,
            fallback_segment_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            CHECK(candidate_start_sequence <= candidate_end_sequence),
            UNIQUE(
                group_id, session_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence
            ),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(session_id) REFERENCES experiment_sessions(id),
            FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_active
        ON state_assessment_batches(group_id, session_id, discussion_id, status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_recent_success
        ON state_assessment_batches(
            group_id, session_id, discussion_id, status, completed_at DESC, id DESC
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_window_key
        ON state_assessment_batches(window_key)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_status
        ON state_assessment_batches(status)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discussion_assessment_cursors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            session_no INTEGER,
            task_id INTEGER,
            discussion_id INTEGER NOT NULL,
            last_finalized_student_sequence INTEGER NOT NULL DEFAULT 0,
            last_scheduled_student_sequence INTEGER NOT NULL DEFAULT 0,
            last_assessment_requested_at TEXT,
            last_assessment_completed_at TEXT,
            last_scheduling_completed_at TEXT,
            last_intervention_sequence INTEGER,
            observation_started_sequence INTEGER,
            observation_status TEXT NOT NULL DEFAULT 'inactive'
                CHECK(observation_status IN ('inactive','observing')),
            updated_at TEXT NOT NULL,
            UNIQUE(group_id, session_id, discussion_id),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(session_id) REFERENCES experiment_sessions(id),
            FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_discussion_assessment_cursors_observation
        ON discussion_assessment_cursors(
            group_id, session_id, discussion_id, observation_status
        )
    """)


def _create_collaboration_state_finalization_tables(conn):
    """Create audit records for end-of-discussion state finalization."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_state_finalizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER,
            session_no INTEGER,
            task_id INTEGER,
            discussion_id INTEGER,
            status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
            reason TEXT,
            analysis_start_message_id INTEGER,
            analysis_end_message_id INTEGER,
            source_run_id INTEGER,
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(group_id) REFERENCES groups(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_finalizations_group_session
        ON collaboration_state_finalizations(group_id, session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_finalizations_status
        ON collaboration_state_finalizations(status)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_finalizations_dedupe
        ON collaboration_state_finalizations(dedupe_key)
    """)


def _create_questionnaire_tables(conn):
    """Create questionnaire-related tables."""
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaires (
id INTEGER PRIMARY KEY AUTOINCREMENT,
code TEXT, category_key TEXT, title TEXT,
description TEXT, timing TEXT,
scale_max INTEGER DEFAULT 5,
active INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0,
created_by INTEGER, created_at TEXT, updated_at TEXT)
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_items (
id INTEGER PRIMARY KEY AUTOINCREMENT,
questionnaire_id INTEGER NOT NULL,
item_code TEXT, prompt_text TEXT,
dimension_label TEXT, sort_order INTEGER DEFAULT 0,
required INTEGER DEFAULT 1, created_at TEXT,
question_type TEXT DEFAULT 'likert_5',
dimension_key TEXT DEFAULT '',
reverse_scored INTEGER DEFAULT 0,
min_value INTEGER DEFAULT 1,
max_value INTEGER DEFAULT 5,
options_json TEXT,
score_map_json TEXT,
include_in_score INTEGER DEFAULT 1,
help_text TEXT DEFAULT '',
section_no INTEGER DEFAULT 1,
section_title TEXT DEFAULT '',
scale_labels_json TEXT,
FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id))
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_responses (
id INTEGER PRIMARY KEY AUTOINCREMENT,
questionnaire_id INTEGER NOT NULL,
user_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
response_value INTEGER, response_text TEXT,
response_option_key TEXT,
response_stage TEXT, session_no INTEGER, group_id INTEGER,
session_id INTEGER, submission_id INTEGER,
created_at TEXT,
FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id),
FOREIGN KEY(user_id) REFERENCES users(id),
FOREIGN KEY(item_id) REFERENCES questionnaire_items(id))
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_sections (
id INTEGER PRIMARY KEY AUTOINCREMENT,
questionnaire_id INTEGER NOT NULL,
section_key TEXT DEFAULT '',
title TEXT DEFAULT '',
description TEXT DEFAULT '',
sort_order INTEGER DEFAULT 0,
created_at TEXT,
FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id))
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_submissions (
id INTEGER PRIMARY KEY AUTOINCREMENT,
response_batch_id TEXT,
questionnaire_id INTEGER NOT NULL,
user_id INTEGER NOT NULL,
group_id INTEGER,
session_id INTEGER,
session_no INTEGER,
response_stage TEXT NOT NULL,
status TEXT DEFAULT 'submitted',
submitted_at TEXT,
created_at TEXT,
FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id),
FOREIGN KEY(user_id) REFERENCES users(id))
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_qsubmissions_q_u_session_stage
        ON questionnaire_submissions(questionnaire_id, user_id, session_id, response_stage)
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qsections_q_sort ON questionnaire_sections(questionnaire_id, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qsections_q_key ON questionnaire_sections(questionnaire_id, section_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qsubmissions_session_group ON questionnaire_submissions(session_id, group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qitems_q_section_sort ON questionnaire_items(questionnaire_id, section_no, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qresponses_submission ON questionnaire_responses(submission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qresponses_q_u_session ON questionnaire_responses(questionnaire_id, user_id, session_id)")


def _create_event_tables(conn):
    """Create event/process tables."""
    conn.execute("""CREATE TABLE IF NOT EXISTS process_events (
id INTEGER PRIMARY KEY AUTOINCREMENT,
event_type TEXT NOT NULL, source TEXT,
group_id INTEGER, user_id INTEGER,
related_table TEXT, related_id INTEGER,
event_key TEXT, payload TEXT, created_at TEXT NOT NULL)
    """)


def _create_help_tables(conn):
    """Create help-request and intervention-run tables."""
    conn.execute("""CREATE TABLE IF NOT EXISTS help_requests (
id INTEGER PRIMARY KEY AUTOINCREMENT,
group_id INTEGER NOT NULL, requester_id INTEGER NOT NULL, task_id INTEGER, session_no INTEGER, session_id INTEGER, discussion_id INTEGER,
status TEXT DEFAULT 'QUEUED',
request_text TEXT, response_message TEXT,
intent TEXT, fallback_used INTEGER DEFAULT 0,
source_message_id INTEGER,
help_request_message_sequence INTEGER,
handled_at TEXT,
handling_status TEXT,
covered_until_sequence INTEGER,
handled_state_code TEXT,
handled_segment_id INTEGER,
handled_evidence_start_sequence INTEGER,
handled_evidence_end_sequence INTEGER,
response_message_id INTEGER,
intervention_run_id INTEGER,
failure_reason TEXT,
created_at TEXT, completed_at TEXT,
FOREIGN KEY(group_id) REFERENCES groups(id),
FOREIGN KEY(requester_id) REFERENCES users(id))
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS intervention_runs (
id INTEGER PRIMARY KEY AUTOINCREMENT,
monitor_run_id INTEGER,
state_assessment_id INTEGER,
group_id INTEGER NOT NULL,
 session_id INTEGER,
 discussion_id INTEGER,
 session_no INTEGER,
 task_id INTEGER,
cutoff_sequence INTEGER,
status TEXT DEFAULT 'pending',
decision TEXT,
assessment_batch_id INTEGER,
target_segment_id INTEGER,
trigger_type TEXT,
reason_code TEXT,
active_segment_index INTEGER,
guard_result TEXT,
guard_reason TEXT,
retry_count INTEGER DEFAULT 0,
raw_response TEXT,
started_at TEXT,
candidate_strategies TEXT,
selected_strategy TEXT,
sub_category TEXT,
strategy_pool_json TEXT NOT NULL DEFAULT '[]',
strategy_source TEXT,
detected_state TEXT,
confidence REAL,
context_from_sequence INTEGER,
context_to_sequence INTEGER,
input_message_sequences_json TEXT,
evidence_sequences_json TEXT,
selected_strategy_id TEXT,
generated_message TEXT,
teacher_reason TEXT,
message_id INTEGER,
help_request_id INTEGER,
prompt_version TEXT,
fallback_used INTEGER DEFAULT 0,
strategy_id INTEGER,
strategy_version TEXT,
model_profile TEXT,
latency_ms INTEGER,
validated INTEGER DEFAULT 0,
validation_error TEXT,
lock_token TEXT,
lock_acquired INTEGER DEFAULT 0,
cooldown_result TEXT,
failure_reason TEXT,
created_at TEXT, completed_at TEXT,
FOREIGN KEY(group_id) REFERENCES groups(id))
    """)


def _create_additional_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        task_id INTEGER, session_no INTEGER,
        content TEXT, file_name TEXT, stored_file_name TEXT,
        file_path TEXT, file_size INTEGER DEFAULT 0,
        submission_mode TEXT DEFAULT 'text',
        scored_by INTEGER,
        submitted_at TEXT, created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS emotion_checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, session_no INTEGER,
        emotion_option TEXT, checkin_type TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, state_code TEXT, state_label TEXT,
        ssrl_phase TEXT, strategy_type TEXT, confidence REAL,
        evidence TEXT, context_summary TEXT,
        intervention_id INTEGER, title TEXT, message TEXT,
        status TEXT DEFAULT 'pending', source TEXT,
        sub_category TEXT, strategy_id TEXT, strategy_name TEXT,
        cognitive_load TEXT, should_intervene INTEGER,
        is_oi_suppressed INTEGER, condition TEXT,
        intended_strategy_id TEXT, analysis_mode TEXT,
        llm_analysis_json TEXT, trigger_source TEXT,
        decision_id INTEGER, template_id INTEGER,
        help_request_id INTEGER,
        allowed_auto_push INTEGER DEFAULT 0,
        strategy_version TEXT,
        model_name TEXT,
        prompt_version TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS intervention_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, intervention_id INTEGER,
        pushed_by_user_id INTEGER, push_mode TEXT,
        title TEXT, message TEXT, condition TEXT,
        trigger_source TEXT,
        message_id INTEGER,
        help_request_id INTEGER,
        state_assessment_id INTEGER,
        monitor_run_id INTEGER,
        intervention_run_id INTEGER,
        agent_type TEXT,
        session_id INTEGER,
        session_no INTEGER,
        task_id INTEGER,
        discussion_id INTEGER,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS intervention_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        rating TEXT, note TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS interventions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, message TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS intervention_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assessment_id INTEGER, group_id INTEGER,
        should_intervene INTEGER DEFAULT 0,
        decision_reason TEXT, suppressed_reason TEXT,
        target TEXT, priority INTEGER,
        strategy_category TEXT, selected_strategy_id TEXT,
        condition TEXT, decision_version TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_state_annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assessment_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        manual_state_code TEXT, should_intervene INTEGER DEFAULT 0,
        note TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS collaborative_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER REFERENCES experiment_sessions(id),
        group_id INTEGER NOT NULL REFERENCES groups(id),
        task_id INTEGER NOT NULL REFERENCES learning_tasks(id),
        session_no INTEGER NOT NULL DEFAULT 0,
        title TEXT,
        y_state BLOB,
        content_json TEXT,
        content_html TEXT,
        content_text TEXT,
        status TEXT NOT NULL DEFAULT 'editing' CHECK(status IN ('editing','returned','submitted','locked')),
        state_revision INTEGER NOT NULL DEFAULT 0,
        state_size_bytes INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        submitted_at TEXT,
        UNIQUE(group_id, task_id, session_no)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS collaborative_document_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES collaborative_documents(id) ON DELETE CASCADE,
        state_revision INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT 'manual' CHECK(reason IN ('submitted','returned','manual')),
        y_state BLOB,
        content_json TEXT,
        content_html TEXT,
        content_text TEXT,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_docs_group_task_session
        ON collaborative_documents(group_id, task_id, session_no)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_docs_status
        ON collaborative_documents(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_docs_session_group
        ON collaborative_documents(session_id, group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_checkpoints_doc_revision
        ON collaborative_document_checkpoints(document_id, state_revision)
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS submission_prepares (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES collaborative_documents(id),
        freeze_id TEXT NOT NULL,
        state_revision INTEGER NOT NULL DEFAULT 0,
        committed INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_submission_prepares_doc
        ON submission_prepares(document_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_submission_prepares_freeze
        ON submission_prepares(freeze_id)
    """)



def _seed_default_data(conn):
    """Seed default users (teacher, SERA agent) for test and demo environments."""
    from werkzeug.security import generate_password_hash
    from config import CONSENT_VERSION

    # Teacher user
    existing = conn.execute("SELECT id FROM users WHERE username=?", ("teacher",)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO users(username, password_hash, real_name, role, created_at) VALUES(?,?,?,?,?)",
            (
                "teacher",
                generate_password_hash(os.urandom(24).hex()),
                "Teacher",
                "teacher",
                now_str(),
            ),
        )

    # SERA agent
    existing = conn.execute("SELECT id FROM users WHERE username=?", ("sera",)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO users(username, password_hash, real_name, role, created_at) VALUES(?,?,?,?,?)",
            ("sera", "*", "SERA", "agent", now_str()),
        )

def create_student_user(*, username, password_hash, real_name, group_id, guardian_consent=False, consent_ack=False):
    """[DEPRECATED] Create a student user via old registration flow.
    
    Returns (user_id, participant_code).
    Called by routes/pages.py register view.

    DEPRECATED: Use experiment_participants + key login instead.
    Kept for backward compatibility during migration period.
    Will be removed in a future cleanup.
    """
    from config import CONSENT_VERSION
    user_id = execute(
        "INSERT INTO users(username, password_hash, real_name, role, guardian_consent, consent_ack, consent_version, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (username, password_hash, real_name, "student",
         1 if guardian_consent else 0,
         1 if consent_ack else 0,
         CONSENT_VERSION, now_str()),
    )
    participant_code = f"P{user_id:04d}"
    execute("UPDATE users SET participant_code=? WHERE id=?", (participant_code, user_id))
    # Add user to group (inline add_user_to_group logic to avoid circular import)
    existing = query_one(
        "SELECT id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if existing:
        execute("UPDATE group_members SET group_id=? WHERE id=?", (group_id, existing["id"]))
    else:
        execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (group_id, user_id))
    return user_id, participant_code



def ensure_experiment_identity_tables(conn):
    """Create experiment_participants and teacher_access_keys tables.

    Idempotent: safe to call multiple times (CREATE TABLE IF NOT EXISTS).
    Called from init_db() for fresh databases and from
    migrations.run_pending_migrations() for existing databases.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiment_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT UNIQUE NOT NULL,
            login_key_hash TEXT NOT NULL,
            key_lookup_hash TEXT,
            group_no INTEGER NOT NULL,
            member_no INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT,
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teacher_access_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_name TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL,
            key_lookup_hash TEXT,
            teacher_user_id INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            FOREIGN KEY(teacher_user_id) REFERENCES users(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_participants_group_id ON experiment_participants(group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_participants_user_id ON experiment_participants(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_participants_group_member ON experiment_participants(group_no, member_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_participants_lookup_active ON experiment_participants(key_lookup_hash) WHERE is_active=1 AND key_lookup_hash IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_teacher_access_keys_teacher_user_id ON teacher_access_keys(teacher_user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_teacher_access_keys_lookup_active ON teacher_access_keys(key_lookup_hash) WHERE is_active=1 AND key_lookup_hash IS NOT NULL")


def table_exists(name):
    """Check whether a table exists in the current database."""
    return name in get_existing_tables()


def column_exists(table, column):
    """Check whether a column exists in the specified table."""
    conn = db()
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info('" + table + "')").fetchall()}
        return column in existing
    finally:
        conn.close()


def get_experiment_participant_by_code(participant_code):
    """Look up an experiment participant by their participant_code.

    Not yet used for login (reserved for Batch 3).
    """
    return query_one(
        "SELECT * FROM experiment_participants WHERE participant_code=?",
        (participant_code,),
    )


def get_experiment_participant_by_user_id(user_id):
    """Look up an experiment participant by their user_id."""
    return query_one(
        "SELECT * FROM experiment_participants WHERE user_id=?",
        (user_id,),
    )


def get_teacher_access_key_by_name(key_name):
    """Look up a teacher access key record by its key_name.

    Not yet used for login (reserved for Batch 3).
    """
    return query_one(
        "SELECT * FROM teacher_access_keys WHERE key_name=?",
        (key_name,),
    )


def list_experiment_participants():
    """Return all experiment participants, ordered by id.

    Intended for testing and data export.
    """
    return query_all("SELECT * FROM experiment_participants ORDER BY id ASC")


# ============================================================
# Agent dual-switch config and research event data access
# ============================================================


def get_session_agent_config(session_id=None, group_id=None):
    """Return agent configuration for a session.

    Args:
        session_id: Direct session lookup.
        group_id: Fallback to find session for group via latest message.

    Returns canonical ``agent_mode`` plus derived compatibility flags.
    """
    session = None
    if session_id:
        session = query_one(
            "SELECT * FROM experiment_sessions WHERE id=?", (session_id,)
        )
    elif group_id:
        session = query_one("""
            SELECT s.* FROM experiment_sessions s
            INNER JOIN messages m ON m.session_id = s.id
            WHERE m.group_id = ?
            ORDER BY m.created_at DESC LIMIT 1
        """, (group_id,))
        if not session:
            session = get_active_experiment_session()

    if not session:
        session = {}
    from services.agent_mode_service import agent_config_from_session
    return agent_config_from_session(dict(session))


def get_agent_intervention_enabled_for_task(task_id, group_id=None):
    """Check whether agent intervention is enabled for a learning task.

    Args:
        task_id: The learning task id.
        group_id: Optional group id for future session-level checks.

    Returns True if the task-level flag allows intervention (defaults to True
    when the column or record is missing).
    """
    row = query_one("SELECT agent_intervention_enabled FROM learning_tasks WHERE id=?", (task_id,))
    if row is None:
        return True
    return bool(row["agent_intervention_enabled"])


def create_agent_research_event(*, group_id, agent_type, event_type,
                                 session_id=None, task_id=None, session_no=None,
                                 discussion_id=None,
                                 enabled_by_config=1, trigger_type=None,
                                 trigger_reason_json=None, monitor_run_id=None,
                                 intervention_run_id=None, message_id=None,
                                 state_before_json=None, context_snapshot_json=None,
                                 llm_prompt_json=None, llm_response_json=None,
                                 validation_json=None,
                                 skip_reason=None, scheduled_at=None,
                                 created_at=None, metadata_json=None):
    """Create a new agent research event record. Returns the new event id."""
    created = created_at or now_str()
    conn = db()
    try:
        if intervention_run_id and discussion_id is None:
            run = conn.execute(
                """
                SELECT session_id, session_no, task_id, discussion_id
                FROM intervention_runs WHERE id=?
                """,
                (intervention_run_id,),
            ).fetchone()
            if run:
                session_id = session_id or run["session_id"]
                session_no = session_no or run["session_no"]
                task_id = task_id or run["task_id"]
                discussion_id = discussion_id or run["discussion_id"]
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            conn,
            group_id=group_id,
            message_id=message_id,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            discussion_id=discussion_id,
            allow_legacy_fallback=False,
        )
        cur = conn.execute(
            """INSERT INTO agent_research_events
               (session_id, task_id, session_no, discussion_id,
                group_id, agent_type, event_type,
                enabled_by_config, trigger_type, trigger_reason_json,
                monitor_run_id, intervention_run_id, message_id,
                state_before_json, context_snapshot_json,
                llm_prompt_json, llm_response_json, validation_json,
                skip_reason, scheduled_at, created_at, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scope.session_id,
                scope.task_id,
                scope.session_no,
                scope.discussion_id,
                group_id,
                agent_type,
                event_type,
                1 if enabled_by_config else 0,
                trigger_type,
                trigger_reason_json,
                monitor_run_id,
                intervention_run_id,
                message_id,
                state_before_json,
                context_snapshot_json,
                llm_prompt_json,
                llm_response_json,
                validation_json,
                skip_reason,
                scheduled_at,
                created,
                metadata_json,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_agent_research_event(event_id, **kwargs):
    """Update an agent research event. Only recognized fields are updated."""
    allowed = {
        "event_type", "enabled_by_config", "trigger_type",
        "trigger_reason_json", "monitor_run_id", "intervention_run_id",
        "message_id", "state_before_json", "context_snapshot_json",
        "llm_prompt_json", "llm_response_json", "validation_json",
        "scheduled_at", "published_at", "skipped_at", "skip_reason",
        "metadata_json", "discussion_id",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return False
    sets = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [event_id]
    conn = db()
    try:
        conn.execute(f"UPDATE agent_research_events SET {sets} WHERE id=?", values)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def attach_message_to_agent_event(event_id, message_id, agent_type):
    """Link a message to an agent research event.

    Updates both the agent_research_events record and the messages table.
    """
    conn = db()
    try:
        conn.execute(
            "UPDATE messages SET agent_type=?, agent_event_id=? WHERE id=?",
            (agent_type, event_id, message_id),
        )
        conn.execute(
            "UPDATE agent_research_events SET message_id=? WHERE id=?",
            (message_id, event_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def snapshot_teacher_agent_config(session_id):
    """Create a JSON snapshot of the current teacher agent config for a session.

    Returns the JSON string, or None if session is not found.
    """
    session = query_one(
        "SELECT agent_mode, strategy_agent_enabled, emotion_agent_enabled, "
        "agent_detection_enabled, agent_intervention_enabled, "
        "research_state_monitoring_enabled "
        "FROM experiment_sessions WHERE id=?",
        (session_id,),
    )
    if not session:
        return None
    from services.agent_mode_service import agent_config_from_session
    payload = dict(session)
    payload.update(agent_config_from_session(payload))
    return json.dumps(payload, ensure_ascii=False)

# ============================================================
# Batch 6: Fixed questionnaire library, publications, submissions
# ============================================================

def list_fixed_questionnaires():
    """List fixed questionnaires (is_fixed=1) with item/section counts and publication counts."""
    rows = query_all("""
        SELECT q.*,
               (SELECT COUNT(*) FROM questionnaire_items qi WHERE qi.questionnaire_id = q.id) AS item_count,
               (SELECT COUNT(DISTINCT qi.section_no) FROM questionnaire_items qi WHERE qi.questionnaire_id = q.id) AS section_count,
               (SELECT COUNT(*) FROM questionnaire_publications qp WHERE qp.questionnaire_id = q.id) AS publication_count,
               (SELECT COUNT(*) FROM questionnaire_publications qp WHERE qp.questionnaire_id = q.id AND qp.status = 'enabled') AS active_publication_count
        FROM questionnaires q
        WHERE q.is_fixed = 1
        ORDER BY q.sort_order ASC, q.id ASC
    """)
    result = [dict(r) for r in rows]
    for q in result:
        items = query_all(
            "SELECT * FROM questionnaire_items WHERE questionnaire_id=? ORDER BY section_no ASC, sort_order ASC, id ASC",
            (q['id'],),
        )
        q['items'] = [_questionnaire_item_dict(it) for it in items]
    return result


def is_fixed_questionnaire(qid):
    """Return True if the questionnaire has is_fixed=1."""
    row = query_one("SELECT is_fixed FROM questionnaires WHERE id=?", (qid,))
    return bool(row and row['is_fixed'])


def _normalize_questionnaire_set_item(item, index):
    if isinstance(item, dict):
        questionnaire_id = item.get("questionnaire_id", item.get("id"))
        sort_order = item.get("sort_order", index)
    else:
        questionnaire_id = item
        sort_order = index
    try:
        questionnaire_id = int(questionnaire_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid questionnaire_id")
    try:
        sort_order = int(sort_order)
    except (TypeError, ValueError):
        sort_order = index
    return questionnaire_id, sort_order


def _validate_active_questionnaires(conn, questionnaire_ids):
    if not questionnaire_ids:
        return
    placeholders = ",".join("?" for _ in questionnaire_ids)
    rows = conn.execute(
        f"SELECT id, active FROM questionnaires WHERE id IN ({placeholders})",
        tuple(questionnaire_ids),
    ).fetchall()
    found = {int(r["id"]): bool(r["active"]) for r in rows}
    missing = [qid for qid in questionnaire_ids if qid not in found]
    inactive = [qid for qid in questionnaire_ids if qid in found and not found[qid]]
    if missing:
        raise ValueError("Questionnaire not found: " + ",".join(str(q) for q in missing))
    if inactive:
        raise ValueError("Questionnaire is not active: " + ",".join(str(q) for q in inactive))


def create_questionnaire_set(name, description="", created_by=None, questionnaire_ids=None):
    """Create a reusable questionnaire set and optionally replace its items."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("Questionnaire set name is required")
    conn = db()
    try:
        now = now_str()
        cur = conn.execute(
            """INSERT INTO questionnaire_sets(name, description, active, created_by, created_at, updated_at)
               VALUES(?,?,?,?,?,?)""",
            (name, description or "", 1, created_by, now, now),
        )
        set_id = cur.lastrowid
        if questionnaire_ids is not None:
            _replace_questionnaire_set_items(conn, set_id, questionnaire_ids, now)
        conn.commit()
        return set_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_questionnaire_set(set_id, name=None, description=None, active=None, questionnaire_ids=None):
    """Update questionnaire set metadata and optionally replace its items."""
    conn = db()
    try:
        existing = conn.execute("SELECT id FROM questionnaire_sets WHERE id=?", (set_id,)).fetchone()
        if not existing:
            raise ValueError("Questionnaire set not found")
        updates = {"updated_at": now_str()}
        if name is not None:
            clean_name = str(name or "").strip()
            if not clean_name:
                raise ValueError("Questionnaire set name is required")
            updates["name"] = clean_name
        if description is not None:
            updates["description"] = description or ""
        if active is not None:
            updates["active"] = 1 if active else 0
        set_clause = ", ".join(f"{field}=?" for field in updates)
        conn.execute(
            f"UPDATE questionnaire_sets SET {set_clause} WHERE id=?",
            tuple(updates.values()) + (set_id,),
        )
        if questionnaire_ids is not None:
            _replace_questionnaire_set_items(conn, set_id, questionnaire_ids, now_str())
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _conn_has_column(conn, table, column):
    return column in {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def _questionnaire_set_is_used(conn, set_id):
    if _conn_has_column(conn, "experiment_sessions", "questionnaire_set_id"):
        row = conn.execute(
            "SELECT 1 FROM experiment_sessions WHERE questionnaire_set_id=? LIMIT 1",
            (set_id,),
        ).fetchone()
        if row:
            return True
    if _conn_has_column(conn, "questionnaire_publications", "questionnaire_set_id"):
        row = conn.execute(
            "SELECT 1 FROM questionnaire_publications WHERE questionnaire_set_id=? LIMIT 1",
            (set_id,),
        ).fetchone()
        if row:
            return True
    return False


def delete_questionnaire_set(set_id):
    """Delete an unused questionnaire set; used sets are deactivated."""
    conn = db()
    try:
        existing = conn.execute("SELECT id FROM questionnaire_sets WHERE id=?", (set_id,)).fetchone()
        if not existing:
            raise ValueError("Questionnaire set not found")
        if _questionnaire_set_is_used(conn, set_id):
            conn.execute(
                "UPDATE questionnaire_sets SET active=0, updated_at=? WHERE id=?",
                (now_str(), set_id),
            )
            deleted = False
        else:
            conn.execute("DELETE FROM questionnaire_set_items WHERE set_id=?", (set_id,))
            conn.execute("DELETE FROM questionnaire_sets WHERE id=?", (set_id,))
            deleted = True
        conn.commit()
        return {"deleted": deleted, "deactivated": not deleted}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_questionnaire_sets(include_inactive=False):
    """List questionnaire sets with their active questionnaire items."""
    where = "" if include_inactive else "WHERE qs.active=1"
    sets = [dict(r) for r in query_all(f"""
        SELECT qs.*, u.real_name AS created_by_name
        FROM questionnaire_sets qs
        LEFT JOIN users u ON qs.created_by = u.id
        {where}
        ORDER BY qs.updated_at DESC, qs.id DESC
    """)]
    for qset in sets:
        items = query_all("""
            SELECT qsi.*, q.title AS questionnaire_title, q.code AS questionnaire_code,
                   q.timing AS questionnaire_timing, q.active AS questionnaire_active,
                   q.is_fixed
            FROM questionnaire_set_items qsi
            JOIN questionnaires q ON q.id = qsi.questionnaire_id
            WHERE qsi.set_id=?
            ORDER BY qsi.sort_order ASC, qsi.id ASC
        """, (qset["id"],))
        qset["items"] = [dict(item) for item in items]
        qset["item_count"] = len(qset["items"])
    return sets


def _replace_questionnaire_set_items(conn, set_id, questionnaire_ids, created):
    existing = conn.execute("SELECT id FROM questionnaire_sets WHERE id=?", (set_id,)).fetchone()
    if not existing:
        raise ValueError("Questionnaire set not found")
    normalized = []
    seen = set()
    for index, item in enumerate(questionnaire_ids or []):
        questionnaire_id, sort_order = _normalize_questionnaire_set_item(item, index)
        if questionnaire_id in seen:
            continue
        seen.add(questionnaire_id)
        normalized.append((questionnaire_id, sort_order))
    _validate_active_questionnaires(conn, [qid for qid, _order in normalized])
    conn.execute("DELETE FROM questionnaire_set_items WHERE set_id=?", (set_id,))
    for questionnaire_id, sort_order in normalized:
        conn.execute(
            """INSERT INTO questionnaire_set_items(set_id, questionnaire_id, sort_order, created_at)
               VALUES(?,?,?,?)""",
            (set_id, questionnaire_id, sort_order, created),
        )


def replace_questionnaire_set_items(set_id, questionnaire_ids):
    """Replace all questionnaires in a set, requiring every questionnaire to be active."""
    conn = db()
    try:
        _replace_questionnaire_set_items(conn, set_id, questionnaire_ids, now_str())
        conn.execute(
            "UPDATE questionnaire_sets SET updated_at=? WHERE id=?",
            (now_str(), set_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def expand_questionnaire_set_for_session(set_id, session_id, group_id=None, user_id=None):
    """Expand a questionnaire set into enabled publications for a session."""
    qset = query_one("SELECT * FROM questionnaire_sets WHERE id=? AND active=1", (set_id,))
    if not qset:
        raise ValueError("Questionnaire set not found or inactive")
    sess = query_one("SELECT id, session_no FROM experiment_sessions WHERE id=?", (session_id,))
    if not sess:
        raise ValueError("Session not found")
    items = query_all("""
        SELECT q.id, q.timing
        FROM questionnaire_set_items qsi
        JOIN questionnaires q ON q.id = qsi.questionnaire_id
        WHERE qsi.set_id=? AND q.active=1
        ORDER BY qsi.sort_order ASC, qsi.id ASC
    """, (set_id,))
    publications = []
    for item in items:
        stages = ["pre", "post"] if item["timing"] == "both" else [item["timing"]]
        for stage in stages:
            existing = query_one(
                "SELECT id FROM questionnaire_publications "
                "WHERE questionnaire_id=? AND session_id=? AND response_stage=? "
                "AND COALESCE(group_id, 0)=COALESCE(?, 0) "
                "AND COALESCE(user_id, 0)=COALESCE(?, 0)",
                (item["id"], session_id, stage, group_id, user_id),
            )
            if existing:
                update_questionnaire_publication(existing["id"], "enabled")
                execute(
                    "UPDATE questionnaire_publications SET questionnaire_set_id=? WHERE id=?",
                    (set_id, existing["id"]),
                )
                publications.append(existing["id"])
            else:
                publications.append(create_questionnaire_publication(
                    item["id"],
                    session_id,
                    sess["session_no"],
                    stage,
                    group_id=group_id,
                    user_id=user_id,
                    questionnaire_set_id=set_id,
                ))
    return publications


def create_questionnaire_publication(questionnaire_id, session_id, session_no, response_stage, group_id=None, user_id=None, questionnaire_set_id=None):
    """Create a questionnaire publication. Returns the new publication id or raises ValueError."""
    stage = response_stage.strip().lower() if response_stage else 'pre'
    if stage not in QUESTIONNAIRE_STAGE_VALUES:
        raise ValueError("Invalid response_stage. Must be 'pre' or 'post'.")
    q = query_one("SELECT id, timing, is_fixed, active FROM questionnaires WHERE id=?", (questionnaire_id,))
    if not q:
        raise ValueError("Questionnaire not found")
    if not q['active']:
        raise ValueError("Questionnaire is not active")
    timing = q['timing']
    if timing != 'both' and timing != stage:
        raise ValueError("Questionnaire timing is not compatible with stage " + stage)
    sess = query_one("SELECT id, session_no FROM experiment_sessions WHERE id=?", (session_id,))
    if not sess:
        raise ValueError("Session not found")
    group_id = int(group_id) if group_id not in (None, "", 0, "0") else None
    user_id = int(user_id) if user_id not in (None, "", 0, "0") else None
    if group_id and not query_one("SELECT id FROM groups WHERE id=?", (group_id,)):
        raise ValueError("Group not found")
    if user_id and not query_one("SELECT id FROM users WHERE id=? AND role='student'", (user_id,)):
        raise ValueError("Student user not found")
    if group_id and user_id:
        member = query_one(
            "SELECT id FROM group_members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        )
        if not member:
            raise ValueError("Student is not a member of the selected group")
    existing = query_one(
        "SELECT id FROM questionnaire_publications "
        "WHERE questionnaire_id=? AND session_id=? AND response_stage=? "
        "AND COALESCE(group_id, 0)=COALESCE(?, 0) "
        "AND COALESCE(user_id, 0)=COALESCE(?, 0)",
        (questionnaire_id, session_id, stage, group_id, user_id),
    )
    if existing:
        raise ValueError("Publication already exists for this questionnaire + session + stage + scope")
    pub_id = execute(
        "INSERT INTO questionnaire_publications(questionnaire_id, session_id, session_no, response_stage, status, group_id, user_id, questionnaire_set_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (questionnaire_id, session_id, session_no, stage, 'enabled', group_id, user_id, questionnaire_set_id, now_str()),
    )
    return pub_id


def list_questionnaire_publications(session_id=None, questionnaire_id=None):
    """List publications with questionnaire and session info."""
    where_clauses = []
    params = []
    if session_id:
        where_clauses.append("qp.session_id=?")
        params.append(session_id)
    if questionnaire_id:
        where_clauses.append("qp.questionnaire_id=?")
        params.append(questionnaire_id)
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    rows = query_all("""
        SELECT qp.*, q.title AS questionnaire_title, q.code AS questionnaire_code,
               q.timing AS questionnaire_timing, q.is_fixed,
               es.session_no AS es_session_no, es.title AS session_title,
               g.name AS group_name, g.group_code,
               u.participant_code AS user_participant_code,
               u.real_name AS user_real_name
        FROM questionnaire_publications qp
        LEFT JOIN questionnaires q ON qp.questionnaire_id = q.id
        LEFT JOIN experiment_sessions es ON qp.session_id = es.id
        LEFT JOIN groups g ON qp.group_id = g.id
        LEFT JOIN users u ON qp.user_id = u.id
        """ + where_sql + """
        ORDER BY qp.created_at DESC
    """, params)
    return [dict(r) for r in rows]


def _legacy_publication_payload(row):
    payload = dict(row)
    payload["stage"] = payload.get("response_stage")
    actual_status = payload.get("status")
    payload["actual_status"] = actual_status
    payload["status"] = "published" if actual_status == "enabled" else actual_status
    payload["legacy_status"] = payload["status"]
    return payload


def publish_questionnaire_to_session(questionnaire_id, session_id, stage="both", group_id=None, user_id=None):
    """Compatibility wrapper for the old publication API.

    Internally this writes explicit response_stage rows with status='enabled'.
    The legacy stage='both' expands according to the questionnaire timing.
    """
    q = query_one("SELECT id, timing FROM questionnaires WHERE id=?", (questionnaire_id,))
    if not q:
        raise ValueError("Questionnaire not found")
    sess = query_one("SELECT id, session_no FROM experiment_sessions WHERE id=?", (session_id,))
    if not sess:
        raise ValueError("Session not found")

    requested_stage = str(stage or "both").strip().lower()
    if requested_stage == "both":
        stages = ["pre", "post"] if q["timing"] == "both" else [q["timing"]]
    elif requested_stage in QUESTIONNAIRE_STAGE_VALUES:
        stages = [requested_stage]
    else:
        raise ValueError("Invalid stage. Must be 'pre', 'post', or 'both'.")

    created_or_existing = []
    for response_stage in stages:
        existing = query_one(
            "SELECT id FROM questionnaire_publications "
            "WHERE questionnaire_id=? AND session_id=? AND response_stage=? "
            "AND COALESCE(group_id, 0)=COALESCE(?, 0) "
            "AND COALESCE(user_id, 0)=COALESCE(?, 0)",
            (questionnaire_id, session_id, response_stage, group_id, user_id),
        )
        if existing:
            update_questionnaire_publication(existing["id"], "enabled")
            pub_id = existing["id"]
        else:
            pub_id = create_questionnaire_publication(
                questionnaire_id,
                session_id,
                sess["session_no"],
                response_stage,
                group_id=group_id,
                user_id=user_id,
            )
        created_or_existing.append(pub_id)

    rows = query_all(
        "SELECT * FROM questionnaire_publications WHERE id IN ("
        + ",".join("?" for _ in created_or_existing)
        + ") ORDER BY response_stage ASC, id ASC",
        created_or_existing,
    )
    payload = _legacy_publication_payload(rows[0])
    payload["publication_ids"] = created_or_existing
    payload["expanded_response_stages"] = [r["response_stage"] for r in rows]
    return payload


def update_questionnaire_publication(pub_id, status):
    """Update publication status ('enabled' or 'closed')."""
    if status not in ('enabled', 'closed'):
        raise ValueError("Status must be 'enabled' or 'closed'")
    existing = query_one("SELECT id, status FROM questionnaire_publications WHERE id=?", (pub_id,))
    if not existing:
        raise ValueError("Publication not found")
    execute("UPDATE questionnaire_publications SET status=? WHERE id=?", (status, pub_id))
    return True


def set_questionnaire_publication_status(pub_id, status):
    """Compatibility wrapper accepting old 'published' status."""
    normalized = "enabled" if status == "published" else status
    update_questionnaire_publication(pub_id, normalized)
    row = query_one("SELECT * FROM questionnaire_publications WHERE id=?", (pub_id,))
    return _legacy_publication_payload(row)


def delete_questionnaire_publication(pub_id):
    """Delete a publication only if no submissions exist. Otherwise close it."""
    pub = query_one("SELECT * FROM questionnaire_publications WHERE id=?", (pub_id,))
    if not pub:
        raise ValueError("Publication not found")
    sub_count = query_one(
        "SELECT COUNT(*) AS c FROM questionnaire_submissions WHERE questionnaire_id=? AND session_id=? AND response_stage=?",
        (pub['questionnaire_id'], pub['session_id'], pub['response_stage']),
    )
    if sub_count and sub_count['c'] > 0:
        execute("UPDATE questionnaire_publications SET status='closed' WHERE id=?", (pub_id,))
        raise ValueError("Cannot delete publication with existing submissions. Publication has been closed instead.")
    execute("DELETE FROM questionnaire_publications WHERE id=?", (pub_id,))
    return True


def list_questionnaire_completion(session_id=None, questionnaire_id=None):
    """Return completion statistics based on questionnaire_submissions.status='submitted'."""
    where_clauses = ["qs.status='submitted'"]
    params = []
    if session_id:
        where_clauses.append("qs.session_id=?")
        params.append(session_id)
    if questionnaire_id:
        where_clauses.append("qs.questionnaire_id=?")
        params.append(questionnaire_id)
    where = " AND ".join(where_clauses)
    rows = query_all("""
        SELECT qs.session_id, es.session_no, es.title AS session_title,
               qs.questionnaire_id, q.title AS questionnaire_title, q.code AS questionnaire_code,
               qs.response_stage,
               qs.group_id, g.name AS group_name, g.group_code,
               COUNT(DISTINCT qs.user_id) AS completed_count,
               COUNT(DISTINCT gm.user_id) AS roster_count
        FROM questionnaire_submissions qs
        JOIN questionnaires q ON qs.questionnaire_id = q.id
        LEFT JOIN experiment_sessions es ON qs.session_id = es.id
        LEFT JOIN groups g ON qs.group_id = g.id
        LEFT JOIN group_members gm ON qs.group_id = gm.group_id
        WHERE """ + where + """
        GROUP BY qs.session_id, qs.questionnaire_id, qs.response_stage, qs.group_id
        ORDER BY es.session_no ASC, q.title ASC, qs.response_stage ASC
    """, params)
    result = [dict(r) for r in rows]
    for r in result:
        r['uncompleted_count'] = max(0, r['roster_count'] - r['completed_count'])
    return result


def create_questionnaire_submission(questionnaire_id, user_id, group_id, session_id, session_no, response_stage, responses):
    """Create a transactional questionnaire submission (all-or-nothing)."""
    stage = response_stage.strip().lower() if response_stage else 'pre'
    if stage not in QUESTIONNAIRE_STAGE_VALUES:
        raise ValueError("Invalid response_stage")
    conn = db()
    try:
        now = now_str()
        existing = conn.execute(
            "SELECT id FROM questionnaire_submissions WHERE questionnaire_id=? AND user_id=? AND session_id=? AND response_stage=? AND status='submitted'",
            (questionnaire_id, user_id, session_id, stage),
        ).fetchone()
        if existing:
            raise ValueError("Already submitted")
        cur = conn.execute("""
            INSERT INTO questionnaire_submissions(questionnaire_id, user_id, group_id, session_id, session_no, response_stage, status, submitted_at, created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (questionnaire_id, user_id, group_id, session_id, session_no, stage, 'submitted', now, now))
        submission_id = cur.lastrowid
        for item_id_str, value in (responses or {}).items():
            response_value = None
            response_text = None
            response_option_key = None
            try:
                item_id = int(item_id_str)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                if value.get("option_key") not in (None, ""):
                    response_option_key = str(value.get("option_key"))
                if value.get("value") not in (None, ""):
                    try:
                        response_value = int(value.get("value"))
                    except (ValueError, TypeError):
                        response_text = str(value.get("value"))
                elif value.get("text") not in (None, ""):
                    response_text = str(value.get("text"))
                elif value.get("option_key") not in (None, ""):
                    response_value = 0
                    response_text = str(value.get("option_key"))
                else:
                    response_text = json.dumps(value, ensure_ascii=False)
            else:
                try:
                    response_value = int(value)
                except (ValueError, TypeError):
                    response_text = None if value is None else str(value)
            if _conn_has_column(conn, "questionnaire_responses", "response_option_key"):
                conn.execute("""
                    INSERT INTO questionnaire_responses(questionnaire_id, user_id, item_id, response_value, response_text, response_option_key, response_stage, group_id, session_id, session_no, submission_id, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (questionnaire_id, user_id, item_id, response_value, response_text, response_option_key, stage, group_id, session_id, session_no, submission_id, now))
            else:
                conn.execute("""
                    INSERT INTO questionnaire_responses(questionnaire_id, user_id, item_id, response_value, response_text, response_stage, group_id, session_id, session_no, submission_id, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, (questionnaire_id, user_id, item_id, response_value, response_text, stage, group_id, session_id, session_no, submission_id, now))
        conn.commit()
        return {'submission_id': submission_id, 'submitted': True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_published_questionnaires_for_student(user_id, session_id, response_stage, group_id=None):
    """List questionnaires published and enabled for a student in a given session and stage."""
    stage = response_stage.strip().lower() if response_stage else 'pre'
    if stage not in QUESTIONNAIRE_STAGE_VALUES:
        stage = 'pre'
    if group_id is None:
        membership = query_one(
            "SELECT group_id FROM group_members WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        group_id = membership["group_id"] if membership else None
    rows = query_all("""
        SELECT q.*, qp.id AS publication_id,
               qp.group_id AS publication_group_id,
               qp.user_id AS publication_user_id,
               CASE
                   WHEN qp.user_id = ? THEN 0
                   WHEN qp.group_id = ? THEN 1
                   ELSE 2
               END AS publication_scope_rank
        FROM questionnaires q
        JOIN questionnaire_publications qp ON q.id = qp.questionnaire_id
        WHERE qp.session_id = ? AND qp.response_stage = ? AND qp.status = 'enabled'
          AND q.active = 1
          AND (qp.group_id IS NULL OR qp.group_id = ?)
          AND (qp.user_id IS NULL OR qp.user_id = ?)
        ORDER BY publication_scope_rank ASC, q.sort_order ASC, q.id ASC
    """, (user_id, group_id, session_id, stage, group_id, user_id))
    result = []
    seen = set()
    for r in rows:
        d = dict(r)
        qid = d["id"]
        if qid in seen:
            continue
        seen.add(qid)
        result.append(d)
    return result


# ============================================================
# End Batch 6 additions
# ============================================================
