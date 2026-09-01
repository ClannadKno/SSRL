# -*- coding: utf-8 -*-
"""
Teacher participation service - T3 member text participation statistics.

Computes per-member and group-level text participation metrics
based on platform discussion data only.
"""
import math
from datetime import datetime, timedelta

from db import db, now_str, query_all, query_one
from services.discussion_window_service import resolve_discussion_window


def get_participation_summary(
    group_id,
    session_id=None,
    window="session",
    start_time=None,
    end_time=None,
):
    """Return participation-summary dict for the given group and window."""
    group = query_one("SELECT * FROM groups WHERE id=?", (group_id,))
    if not group:
        return {"error": f"Group {group_id} not found"}
    ws, we, lw = _resolve_window(window, start_time, end_time, session_id, group_id)
    msgs = _fetch_student_messages(group_id, session_id, ws, we)
    members = _aggregate_members(msgs)
    gm = _compute_group_metrics(members)
    return {
        "group_id": group_id,
        "session_id": session_id,
        "window_start": ws,
        "window_end": we,
        "generated_at": now_str(),
        "members": members,
        "group_metrics": gm,
        "legacy_data_warning": lw,
    }


def get_participation_timeline(
    group_id,
    session_id=None,
    start_time=None,
    end_time=None,
    window_minutes=3,
):
    """Return time-bucketed participation timeline for a group.

    Groups student messages into time windows (default 3 min) and
    computes per-member participation metrics per window, so the
    frontend can render a multi-line chart over time.
    """
    group = query_one("SELECT id, name FROM groups WHERE id=?", (group_id,))
    if not group:
        return {"error": "Group %d not found" % group_id}

    if start_time and end_time:
        ws, we, lw = _resolve_window("custom", start_time, end_time, session_id, group_id)
    else:
        ws, we, lw = _resolve_window("session", None, None, session_id, group_id)
    msgs = _fetch_student_messages(group_id, session_id, ws, we)

    if not msgs:
        return {
            "group_id": group_id,
            "session_id": session_id,
            "window_minutes": window_minutes,
            "timeline": [],
            "members": [],
            "generated_at": now_str(),
        }

    # Determine overall time range
    first_dt = _parse_dt(msgs[0]["created_at"])
    last_dt = _parse_dt(msgs[-1]["created_at"])
    if not first_dt or not last_dt:
        first_dt = datetime.now() - timedelta(hours=1)
        last_dt = datetime.now()
    # Extend last window slightly so the last bucket is inclusive
    last_dt = last_dt + timedelta(minutes=window_minutes)

    # Collect all member codes
    member_codes = {}
    for msg in msgs:
        uid = msg["user_id"]
        code = msg.get("participant_code") or "UID%d" % uid
        if uid not in member_codes:
            member_codes[uid] = code

    # Build time buckets
    buckets = []
    cursor = first_dt
    while cursor < last_dt:
        bucket_end = cursor + timedelta(minutes=window_minutes)
        buckets.append({
            "window_start": cursor,
            "window_end": bucket_end,
            "members": {},
        })
        cursor = bucket_end

    # Assign messages to buckets
    for msg in msgs:
        msg_dt = _parse_dt(msg["created_at"])
        if not msg_dt:
            continue
        uid = msg["user_id"]
        for bucket in buckets:
            if bucket["window_start"] <= msg_dt < bucket["window_end"]:
                if uid not in bucket["members"]:
                    bucket["members"][uid] = {
                        "user_id": uid,
                        "participant_code": member_codes[uid],
                        "message_count": 0,
                        "char_count": 0,
                        "active_minutes_set": set(),
                    }
                acc = bucket["members"][uid]
                acc["message_count"] += 1
                cl = msg.get("char_len")
                if cl is not None:
                    acc["char_count"] += int(cl)
                else:
                    acc["char_count"] += len(msg.get("content") or "")
                created_at = msg.get("created_at")
                if created_at:
                    acc["active_minutes_set"].add(str(created_at)[:16])
                break

    def _fmt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # Build timeline output
    timeline = []
    all_member_ids = sorted(member_codes.keys())
    for bucket in buckets:
        members_list = []
        for uid in all_member_ids:
            m = bucket["members"].get(uid, {
                "user_id": uid,
                "participant_code": member_codes[uid],
                "message_count": 0,
                "char_count": 0,
                "active_minutes": 0,
            })
            if "active_minutes_set" in m:
                m = dict(m)
                m["active_minutes"] = len(m.pop("active_minutes_set") or set())
            members_list.append(m)
        timeline.append({
            "window_start": _fmt(bucket["window_start"]),
            "window_end": _fmt(bucket["window_end"]),
            "members": members_list,
        })

    # Also return the member list for line chart legend
    member_list = [
        {"user_id": uid, "participant_code": code}
        for uid, code in sorted(member_codes.items())
    ]

    return {
        "group_id": group_id,
        "session_id": session_id,
        "window_minutes": window_minutes,
        "window_start": ws,
        "window_end": we,
        "generated_at": now_str(),
        "timeline": timeline,
        "members": member_list,
    }


def compute_gini(values):
    """Compute Gini coefficient."""
    if not values or len(values) < 2:
        return 0.0
    sv = sorted(values)
    n = len(sv)
    s = sum(sv)
    if s == 0:
        return 0.0
    ws = sum((i + 1) * x for i, x in enumerate(sv))
    return round((2 * ws) / (n * s) - (n + 1) / n, 6)


def _resolve_window(window, start_time, end_time, session_id, group_id=None):
    legacy_warning = False
    now = datetime.now()
    def fmt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if window == "custom":
        if not start_time or not end_time:
            raise ValueError("start_time and end_time required")
        ws = _parse_dt(start_time)
        we = _parse_dt(end_time)
        return (fmt(ws) if ws else start_time,
                fmt(we) if we else end_time,
                legacy_warning)
    if window == "session":
        if session_id and group_id:
            return resolve_discussion_window(
                group_id=group_id,
                session_id=session_id,
                start_time=start_time,
                end_time=end_time,
            )
        if session_id:
            sess = query_one(
                "SELECT start_time FROM experiment_sessions WHERE id=?",
                (session_id,),
            )
            if sess and sess["start_time"]:
                ws = _parse_dt(sess["start_time"])
                if ws:
                    return (fmt(ws), fmt(now), True)
        return (fmt(now - timedelta(minutes=60)),
                fmt(now), True)
    mins = {"5m": 5, "10m": 10, "30m": 30}.get(window)
    if mins is None:
        raise ValueError(
            "Invalid window '%s'. Use 5m/10m/30m/session/custom." % window)
    return (fmt(now - timedelta(minutes=mins)), fmt(now), legacy_warning)


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _fetch_student_messages(group_id, session_id, window_start, window_end):
    conn = db()
    try:
        if session_id:
            rows = conn.execute("""
                SELECT m.*, u.participant_code
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.group_id = ?
                  AND m.session_id = ?
                  AND m.created_at >= ?
                  AND m.created_at <= ?
                  AND (m.sender_type = 'student' OR m.role = 'student')
                ORDER BY m.created_at ASC
            """, (group_id, session_id, window_start, window_end)).fetchall()
            if rows:
                return [dict(r) for r in rows]
        else:
            rows = conn.execute("""
                SELECT m.*, u.participant_code
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.group_id = ?
                  AND m.created_at >= ?
                  AND m.created_at <= ?
                  AND (m.sender_type = 'student' OR m.role = 'student')
                ORDER BY m.created_at ASC
            """, (group_id, window_start, window_end)).fetchall()
        if session_id:
            rows = conn.execute("""
                SELECT m.*, u.participant_code
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.group_id = ?
                  AND m.created_at >= ?
                  AND m.created_at <= ?
                  AND (m.sender_type = 'student' OR m.role = 'student')
                ORDER BY m.created_at ASC
            """, (group_id, window_start, window_end)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _aggregate_members(messages):
    by_user = {}
    for msg in messages:
        uid = msg["user_id"]
        if uid not in by_user:
            by_user[uid] = {
                "user_id": uid,
                "participant_code": msg.get("participant_code") or "UID%d" % uid,
                "message_count": 0,
                "char_count": 0,
                "active_minutes_set": set(),
            }
        acc = by_user[uid]
        acc["message_count"] += 1
        cl = msg.get("char_len")
        if cl is not None:
            acc["char_count"] += int(cl)
        else:
            acc["char_count"] += len(msg.get("content") or "")
        c = msg.get("created_at")
        if c:
            try:
                acc["active_minutes_set"].add(c[:16])
            except Exception:
                pass
    total = sum(v["message_count"] for v in by_user.values())
    members = []
    for uid in sorted(by_user):
        acc = by_user[uid]
        members.append({
            "participant_code": acc["participant_code"],
            "display_name_blinded": acc["participant_code"],
            "message_count": acc["message_count"],
            "char_count": acc["char_count"],
            "message_share": round(acc["message_count"] / total, 6) if total > 0 else 0.0,
            "active_minutes": len(acc["active_minutes_set"]),
        })
    return members


def _compute_group_metrics(members):
    if not members:
        return {
            "active_member_count": 0,
            "max_message_share": 0.0,
            "min_message_share": 0.0,
            "gini_coefficient": 0.0,
            "imbalance_level": "low",
        }
    shares = [m["message_share"] for m in members]
    gini = compute_gini(shares)
    if gini < 0.25:
        level = "low"
    elif gini < 0.50:
        level = "medium"
    else:
        level = "high"
    return {
        "active_member_count": len(members),
        "max_message_share": round(max(shares), 6) if shares else 0.0,
        "min_message_share": round(min(shares), 6) if shares else 0.0,
        "gini_coefficient": gini,
        "imbalance_level": level,
    }
