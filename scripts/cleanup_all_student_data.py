import os
import sqlite3
import shutil
import sys
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "ssrl_esp.db")
HUEY_DB_PATH = os.path.join(PROJECT_ROOT, "data", "tasks.db")
SUBMISSIONS_DIR = os.path.join(PROJECT_ROOT, "uploads", "submissions")

DATA_TABLES = [
    "data_quality_items",
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
    # 任务（由 seed_tasks.py 重新布置）
    "learning_tasks",
    "tasks",
]

def clean_data_tables(cursor, conn):
    existing_tables = {
        row[0]
        for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table in DATA_TABLES:
        if table not in existing_tables:
            print(f"  SKIP {table} (missing)")
            continue
        cursor.execute(f'DELETE FROM "{table}"')
        print(f"  DELETE {table}")
    conn.commit()

def delete_student_users(cursor, conn):
    rows = cursor.execute("SELECT id FROM users WHERE role='student'").fetchall()
    student_ids = [r[0] for r in rows]
    if student_ids:
        ph = ",".join("?" for _ in student_ids)
        cursor.execute(f"DELETE FROM client_sessions WHERE user_id IN ({ph})", student_ids)
        print(f"  DELETE client_sessions (student): {cursor.rowcount}")
        cursor.execute(f"DELETE FROM group_members WHERE user_id IN ({ph})", student_ids)
        print(f"  DELETE group_members (student): {cursor.rowcount}")
        cursor.execute(f"DELETE FROM users WHERE id IN ({ph})", student_ids)
        print(f"  DELETE users (student): {cursor.rowcount}")
    conn.commit()
    cursor.execute("DELETE FROM groups WHERE id NOT IN (SELECT DISTINCT group_id FROM group_members)")
    print(f"  DELETE groups (empty): {cursor.rowcount}")
    conn.commit()

def reset_settings(cursor, conn):
    cursor.execute("UPDATE settings SET value='1' WHERE key='current_session_no'")
    cursor.execute("UPDATE settings SET value='1' WHERE key='current_task_id'")
    conn.commit()
    print("  settings: current_session_no=1, current_task_id=1")

def reset_sqlite_sequence(cursor, conn):
    tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for (tname,) in tables:
        cursor.execute("DELETE FROM sqlite_sequence WHERE name=?", (tname,))
    conn.commit()
    print(f"  sqlite_sequence reset for {len(tables)} tables")

def clean_uploads():
    if not os.path.isdir(SUBMISSIONS_DIR):
        print("  uploads/: directory missing, skip")
        return
    count = 0
    for entry in os.scandir(SUBMISSIONS_DIR):
        if entry.is_file() or entry.is_symlink():
            os.unlink(entry.path)
            count += 1
        elif entry.is_dir():
            shutil.rmtree(entry.path)
            count += 1
    print(f"  uploads/: {count} items removed")

def clean_huey_db():
    if not os.path.isfile(HUEY_DB_PATH):
        print("  data/tasks.db: missing, skip")
        return
    conn = sqlite3.connect(HUEY_DB_PATH)
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (tname,) in tables:
            conn.execute(f'DELETE FROM "{tname}"')
        conn.commit()
        for ext in ("-wal", "-shm"):
            p = HUEY_DB_PATH + ext
            if os.path.isfile(p):
                try:
                    os.unlink(p)
                except OSError:
                    print(f"  data/tasks.db{ext}: locked by another process, skip")
        print(f"  data/tasks.db: {len(tables)} tables cleared")
    finally:
        conn.close()

def vacuum_db(cursor, conn):
    cursor.execute("VACUUM")
    conn.commit()
    print("  VACUUM done")

def main():
    parser = argparse.ArgumentParser(description="清除所有学生数据及测试痕迹")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    parser.add_argument("--skip-tasks-db", action="store_true", help="跳过清理任务队列")
    parser.add_argument("--skip-uploads", action="store_true", help="跳过清理上传文件")
    args = parser.parse_args()

    if not args.yes:
        print("=" * 60)
        print("  即将删除所有学生数据及测试痕迹！")
        print("  保留：表格结构、教师/Agent账号、问卷模板。\n  注意：任务（learning_tasks/tasks）也会被清除，之后用 seed_tasks.py 重新布置。")
        print("=" * 60)
        ans = input("\n继续执行？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            sys.exit(0)

    os.chdir(PROJECT_ROOT)

    if not os.path.isfile(DB_PATH):
        print(f"错误：未找到数据库 {DB_PATH}")
        sys.exit(1)

    print(f"\n数据库: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=OFF")
    cursor = conn.cursor()

    try:
        print("\n[1/7] 清空学生数据表...")
        clean_data_tables(cursor, conn)

        print("[2/7] 删除学生账号及关联...")
        delete_student_users(cursor, conn)

        print("[3/7] 重置 auto-increment...")
        reset_sqlite_sequence(cursor, conn)

        print("[4/7] 重置 settings...")
        reset_settings(cursor, conn)

        print("[5/7] 清理上传文件...")
        if args.skip_uploads:
            print("  跳过")
        else:
            clean_uploads()

        print("[6/7] 清理任务队列...")
        if args.skip_tasks_db:
            print("  跳过")
        else:
            clean_huey_db()

        print("[7/7] VACUUM 回收空间...")
        vacuum_db(cursor, conn)

        print("\n成功！所有学生数据已清除，数据库已恢复初始状态。")
    except Exception as e:
        conn.rollback()
        print(f"\n错误：{e}")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()


