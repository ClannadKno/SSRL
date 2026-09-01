# -*- coding: utf-8 -*-
"""Resolve the teacher-reporting time window for one group discussion."""
from __future__ import annotations

from datetime import datetime, timedelta

from db import query_one


def parse_dt(value):
    if not value:
        return None
    text = str(value).replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def fmt_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _session_row(session_id):
    if not session_id:
        return None
    row = query_one(
        """
        SELECT id, start_time, end_time, task_id, session_no, time_limit_minutes
        FROM experiment_sessions
        WHERE id=?
        """,
        (session_id,),
    )
    return dict(row) if row else None


def _runtime_row(group_id, session_id):
    if not group_id or not session_id:
        return None
    row = query_one(
        """
        SELECT *
        FROM group_session_discussions
        WHERE session_id=? AND group_id=?
        """,
        (session_id, group_id),
    )
    return dict(row) if row else None


def _first_entry_time(runtime_id):
    if not runtime_id:
        return None
    row = query_one(
        """
        SELECT MIN(COALESCE(ready_at, entered_at, created_at)) AS first_at
        FROM group_discussion_entries
        WHERE group_discussion_id=?
        """,
        (runtime_id,),
    )
    return row["first_at"] if row and row["first_at"] else None


def _document_submitted_at(group_id, session):
    if not group_id or not session:
        return None
    row = query_one(
        """
        SELECT submitted_at
        FROM collaborative_documents
        WHERE group_id=?
          AND submitted_at IS NOT NULL
          AND (
            session_id=?
            OR (session_id IS NULL AND task_id=? AND session_no=?)
          )
        ORDER BY submitted_at ASC
        LIMIT 1
        """,
        (
            group_id,
            session["id"],
            session.get("task_id"),
            session.get("session_no"),
        ),
    )
    return row["submitted_at"] if row and row["submitted_at"] else None


def _first_valid_after_start(candidates, start_dt):
    parsed = []
    for value in candidates:
        dt = parse_dt(value)
        if not dt:
            continue
        if start_dt and dt < start_dt:
            continue
        parsed.append(dt)
    return min(parsed) if parsed else None


def resolve_discussion_window(
    group_id,
    session_id=None,
    start_time=None,
    end_time=None,
    fallback_minutes=60,
):
    """Return ``(window_start, window_end, legacy_warning)``.

    The preferred reporting interval is group discussion start through group
    deliverable submission.  Session end is only used as a legacy fallback.
    """
    legacy_warning = False
    now = datetime.now()

    if start_time and end_time:
        ws = parse_dt(start_time)
        we = parse_dt(end_time)
        return (
            fmt_dt(ws) if ws else start_time,
            fmt_dt(we) if we else end_time,
            legacy_warning,
        )

    session = _session_row(session_id)
    runtime = _runtime_row(group_id, session_id)

    start_dt = None
    if runtime:
        start_dt = parse_dt(runtime.get("started_at")) or parse_dt(_first_entry_time(runtime.get("id")))
    if not start_dt and session:
        start_dt = parse_dt(session.get("start_time"))
        legacy_warning = True
    if not start_dt:
        start_dt = now - timedelta(minutes=fallback_minutes)
        legacy_warning = True

    submitted_dt = _first_valid_after_start(
        [
            runtime.get("submitted_at") if runtime else None,
            runtime.get("auto_submitted_at") if runtime else None,
            _document_submitted_at(group_id, session),
        ],
        start_dt,
    )

    end_dt = submitted_dt
    if not end_dt and runtime:
        status = runtime.get("status")
        if status == "timed_out":
            end_dt = parse_dt(runtime.get("deadline")) or parse_dt(runtime.get("updated_at"))
        elif status in {"submitted", "closed"}:
            end_dt = parse_dt(runtime.get("updated_at")) or parse_dt(runtime.get("deadline"))
        elif parse_dt(runtime.get("deadline")) and now >= parse_dt(runtime.get("deadline")):
            end_dt = parse_dt(runtime.get("deadline"))

    if not end_dt:
        end_dt = now

    if end_dt < start_dt:
        end_dt = start_dt

    return fmt_dt(start_dt), fmt_dt(end_dt), legacy_warning
