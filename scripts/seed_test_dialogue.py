#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Seed a simple two-person test dialogue for manual agent testing.

Creates:
  - A group with 2 student participants
  - A running experiment session (S2, both agents enabled)
  - A learning task
  - ~16 back-and-forth messages (simulating a discussion hitting frustration)

Run:  uv run python scripts/seed_test_dialogue.py

The script prints summary at the end so you can use the IDs
for manual testing (checklist items 2–10).
"""

import os, sys, pathlib, json, shutil
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = os.environ.get("SSRL_ESP_DB_PATH", str(ROOT / "ssrl_esp.db"))


def main():
    print("=" * 56)
    print("  Dual-Agent Test Dialogue Seeder")
    print("=" * 56)
    print()

    # Backup existing DB
    if os.path.exists(DB_PATH):
        bak = DB_PATH + ".seed_backup"
        shutil.copy2(DB_PATH, bak)
        print("[BACKUP] saved to %s" % bak)
    else:
        print("[INFO] no existing DB, will create fresh")

    # Ensure environment is clean
    os.environ["SSRL_ESP_DB_PATH"] = DB_PATH
    os.environ.setdefault("HUEY_ENABLED", "0")
    os.environ.setdefault("HUEY_IMMEDIATE", "1")
    os.environ.setdefault("DISCUSSION_PIPELINE_V2_ENABLED", "0")

    import importlib
    for name in list(sys.modules.keys()):
        if name in ("db", "app", "config", "core", "huey_instance"):
            del sys.modules[name]
    db = importlib.import_module("db")
    db.ensure_database_ready()
    now = db.now_str()

    # 1. Create teacher user
    teacher_id = db.execute(
        "INSERT INTO users(username, password_hash, real_name, role, created_at) VALUES(?,?,?,?,?)",
        ("seed_teacher", "*", "测试教师", "teacher", now),
    )
    print("[TEACHER] id=%d" % teacher_id)

    # 2. Create two student users
    s1_id = db.execute(
        "INSERT INTO users(username, password_hash, real_name, role, participant_code, created_at) VALUES(?,?,?,?,?,?)",
        ("seed_student_a", "*", "学生A", "student", "SA001", now),
    )
    s2_id = db.execute(
        "INSERT INTO users(username, password_hash, real_name, role, participant_code, created_at) VALUES(?,?,?,?,?,?)",
        ("seed_student_b", "*", "学生B", "student", "SB001", now),
    )
    print("[STUDENTS] A id=%d   B id=%d" % (s1_id, s2_id))

    # 3. Create group
    gid = db.execute(
        "INSERT INTO groups(name, group_code, condition, state, last_message_sequence, created_at) VALUES(?,?,?,?,?,?)",
        ("测试讨论组", "G-TEST-01", "experiment", "OPEN", 0, now),
    )
    db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (gid, s1_id))
    db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (gid, s2_id))
    print("[GROUP] id=%d  (2 members)" % gid)

    # 4. Create learning task
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, description, question, is_active, sort_order, time_limit_minutes, created_at) VALUES(?,?,?,?,?,?,?)",
        ("城市可持续发展方案设计", "请以小组形式讨论并提出你们所在城市可持续发展的具体方案", "如何平衡城市经济发展与环境保护？", 1, 1, 30, now),
    )
    print("[TASK] id=%d" % task_id)

    # 5. Create running experiment session (S2, both agents enabled)
    session_id = db.execute(
        """INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            strategy_agent_enabled, emotion_agent_enabled,
            agent_detection_enabled, agent_intervention_enabled,
            created_by, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (1, "S2_intervention", task_id, "running", now,
         1, 1, 1, 1, teacher_id, now, now),
    )
    # Mark as active via settings
    db.execute("REPLACE INTO settings(key, value) VALUES('current_session_id', ?)", (str(session_id),))
    db.execute("REPLACE INTO settings(key, value) VALUES('current_session_no', '1')")
    db.execute("REPLACE INTO settings(key, value) VALUES('current_task_id', ?)", (str(task_id),))
    print("[SESSION] id=%d  (S2, both agents enabled)" % session_id)

    # 6. Seed dialogue: ~16 messages, two-person, escalating frustration
    dialogue = [
        # --- Phase 1: Normal collaboration ---
        ("sa", "大家好啊，今天我们要讨论城市可持续发展的话题", 0),
        ("sb", "对，我觉得可以从交通方面入手，减少碳排放", 1),
        ("sa", "嗯，公共交通是一个好方向，电动公交车之类的", 2),
        ("sb", "还有垃圾分类和回收利用，这也是重要的一环", 3),
        ("sa", "是的，不过我觉得首先要有明确的目标和分工", 4),
        # --- Phase 2: Hitting a block ---
        ("sb", "但是我们的资料太少了，不知道从哪里开始查", 5),
        ("sa", "我也觉得很难，这个话题太大了，不知道怎么下手", 6),
        ("sb", "要不我们先查一下别的城市是怎么做的？", 7),
        ("sa", "查了也没用啊，每个城市的情况都不一样", 8),
        # --- Phase 3: Frustration building ---
        ("sb", "那我们现在到底要做什么？完全没思路", 9),
        ("sa", "不知道，我感觉好难，不想做了", 10),
        ("sb", "别这么说，总得完成这个任务吧", 11),
        ("sa", "但是真的想不出来有什么好方案", 12),
        ("sb", "要不再看看题目？可能是我们理解错了", 13),
        ("sa", "看了好几遍了，就是不知道怎么下手，烦死了", 14),
        ("sb", "要不我们分成两部分，你负责环境部分，我负责经济部分？", 15),
    ]

    users = {"sa": s1_id, "sb": s2_id}
    msg_ids = []
    for speaker, text, seq in dialogue:
        mid = db.execute(
            """INSERT INTO messages(
                group_id, user_id, content, role, sequence,
                session_no, task_id, session_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (gid, users[speaker], text, "student", seq + 1,
             1, task_id, session_id, now),
        )
        msg_ids.append(mid)
    # Update group's last_message_sequence
    db.execute("UPDATE groups SET last_message_sequence=? WHERE id=?", (len(dialogue), gid))

    print("[MESSAGES] created %d messages (seq 1..%d)" % (len(dialogue), len(dialogue)))

    # 7. Print summary
    print()
    print("-" * 56)
    print("  SUMMARY")
    print("-" * 56)
    print("  group_id (for API filters) ...... %d" % gid)
    print("  session_id ...................... %d" % session_id)
    print("  task_id ......................... %d" % task_id)
    print("  student A id ................... %d" % s1_id)
    print("  student B id ................... %d" % s2_id)
    print("  teacher id ..................... %d" % teacher_id)
    print()
    print("  Strategy agent ................. ENABLED")
    print("  Emotion agent .................. ENABLED")
    print("  Session status ................. running")
    print()
    print("  Messages (student A -> B):")
    for i, (speaker, text, seq) in enumerate(dialogue):
        label = "A" if speaker == "sa" else "B"
        print("    %2d. [%s] %s" % (seq + 1, label, text))
    print()
    print("  The dialogue escalates from normal collaboration")
    print("  into frustration (seq 9+), which should trigger")
    print("  the strategy agent on next detection cycle.")
    print()
    print("  Emotion agent will send a mood message after")
    print("  ~10 minutes of inactivity (or on next tick).")
    print()
    print("  Export: GET /export/agent_research_events.csv")
    print("         ?group_id=%d&session_id=%d" % (gid, session_id))
    print()
    print("  UI: open teacher console -> History tab")
    print("      -> filter by group_id=%d" % gid)
    print("=" * 56)


if __name__ == "__main__":
    main()
