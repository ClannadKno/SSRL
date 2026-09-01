#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Initialize default experiment session and learning task.

Run this after reset_database.py to set up the collaborative editor.

Usage:
    python scripts/init_default_session.py
"""
import sys
PROJECT_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from db import now_str, execute, set_setting, db

# Create default learning task (id=1)
has_task = execute("SELECT id FROM learning_tasks WHERE id=1")
if not has_task:
    tid = execute(
        "INSERT INTO learning_tasks(title,question,task_goal,output_requirement,time_limit_minutes,expected_dimensions_json,key_concepts_json,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("默认协作任务", "请与组内同学协作完成", "协作完成学习目标", "提交协作成果", 30, "[]", "[]", 1, now_str())
    )
    print("[TASK] Created default task (id=%d)" % tid)
else:
    tid = 1
    print("[TASK] Task already exists (id=1)")

# Create running experiment session
conn = db()
session = conn.execute("SELECT id FROM experiment_sessions WHERE status='running'").fetchone()
if not session:
    sid = execute(
        "INSERT INTO experiment_sessions(session_no,session_role,task_id,status,start_time,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (1, "S1_baseline", tid, "running", now_str(), now_str(), now_str())
    )
    print("[SESSION] Created running session (id=%d)" % sid)
else:
    sid = session["id"]
    print("[SESSION] Running session already exists (id=%d)" % sid)
conn.close()

# Set settings
set_setting("current_session_no", "1")
print("[SETTING] current_session_no=1")

print("\n[DONE] Collaborative editor should now work. Login with a student key to test.")
