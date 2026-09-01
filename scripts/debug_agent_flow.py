# -*- coding: utf-8 -*-
"""Debug agent flow for a given group.

Usage:
    python scripts/debug_agent_flow.py --group-id 16
    python scripts/debug_agent_flow.py --group-id 16 --limit 30
"""
import argparse
import sqlite3
import os
import sys

DB_PATH = os.environ.get(
    "SSRL_ESP_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "ssrl_esp.db"),
)


def connect():
    if not os.path.exists(DB_PATH):
        print("ERROR: Database not found: " + DB_PATH)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()


def has_table(cursor, table_name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def has_column(cursor, table_name, column_name):
    if not has_table(cursor, table_name):
        return False
    cursor.execute("PRAGMA table_info(" + table_name + ")")
    return any(col[1] == column_name for col in cursor.fetchall())


def safe_get(row, key, default="N/A"):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def print_messages(cursor, group_id, limit):
    print("=" * 60)
    print("  RECENT MESSAGES (last " + str(limit) + ") for group_id=" + str(group_id))
    print("=" * 60)
    has_visible = has_column(cursor, "messages", "is_visible")
    has_type = has_column(cursor, "messages", "type")
    has_sender_type = has_column(cursor, "messages", "sender_type")
    try:
        rows = cursor.execute(
            "SELECT m.*, u.participant_code, u.real_name FROM messages m "
            "LEFT JOIN users u ON m.user_id = u.id "
            "WHERE m.group_id=? ORDER BY m.id DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
        if not rows:
            print("  (no messages found)")
            return
        for row in reversed(rows):
            content = (safe_get(row, "content") or "")[:80].replace("\n", "\\n")
            row_type = safe_get(row, "type", "") if has_type else ""
            sender_type = safe_get(row, "sender_type", "") if has_sender_type else ""
            is_vis = " visible=" + str(safe_get(row, "is_visible")) if has_visible else ""
            print(
                "  id=" + str(safe_get(row, "id"))
                + " role=" + str(safe_get(row, "role"))
                + " user=" + str(safe_get(row, "user_id"))
                + " type=" + str(row_type)
                + " sender_type=" + str(sender_type)
                + is_vis
                + " session=" + str(safe_get(row, "session_no", "?"))
                + " task=" + str(safe_get(row, "task_id", "?"))
                + " created=" + str(safe_get(row, "created_at", ""))[:19]
            )
            print("    content: " + content)
    except Exception as e:
        print("  ERROR querying messages: " + str(e))


def print_events(cursor, group_id, limit):
    print("")
    print("=" * 60)
    print("  STATE / MONITOR / INTERVENTION EVENTS (last " + str(limit) + ")")
    print("=" * 60)
    tables = [
        "state_assessments",
        "monitor_runs",
        "intervention_runs",
        "intervention_logs",
        "autonomous_regulation_events",
        "group_states",
    ]
    found_any = False
    for table in tables:
        if not has_table(cursor, table):
            continue
        try:
            rows = cursor.execute(
                "SELECT * FROM " + table + " WHERE group_id=? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
            if not rows:
                continue
            found_any = True
            print("")
            print("  --- " + table + " ---")
            for row in rows[:5]:
                rd = dict(row)
                keys = [
                    "id", "state_code", "fused_state_code", "final_state",
                    "confidence", "should_intervene", "status",
                    "trigger_reason", "error_type", "failure_reason",
                    "assessment_status", "self_regulation_detected",
                    "detected_state", "created_at",
                ]
                parts = []
                for k in keys:
                    if k in rd and rd[k] is not None:
                        parts.append(str(k) + "=" + str(rd[k]))
                if parts:
                    print("    " + "; ".join(parts))
        except Exception as e:
            print("  (error reading " + table + ": " + str(e) + ")")
    if not found_any:
        print("  (no event tables with data for this group)")


def print_agent_config(cursor, group_id):
    print("")
    print("=" * 60)
    print("  GROUP / TASK / SESSION AGENT CONFIGURATION")
    print("=" * 60)
    try:
        group = cursor.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if group:
            gd = dict(group)
            print(
                "  Group: id=" + str(gd.get("id"))
                + " state=" + str(gd.get("state"))
                + " condition=" + str(gd.get("condition"))
                + " auto_intervention_enabled=" + str(gd.get("auto_intervention_enabled", "?"))
                + " version=" + str(gd.get("version", "?"))
                + " last_msg_seq=" + str(gd.get("last_message_sequence", "?"))
            )
        else:
            print("  Group id=" + str(group_id) + ": NOT FOUND")
            return

        session = cursor.execute(
            "SELECT * FROM experiment_sessions WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if session:
            sd = dict(session)
            print(
                "  Session: id=" + str(sd.get("id"))
                + " role=" + str(sd.get("session_role"))
                + " session_no=" + str(sd.get("session_no"))
                + " detection_enabled=" + str(sd.get("agent_detection_enabled", "?"))
                + " intervention_enabled=" + str(sd.get("agent_intervention_enabled", "?"))
            )
            role = sd.get("session_role") or ""
            print("  Derived agent flags:")
            if role == "S2_intervention":
                print("    -> agent_intervention_enabled=True, strategy_agent=True, emotion_agent=True")
            else:
                print("    -> agent_intervention_enabled=False (role=" + role + ")")
        else:
            print("  Session: (none running)")

        task = cursor.execute(
            "SELECT * FROM learning_tasks WHERE is_current=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if task:
            td = dict(task)
            print(
                "  Current Task: id=" + str(td.get("id"))
                + " title=" + str(td.get("title", ""))[:40]
                + " agent_intervention_enabled=" + str(td.get("agent_intervention_enabled", "?"))
            )
        else:
            print("  Task: (none current)")

    except Exception as e:
        print("  ERROR: " + str(e))


def main():
    parser = argparse.ArgumentParser(description="Debug agent flow for a group")
    parser.add_argument("--group-id", type=int, required=True, help="Group ID to inspect")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent records")
    args = parser.parse_args()

    conn, cursor = connect()
    gid = args.group_id
    limit = args.limit

    print("Using database: " + str(DB_PATH))
    print("Group ID: " + str(gid) + ", Limit: " + str(limit))

    print_messages(cursor, gid, limit)
    print_events(cursor, gid, limit)
    print_agent_config(cursor, gid)

    conn.close()
    print("")
    print("=" * 60)
    print("  Done.")


if __name__ == "__main__":
    main()
