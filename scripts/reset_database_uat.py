#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SSRL-ESP UAT Database Reset Script (small mixed-group clean slate).

Deletes the configured database file entirely and recreates it from scratch.
Designed for上线测试/验收环境, with a much smaller mixed group layout.

Default layout:
  - 3 one-person groups
  - 3 two-person groups
  - 3 three-person groups

Login keys are generated independently on every reset and written only to the
configured keys CSV. Treat that CSV as a secret and never commit it.

Usage:
    python scripts/reset_database_uat.py --dry-run
    python scripts/reset_database_uat.py --apply
    python scripts/reset_database_uat.py --apply --single-groups 2 --double-groups 2 --triple-groups 2
    python scripts/reset_database_uat.py --apply --keys-csv data/uat_login_keys.csv
"""
import argparse
import csv
import os
import secrets
import shutil
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import DB_PATH, HUEY_DB_PATH
from services.login_key_lookup import compute_login_key_lookup_hash
from werkzeug.security import generate_password_hash


DEFAULT_GROUPS_PER_SIZE = 3
TEACHER_KEY_NAME = "uat_teacher"
TEACHER_DISPLAY_NAME = "UAT Teacher"
PARTICIPANT_PREFIX = "UAT"
KEY_CSV_COLUMNS = [
    "role",
    "username",
    "real_name",
    "participant_code",
    "display_name",
    "group_code",
    "group_no",
    "member_no",
    "group_id",
    "user_id",
    "key_name",
    "login_key",
]


def _student_key(participant_code):
    return participant_code + "-" + secrets.token_hex(8).upper()


def _teacher_key():
    return "T-UAT-" + secrets.token_hex(8).upper()


def _default_keys_csv_path():
    db_dir = os.path.dirname(DB_PATH) or "."
    db_stem = os.path.splitext(os.path.basename(DB_PATH))[0]
    return os.path.join(db_dir, db_stem + "_uat_login_keys.csv")


def _write_keys_csv(rows, path):
    """Write plaintext login keys generated during reset."""
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=KEY_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print("[KEYS] UAT login keys CSV written: %s" % path)


def _reset_file_paths():
    paths = []
    seen = set()
    for base_path in (DB_PATH, HUEY_DB_PATH):
        for candidate in (base_path, base_path + "-wal", base_path + "-shm"):
            normalized = os.path.abspath(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            paths.append(candidate)
    return paths


def _backup_and_remove_db():
    """Backup the app DB and Huey queue DB, then delete their WAL/SHM journals."""
    for f in _reset_file_paths():
        if os.path.exists(f):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.basename(f)
            backup_dir = os.path.dirname(os.path.abspath(f)) or "."
            backup_path = os.path.join(backup_dir, base + ".backup_drop_uat_" + ts)
            shutil.copy2(f, backup_path)
            print("[BACKUP] %s -> %s" % (f, backup_path))
            os.remove(f)
            print("[DELETE] %s removed" % f)


def _ensure_fresh_db():
    """Force db module to recreate all tables from scratch."""
    import db as _db
    _db.DB_READY = False
    _db.ensure_database_ready()
    print("[DB] Fresh database created with all tables")


def _now():
    from db import now_str
    return now_str()


def _build_group_specs(single_groups, double_groups, triple_groups):
    specs = []
    group_no = 1
    for member_count, count in (
        (1, single_groups),
        (2, double_groups),
        (3, triple_groups),
    ):
        for _ in range(count):
            specs.append({
                "group_no": group_no,
                "group_code": "G%02d" % group_no,
                "member_count": member_count,
            })
            group_no += 1
    return specs


def _layout_summary(group_specs):
    counts = {1: 0, 2: 0, 3: 0}
    for spec in group_specs:
        counts[spec["member_count"]] = counts.get(spec["member_count"], 0) + 1
    students = sum(spec["member_count"] for spec in group_specs)
    return (
        "%d one-person, %d two-person, %d three-person groups" % (
            counts.get(1, 0),
            counts.get(2, 0),
            counts.get(3, 0),
        ),
        students,
    )


def _group_name(group_no, member_count):
    return "上线测试第%02d组（%d人组）" % (group_no, member_count)


def _participant_code(group_code, member_no):
    return "%s-%s-M%d" % (PARTICIPANT_PREFIX, group_code, member_no)


def _seed_preset_data(conn, dry_run, group_specs):
    """Insert UAT users, mixed-size groups, participants, and teacher key."""
    layout_text, total_students = _layout_summary(group_specs)
    if dry_run:
        print("[DRY RUN] Would seed: teacher, sera, %d students, %d groups, 1 UAT teacher key" % (
            total_students,
            len(group_specs),
        ))
        print("[DRY RUN] UAT layout: %s" % layout_text)
        return []

    print("")
    print("[UAT] Layout: %s" % layout_text)
    key_rows = []

    teacher = conn.execute("SELECT id FROM users WHERE username='teacher'").fetchone()
    if teacher:
        teacher_id = teacher[0]
    else:
        teacher_id = conn.execute(
            "INSERT INTO users(username,password_hash,real_name,role,created_at) VALUES(?,?,?,?,?)",
            ("teacher", generate_password_hash(os.urandom(24).hex()), TEACHER_DISPLAY_NAME, "teacher", _now()),
        ).lastrowid

    conn.execute("DELETE FROM teacher_access_keys WHERE key_name=?", (TEACHER_KEY_NAME,))
    teacher_key = _teacher_key()
    conn.execute(
        "INSERT INTO teacher_access_keys(key_name,key_hash,key_lookup_hash,teacher_user_id,is_active,created_at) VALUES(?,?,?,?,?,?)",
        (
            TEACHER_KEY_NAME,
            generate_password_hash(teacher_key),
            compute_login_key_lookup_hash(teacher_key),
            teacher_id,
            1,
            _now(),
        ),
    )
    key_rows.append({
        "role": "teacher",
        "username": "teacher",
        "real_name": TEACHER_DISPLAY_NAME,
        "participant_code": "",
        "display_name": TEACHER_DISPLAY_NAME,
        "group_code": "",
        "group_no": "",
        "member_no": "",
        "group_id": "",
        "user_id": teacher_id,
        "key_name": TEACHER_KEY_NAME,
        "login_key": teacher_key,
    })
    print("  UAT teacher key:        generated (see ignored keys CSV)")

    sera = conn.execute("SELECT id FROM users WHERE username='sera'").fetchone()
    if not sera:
        conn.execute(
            "INSERT INTO users(username,password_hash,real_name,role,created_at) VALUES(?,?,?,?,?)",
            ("sera", generate_password_hash(os.urandom(24).hex()), "SERA", "agent", _now()),
        )

    group_ids = []
    for spec in group_specs:
        group_id = conn.execute(
            "INSERT INTO groups(name,group_code,condition,state,last_message_sequence,created_at) VALUES(?,?,?,?,?,?)",
            (
                _group_name(spec["group_no"], spec["member_count"]),
                spec["group_code"],
                "experiment",
                "OPEN",
                0,
                _now(),
            ),
        ).lastrowid
        group_ids.append(group_id)
        spec["group_id"] = group_id

    print("  Groups:                 %s-%s (ids %d-%d)" % (
        group_specs[0]["group_code"],
        group_specs[-1]["group_code"],
        group_ids[0],
        group_ids[-1],
    ))

    total_students = 0
    for spec in group_specs:
        group_no = spec["group_no"]
        group_code = spec["group_code"]
        group_id = spec["group_id"]
        member_count = spec["member_count"]
        for member_no in range(1, member_count + 1):
            participant_code = _participant_code(group_code, member_no)
            display_name = participant_code
            student_key = _student_key(participant_code)
            user_id = conn.execute(
                "INSERT INTO users(username,password_hash,real_name,participant_code,role,created_at) VALUES(?,?,?,?,?,?)",
                (
                    participant_code,
                    generate_password_hash(os.urandom(24).hex()),
                    display_name,
                    participant_code,
                    "student",
                    _now(),
                ),
            ).lastrowid
            conn.execute("INSERT INTO group_members(group_id,user_id) VALUES(?,?)", (group_id, user_id))
            conn.execute(
                "INSERT INTO experiment_participants(participant_code,login_key_hash,key_lookup_hash,group_no,member_no,group_id,user_id,display_name,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    participant_code,
                    generate_password_hash(student_key),
                    compute_login_key_lookup_hash(student_key),
                    group_no,
                    member_no,
                    group_id,
                    user_id,
                    display_name,
                    1,
                    _now(),
                ),
            )
            key_rows.append({
                "role": "student",
                "username": participant_code,
                "real_name": display_name,
                "participant_code": participant_code,
                "display_name": display_name,
                "group_code": group_code,
                "group_no": group_no,
                "member_no": member_no,
                "group_id": group_id,
                "user_id": user_id,
                "key_name": participant_code,
                "login_key": student_key,
            })
            total_students += 1
    print("  Students:               %d (random keys; see ignored keys CSV)" % total_students)
    print("  ... (%d students total)" % total_students)
    return key_rows


def _seed_questionnaires(conn, dry_run):
    """Seed formal fixed questionnaires after a clean database reset."""
    if dry_run:
        print("[DRY RUN] Would seed: 7 formal fixed questionnaires")
        return
    import seed_questionnaires_from_docs as qseed
    for payload in qseed.QUESTIONNAIRES:
        action, questionnaire_id, item_count = qseed._upsert_questionnaire(conn, payload)
        print("  Questionnaire %-28s id=%s items=%s (%s)" % (
            payload["code"], questionnaire_id, item_count, action,
        ))


def _seed_tasks(conn, dry_run):
    """Seed the two formal collaborative learning tasks."""
    if dry_run:
        print("[DRY RUN] Would seed: 2 formal collaborative learning tasks")
        return
    import seed_tasks
    learning_count, legacy_count = seed_tasks.seed_all_tasks(conn)
    print("  Tasks seeded:            learning_tasks=%d, legacy_tasks=%d" % (
        learning_count, legacy_count,
    ))


def _sync_strategy_definitions(conn, dry_run):
    """Sync the Markdown-backed three-stage strategy library into SQLite."""
    if dry_run:
        print("[DRY RUN] Would sync: three-stage strategy definitions")
        return None
    from services.three_stage_strategy_library import sync_strategy_definitions
    meta = sync_strategy_definitions(conn)
    print("  Strategy definitions:    %d (%s)" % (
        meta["strategy_count"], meta["version"],
    ))
    return meta


def _verify(conn, group_specs):
    """Post-reset consistency checks."""
    expected_groups = len(group_specs)
    expected_students = sum(spec["member_count"] for spec in group_specs)
    expected_members = expected_students

    print("\n=== UAT Verification ===")
    row = conn.execute("PRAGMA integrity_check").fetchone()
    print("  Integrity:                %s" % (row[0] if row else "FAILED"))
    import seed_tasks
    from services.three_stage_strategy_library import get_strategy_library_metadata
    strategy_meta = get_strategy_library_metadata()
    teacher_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='teacher'").fetchone()[0]
    agent_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='agent'").fetchone()[0]
    student_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='student'").fetchone()[0]
    group_count = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()[0]
    member_count = conn.execute("SELECT COUNT(*) AS c FROM group_members").fetchone()[0]
    active_participants = conn.execute(
        "SELECT COUNT(*) AS c FROM experiment_participants WHERE is_active=1"
    ).fetchone()[0]
    teacher_key_count = conn.execute(
        "SELECT COUNT(*) AS c FROM teacher_access_keys WHERE is_active=1 AND key_name=?",
        (TEACHER_KEY_NAME,),
    ).fetchone()[0]
    learning_task_count = conn.execute("SELECT COUNT(*) AS c FROM learning_tasks").fetchone()[0]
    legacy_task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()[0]
    expected_task_limit = seed_tasks.FORMAL_TASK_TIME_LIMIT_MINUTES
    matching_task_limit_count = conn.execute(
        "SELECT COUNT(*) AS c FROM learning_tasks WHERE time_limit_minutes=?",
        (expected_task_limit,),
    ).fetchone()[0]
    strategy_count = conn.execute(
        "SELECT COUNT(*) AS c FROM strategy_definitions WHERE is_active=1"
    ).fetchone()[0]
    pipeline_count = conn.execute("SELECT COUNT(*) AS c FROM strategy_pipeline_runs").fetchone()[0]
    first_user = conn.execute("SELECT MIN(id) FROM users").fetchone()[0]
    last_user = conn.execute("SELECT MAX(id) FROM users").fetchone()[0]
    layout_rows = conn.execute(
        """
        SELECT group_no, COUNT(*) AS c
        FROM experiment_participants
        WHERE is_active=1
        GROUP BY group_no
        ORDER BY group_no
        """
    ).fetchall()
    observed_layout = [row[1] for row in layout_rows]
    expected_layout = [spec["member_count"] for spec in group_specs]

    print("  Teacher:                  %d" % teacher_count)
    print("  Agent:                    %d" % agent_count)
    print("  Students:                 %d" % student_count)
    print("  Groups:                   %d" % group_count)
    print("  Members:                  %d" % member_count)
    print("  Active participants:      %d" % active_participants)
    print("  UAT teacher keys:         %d" % teacher_key_count)
    print("  Learning tasks:           %d" % learning_task_count)
    print("  Task time limit:          %d minutes (%d/%d tasks)" % (
        expected_task_limit,
        matching_task_limit_count,
        learning_task_count,
    ))
    print("  Legacy tasks:             %d" % legacy_task_count)
    print("  Strategy definitions:     %d active (%s expected)" % (
        strategy_count,
        strategy_meta["strategy_count"],
    ))
    print("  Strategy pipeline runs:   %d" % pipeline_count)
    print("  Group member layout:      %s" % observed_layout)
    print("  User IDs range:           %d - %d" % (first_user, last_user))

    ok = True
    if teacher_count != 1:
        print("  [FAIL] Expected 1 teacher, got %d" % teacher_count)
        ok = False
    if agent_count != 1:
        print("  [FAIL] Expected 1 agent, got %d" % agent_count)
        ok = False
    if student_count != expected_students:
        print("  [FAIL] Expected %d students, got %d" % (expected_students, student_count))
        ok = False
    if group_count != expected_groups:
        print("  [FAIL] Expected %d groups, got %d" % (expected_groups, group_count))
        ok = False
    if member_count != expected_members:
        print("  [FAIL] Expected %d members, got %d" % (expected_members, member_count))
        ok = False
    if active_participants != expected_students:
        print("  [FAIL] Expected %d active participants, got %d" % (expected_students, active_participants))
        ok = False
    if teacher_key_count != 1:
        print("  [FAIL] Expected 1 active UAT teacher key, got %d" % teacher_key_count)
        ok = False
    if learning_task_count != 2:
        print("  [FAIL] Expected 2 learning tasks, got %d" % learning_task_count)
        ok = False
    if matching_task_limit_count != learning_task_count:
        print("  [FAIL] Expected all learning tasks to use a %d-minute limit, got %d/%d" % (
            expected_task_limit,
            matching_task_limit_count,
            learning_task_count,
        ))
        ok = False
    if legacy_task_count != 2:
        print("  [FAIL] Expected 2 legacy tasks, got %d" % legacy_task_count)
        ok = False
    if strategy_count != strategy_meta["strategy_count"]:
        print("  [FAIL] Expected %d active strategy definitions, got %d" % (
            strategy_meta["strategy_count"],
            strategy_count,
        ))
        ok = False
    if pipeline_count != 0:
        print("  [FAIL] Expected 0 strategy pipeline runs after reset, got %d" % pipeline_count)
        ok = False
    if first_user != 1:
        print("  [FAIL] First user ID should be 1 (fresh DB), got %d" % first_user)
        ok = False
    if observed_layout != expected_layout:
        print("  [FAIL] Expected layout %s, got %s" % (expected_layout, observed_layout))
        ok = False
    if ok:
        print("  All checks:               PASS")


def main():
    parser = argparse.ArgumentParser(description="SSRL-ESP UAT Database Reset (small mixed groups)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only (no changes)")
    parser.add_argument("--apply", action="store_true", help="Execute the UAT reset")
    parser.add_argument(
        "--single-groups",
        type=int,
        choices=(2, 3),
        default=DEFAULT_GROUPS_PER_SIZE,
        help="Number of one-person groups (2 or 3; default: 3)",
    )
    parser.add_argument(
        "--double-groups",
        type=int,
        choices=(2, 3),
        default=DEFAULT_GROUPS_PER_SIZE,
        help="Number of two-person groups (2 or 3; default: 3)",
    )
    parser.add_argument(
        "--triple-groups",
        type=int,
        choices=(2, 3),
        default=DEFAULT_GROUPS_PER_SIZE,
        help="Number of three-person groups (2 or 3; default: 3)",
    )
    parser.add_argument(
        "--keys-csv",
        default=None,
        help="Path for plaintext UAT login keys CSV (default: next to database)",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        parser.print_help()
        print("\nERROR: Specify --dry-run or --apply")
        sys.exit(1)

    group_specs = _build_group_specs(args.single_groups, args.double_groups, args.triple_groups)
    layout_text, total_students = _layout_summary(group_specs)
    keys_csv_path = args.keys_csv or _default_keys_csv_path()

    if args.dry_run:
        print("DRY RUN: Would perform the following UAT reset steps:")
        print("  1. Backup app database %s and Huey queue %s" % (DB_PATH, HUEY_DB_PATH))
        print("  2. Delete database/queue files + WAL/SHM journals")
        print("  3. Recreate all tables from scratch (init_db)")
        print("  4. Seed teacher, sera, %d groups, %d students, UAT teacher key" % (
            len(group_specs),
            total_students,
        ))
        print("     Layout: %s" % layout_text)
        print("  5. Seed formal fixed questionnaires")
        print("  6. Seed two formal collaborative learning tasks")
        print("  7. Sync three-stage strategy definitions")
        print("  8. Write plaintext UAT login keys CSV: %s" % keys_csv_path)
        print("  9. Verify consistency")
        print("\n--- Current database state (before reset) ---")
        import db as _db
        _db.DB_READY = False
        _db.ensure_database_ready()
        conn = _db.db()
        try:
            teacher_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='teacher'").fetchone()[0]
            student_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='student'").fetchone()[0]
            group_count = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()[0]
            first_user = conn.execute("SELECT MIN(id) FROM users").fetchone()[0]
            last_user = conn.execute("SELECT MAX(id) FROM users").fetchone()[0]
            print("  Current users: %d (id range: %s - %s)" % (
                teacher_count + student_count,
                first_user,
                last_user,
            ))
            print("  Current groups: %d" % group_count)
        finally:
            conn.close()
        return

    _backup_and_remove_db()
    _ensure_fresh_db()

    import db as _db
    conn = _db.db()
    try:
        key_rows = _seed_preset_data(conn, dry_run=False, group_specs=group_specs)
        _seed_questionnaires(conn, dry_run=False)
        _seed_tasks(conn, dry_run=False)
        _sync_strategy_definitions(conn, dry_run=False)
        conn.commit()
        _write_keys_csv(key_rows, keys_csv_path)
        print("\n[APPLIED] UAT reset complete. Database is clean with mixed-size UAT groups.")
        _verify(conn, group_specs)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
