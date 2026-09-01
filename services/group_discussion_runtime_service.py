# -*- coding: utf-8 -*-
"""Per-group discussion runtime and student waiting gate."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Iterable, Optional

from db import db, now_str, parse_dt, query_one


DEFAULT_DISCUSSION_MINUTES = 30
WRITE_CLOSED_STATUSES = {"timed_out", "submitted", "closed"}


def _json_int_list(value) -> list[int]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    result = []
    for item in parsed or []:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


def _dump_ids(ids: Iterable[int]) -> str:
    return json.dumps(sorted({int(i) for i in ids}), ensure_ascii=False)


def _discussion_limit_minutes(conn, session_id: int) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(s.time_limit_minutes, t.time_limit_minutes) AS limit_minutes
        FROM experiment_sessions s
        LEFT JOIN learning_tasks t ON s.task_id = t.id
        WHERE s.id=?
        """,
        (session_id,),
    ).fetchone()
    try:
        limit = int(row["limit_minutes"] if row else 0)
    except (TypeError, ValueError):
        limit = 0
    return limit if limit > 0 else DEFAULT_DISCUSSION_MINUTES


def _snapshot_group_members(conn, group_id: int, fallback_user_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id ASC",
        (group_id,),
    ).fetchall()
    ids = [int(row["user_id"]) for row in rows]
    if fallback_user_id and fallback_user_id not in ids:
        ids.append(int(fallback_user_id))
    return sorted(set(ids))


def _row_to_runtime(row) -> Optional[dict]:
    if not row:
        return None
    data = dict(row)
    data["expected_student_ids"] = _json_int_list(data.get("expected_student_ids_json"))
    data["ready_student_ids"] = _json_int_list(data.get("ready_student_ids_json"))
    data["is_waiting"] = data.get("status") == "waiting"
    data["is_running"] = data.get("status") == "running"
    return data


def _deadline_remaining_seconds(deadline, now_dt: Optional[datetime] = None) -> Optional[int]:
    deadline_dt = parse_dt(deadline) if isinstance(deadline, str) else deadline
    if not deadline_dt:
        return None
    delta = int((deadline_dt - (now_dt or datetime.now())).total_seconds())
    return max(0, delta)


def _runtime_deadline_passed(runtime: Optional[dict], now_dt: Optional[datetime] = None) -> bool:
    if not runtime or runtime.get("status") != "running":
        return False
    deadline_dt = parse_dt(runtime.get("deadline"))
    if not deadline_dt:
        return False
    return (now_dt or datetime.now()) >= deadline_dt


def is_group_discussion_write_closed(
    session_id: int,
    group_id: int,
    now_dt: Optional[datetime] = None,
) -> bool:
    """Return True when this group's discussion should reject new writes."""
    runtime = get_group_discussion_runtime(session_id, group_id)
    if not runtime:
        return False
    if runtime.get("status") in WRITE_CLOSED_STATUSES:
        return True
    return _runtime_deadline_passed(runtime, now_dt=now_dt)


def group_discussion_timer_payload(
    runtime: Optional[dict],
    now_dt: Optional[datetime] = None,
) -> dict:
    """Build the API-facing timer fields for one group runtime."""
    if not runtime:
        return {
            "group_discussion_status": None,
            "group_discussion_started_at": None,
            "group_discussion_deadline": None,
            "group_remaining_seconds": None,
            "group_timed_out": False,
        }
    inferred_timed_out = runtime.get("status") == "timed_out" or _runtime_deadline_passed(runtime, now_dt=now_dt)
    status = "timed_out" if inferred_timed_out and runtime.get("status") == "running" else runtime.get("status")
    return {
        "group_discussion_status": status,
        "group_discussion_started_at": runtime.get("started_at"),
        "group_discussion_deadline": runtime.get("deadline"),
        "group_remaining_seconds": _deadline_remaining_seconds(runtime.get("deadline"), now_dt=now_dt),
        "group_timed_out": bool(inferred_timed_out),
    }


def get_group_discussion_runtime(session_id: int, group_id: int) -> Optional[dict]:
    row = query_one(
        "SELECT * FROM group_session_discussions WHERE session_id=? AND group_id=?",
        (session_id, group_id),
    )
    return _row_to_runtime(row)


def enter_group_discussion_stage(session_id: int, group_id: int, student_id: int) -> dict:
    """Mark a student ready for discussion and start the group timer if complete.

    This is idempotent for refresh/re-entry. The expected group roster is
    snapshotted when the first group member reaches the discussion stage.
    """
    if not session_id or not group_id or not student_id:
        raise ValueError("session_id, group_id and student_id are required")

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = now_str()
        runtime = conn.execute(
            "SELECT * FROM group_session_discussions WHERE session_id=? AND group_id=?",
            (session_id, group_id),
        ).fetchone()

        if not runtime:
            expected_ids = _snapshot_group_members(conn, group_id, student_id)
            conn.execute(
                """
                INSERT INTO group_session_discussions(
                    session_id, group_id, status, expected_student_ids_json,
                    ready_student_ids_json, expected_student_count,
                    ready_student_count, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    group_id,
                    "waiting",
                    _dump_ids(expected_ids),
                    "[]",
                    len(expected_ids),
                    0,
                    now,
                    now,
                ),
            )
            runtime = conn.execute(
                "SELECT * FROM group_session_discussions WHERE session_id=? AND group_id=?",
                (session_id, group_id),
            ).fetchone()

        runtime_id = int(runtime["id"])
        conn.execute(
            """
            INSERT OR IGNORE INTO group_discussion_entries(
                group_discussion_id, student_id, entered_at, ready_at, last_seen_at,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (runtime_id, student_id, now, now, now, now, now),
        )
        conn.execute(
            """
            UPDATE group_discussion_entries
            SET ready_at=COALESCE(ready_at, ?), last_seen_at=?, updated_at=?
            WHERE group_discussion_id=? AND student_id=?
            """,
            (now, now, now, runtime_id, student_id),
        )

        expected_ids = _json_int_list(runtime["expected_student_ids_json"])
        ready_rows = conn.execute(
            """
            SELECT student_id FROM group_discussion_entries
            WHERE group_discussion_id=? AND ready_at IS NOT NULL
            ORDER BY student_id ASC
            """,
            (runtime_id,),
        ).fetchall()
        ready_ids = sorted({int(row["student_id"]) for row in ready_rows})
        ready_expected_ids = sorted(set(ready_ids).intersection(expected_ids))
        status = runtime["status"]
        started_at = runtime["started_at"]
        deadline = runtime["deadline"]

        if status == "waiting" and expected_ids and set(expected_ids).issubset(set(ready_ids)):
            started_at = now
            started_dt = parse_dt(started_at) or datetime.now()
            deadline = (started_dt + timedelta(minutes=_discussion_limit_minutes(conn, session_id))).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            status = "running"

        conn.execute(
            """
            UPDATE group_session_discussions
            SET status=?, ready_student_ids_json=?, ready_student_count=?,
                started_at=COALESCE(started_at, ?),
                deadline=COALESCE(deadline, ?),
                updated_at=?
            WHERE id=?
            """,
            (
                status,
                _dump_ids(ready_expected_ids),
                len(ready_expected_ids),
                started_at,
                deadline,
                now,
                runtime_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM group_session_discussions WHERE id=?",
            (runtime_id,),
        ).fetchone()
        conn.commit()
        return _row_to_runtime(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
