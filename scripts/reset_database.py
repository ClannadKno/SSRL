#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SSRL-ESP Database Reset Script (clean slate).

Deletes the database file entirely and recreates from scratch.
All sequences (user IDs, group IDs, etc.) start from 1.
Students, teacher, and SERA agent are pre-created with random login keys.
Default layout is 15 four-person groups plus G16/G17/G18 with 3, 2, and 1 members.

Login keys are generated independently on every reset and written only to the
configured keys CSV. Treat that CSV as a secret and never commit it.

Usage:
    python scripts/reset_database.py --dry-run
    python scripts/reset_database.py --apply
    python scripts/reset_database.py --apply --keys-csv data/login_keys.csv
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

# --- Constants ---
BASE_GROUP_COUNT = 15
MEMBERS_PER_GROUP = 4
EXTRA_GROUP_MEMBER_COUNTS = (3, 2, 1)
GROUP_COUNT = BASE_GROUP_COUNT + len(EXTRA_GROUP_MEMBER_COUNTS)
EXPECTED_STUDENT_COUNT = BASE_GROUP_COUNT * MEMBERS_PER_GROUP + sum(EXTRA_GROUP_MEMBER_COUNTS)
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


def _student_key(pc):
    return pc + "-" + secrets.token_hex(8).upper()


def _teacher_key():
    return "T-ADMIN-" + secrets.token_hex(8).upper()


def _build_group_specs():
    specs = []
    for i in range(1, BASE_GROUP_COUNT + 1):
        specs.append({
            "group_no": i,
            "group_code": "G%02d" % i,
            "member_count": MEMBERS_PER_GROUP,
        })
    for offset, member_count in enumerate(EXTRA_GROUP_MEMBER_COUNTS, start=1):
        group_no = BASE_GROUP_COUNT + offset
        specs.append({
            "group_no": group_no,
            "group_code": "G%02d" % group_no,
            "member_count": member_count,
        })
    return specs


def _layout_summary(group_specs):
    by_size = {}
    for spec in group_specs:
        member_count = spec["member_count"]
        by_size[member_count] = by_size.get(member_count, 0) + 1
    parts = []
    for member_count in sorted(by_size.keys(), reverse=True):
        parts.append("%d x %d-member" % (by_size[member_count], member_count))
    return ", ".join(parts)


def _default_keys_csv_path():
    db_dir = os.path.dirname(DB_PATH) or "."
    db_stem = os.path.splitext(os.path.basename(DB_PATH))[0]
    return os.path.join(db_dir, db_stem + "_login_keys.csv")


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
    print("[KEYS] Login keys CSV written: %s" % path)


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
            bp = os.path.join(backup_dir, base + ".backup_drop_" + ts)
            shutil.copy2(f, bp)
            print("[BACKUP] %s -> %s" % (f, bp))
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


def _seed_preset_data(conn, dry_run):
    """Insert preset users, groups, participants, and teacher keys."""
    group_specs = _build_group_specs()
    layout_text = _layout_summary(group_specs)
    if dry_run:
        print("[DRY RUN] Would seed: teacher, sera, %d students, %d groups, 1 teacher key" % (
            EXPECTED_STUDENT_COUNT,
            GROUP_COUNT,
        ))
        print("[DRY RUN] Group layout: %s" % layout_text)
        return []

    print("")
    key_rows = []

    # Teacher user (id=1 from seed data, but we ensure it without bumping AUTOINCREMENT)
    teacher = conn.execute("SELECT id FROM users WHERE username='teacher'").fetchone()
    if teacher:
        tid = teacher[0]
    else:
        tid = conn.execute(
            "INSERT INTO users(username,password_hash,real_name,role,created_at) VALUES(?,?,?,?,?)",
            ("teacher", generate_password_hash(os.urandom(24).hex()), "Teacher", "teacher", _now())
        ).lastrowid
    # Replace the teacher key with a fresh random value for this reset.
    conn.execute(
        "DELETE FROM teacher_access_keys WHERE key_name='default_teacher'"
    )
    teacher_key = _teacher_key()
    conn.execute(
        "INSERT INTO teacher_access_keys(key_name,key_hash,key_lookup_hash,teacher_user_id,is_active,created_at) VALUES(?,?,?,?,?,?)",
        (
            "default_teacher",
            generate_password_hash(teacher_key),
            compute_login_key_lookup_hash(teacher_key),
            tid,
            1,
            _now(),
        )
    )
    key_rows.append({
        "role": "teacher",
        "username": "teacher",
        "real_name": "Teacher",
        "participant_code": "",
        "display_name": "Teacher",
        "group_code": "",
        "group_no": "",
        "member_no": "",
        "group_id": "",
        "user_id": tid,
        "key_name": "default_teacher",
        "login_key": teacher_key,
    })
    print("  Teacher key:            generated (see ignored keys CSV)")

    # SERA agent (id=2 from seed data)
    sera = conn.execute("SELECT id FROM users WHERE username='sera'").fetchone()
    if not sera:
        conn.execute(
            "INSERT INTO users(username,password_hash,real_name,role,created_at) VALUES(?,?,?,?,?)",
            ("sera", generate_password_hash(os.urandom(24).hex()), "SERA", "agent", _now())
        )

    # Groups: G01-G15 have 4 members; G16+ provide smaller mixed-size cohorts.
    gids = []
    for spec in group_specs:
        group_no = spec["group_no"]
        gcode = spec["group_code"]
        gid = conn.execute(
            "INSERT INTO groups(name,group_code,condition,state,last_message_sequence,created_at) VALUES(?,?,?,?,?,?)",
            ("第%02d组" % group_no, gcode, "experiment", "OPEN", 0, _now())
        ).lastrowid
        spec["group_id"] = gid
        gids.append(gid)
    print("  Groups:                 G01-G%02d (ids %d-%d)" % (
        GROUP_COUNT,
        gids[0],
        gids[-1],
    ))
    print("  Group layout:           %s" % layout_text)

    # Experiment participants with fresh random keys
    total_students = 0
    for spec in group_specs:
        group_no = spec["group_no"]
        gcode = spec["group_code"]
        group_id = spec["group_id"]
        for mi in range(1, spec["member_count"] + 1):
            pc = "S1-" + gcode + "-M" + str(mi)
            dn = gcode + "-M" + str(mi)
            student_key = _student_key(pc)
            # Student user
            uid = conn.execute(
                "INSERT INTO users(username,password_hash,real_name,participant_code,role,created_at) VALUES(?,?,?,?,?,?)",
                (pc, generate_password_hash(os.urandom(24).hex()), dn, pc, "student", _now())
            ).lastrowid
            # Group member
            conn.execute("INSERT INTO group_members(group_id,user_id) VALUES(?,?)", (group_id, uid))
            # Experiment participant
            conn.execute(
                "INSERT INTO experiment_participants(participant_code,login_key_hash,key_lookup_hash,group_no,member_no,group_id,user_id,display_name,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    pc,
                    generate_password_hash(student_key),
                    compute_login_key_lookup_hash(student_key),
                    group_no,
                    mi,
                    group_id,
                    uid,
                    dn,
                    1,
                    _now(),
                )
            )
            key_rows.append({
                "role": "student",
                "username": pc,
                "real_name": dn,
                "participant_code": pc,
                "display_name": dn,
                "group_code": gcode,
                "group_no": group_no,
                "member_no": mi,
                "group_id": group_id,
                "user_id": uid,
                "key_name": pc,
                "login_key": student_key,
            })
            total_students += 1
    print("  Students:               %d (random keys; see ignored keys CSV)" % total_students)
    return key_rows


def _seed_questionnaires(conn, dry_run):
    """Seed formal fixed questionnaires after a clean database reset."""
    if dry_run:
        print("[DRY RUN] Would seed: 7 formal fixed questionnaires")
        return
    import seed_questionnaires_from_docs as qseed
    for payload in qseed.QUESTIONNAIRES:
        action, qid, item_count = qseed._upsert_questionnaire(conn, payload)
        print("  Questionnaire %-28s id=%s items=%s (%s)" % (
            payload["code"], qid, item_count, action
        ))


def _seed_tasks(conn, dry_run):
    """Seed the two formal collaborative learning tasks."""
    if dry_run:
        print("[DRY RUN] Would seed: 2 formal collaborative learning tasks")
        return
    import seed_tasks
    learning_count, legacy_count = seed_tasks.seed_all_tasks(conn)
    print("  Tasks seeded:            learning_tasks=%d, legacy_tasks=%d" % (
        learning_count, legacy_count
    ))


def _sync_strategy_definitions(conn, dry_run):
    """Sync the Markdown-backed three-stage strategy library into SQLite."""
    if dry_run:
        print("[DRY RUN] Would sync: three-stage strategy definitions")
        return None
    from services.three_stage_strategy_library import sync_strategy_definitions
    meta = sync_strategy_definitions(conn)
    print("  Strategy definitions:    %d (%s)" % (
        meta["strategy_count"], meta["version"]
    ))
    return meta


def _verify(conn):
    """Post-reset consistency checks."""
    print("\n=== Verification ===")
    row = conn.execute("PRAGMA integrity_check").fetchone()
    print("  Integrity:                %s" % (row[0] if row else "FAILED"))
    import seed_tasks
    from services.three_stage_strategy_library import get_strategy_library_metadata
    strategy_meta = get_strategy_library_metadata()
    tc = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='teacher'").fetchone()[0]
    ac = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='agent'").fetchone()[0]
    sc = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='student'").fetchone()[0]
    gc = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()[0]
    mc = conn.execute("SELECT COUNT(*) AS c FROM group_members").fetchone()[0]
    ep = conn.execute("SELECT COUNT(*) AS c FROM experiment_participants WHERE is_active=1").fetchone()[0]
    tk = conn.execute("SELECT COUNT(*) AS c FROM teacher_access_keys WHERE is_active=1").fetchone()[0]
    ltc = conn.execute("SELECT COUNT(*) AS c FROM learning_tasks").fetchone()[0]
    lgtc = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()[0]
    expected_task_limit = seed_tasks.FORMAL_TASK_TIME_LIMIT_MINUTES
    matching_task_limit_count = conn.execute(
        "SELECT COUNT(*) AS c FROM learning_tasks WHERE time_limit_minutes=?",
        (expected_task_limit,),
    ).fetchone()[0]
    strategy_count = conn.execute(
        "SELECT COUNT(*) AS c FROM strategy_definitions WHERE is_active=1"
    ).fetchone()[0]
    pipeline_count = conn.execute("SELECT COUNT(*) AS c FROM strategy_pipeline_runs").fetchone()[0]
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
    expected_layout = [spec["member_count"] for spec in _build_group_specs()]
    first_user = conn.execute("SELECT MIN(id) FROM users").fetchone()[0]
    last_user = conn.execute("SELECT MAX(id) FROM users").fetchone()[0]
    print("  Teacher:                  %d" % tc)
    print("  Agent:                    %d" % ac)
    print("  Students:                 %d" % sc)
    print("  Groups:                   %d" % gc)
    print("  Members:                  %d" % mc)
    print("  Active participants:      %d" % ep)
    print("  Teacher keys:             %d" % tk)
    print("  Learning tasks:           %d" % ltc)
    print("  Task time limit:          %d minutes (%d/%d tasks)" % (
        expected_task_limit,
        matching_task_limit_count,
        ltc,
    ))
    print("  Legacy tasks:             %d" % lgtc)
    print("  Strategy definitions:     %d active (%s expected)" % (
        strategy_count,
        strategy_meta["strategy_count"],
    ))
    print("  Strategy pipeline runs:   %d" % pipeline_count)
    print("  Group member layout:      %s" % observed_layout)
    print("  User IDs range:           %d - %d" % (first_user, last_user))

    expected_students = EXPECTED_STUDENT_COUNT
    ok = True
    if tc != 1:
        print("  [FAIL] Expected 1 teacher, got %d" % tc)
        ok = False
    if ac != 1:
        print("  [FAIL] Expected 1 agent, got %d" % ac)
        ok = False
    if sc != expected_students:
        print("  [FAIL] Expected %d students, got %d" % (expected_students, sc))
        ok = False
    if gc != GROUP_COUNT:
        print("  [FAIL] Expected %d groups, got %d" % (GROUP_COUNT, gc))
        ok = False
    if mc != expected_students:
        print("  [FAIL] Expected %d members, got %d" % (expected_students, mc))
        ok = False
    if ep != expected_students:
        print("  [FAIL] Expected %d active participants, got %d" % (expected_students, ep))
        ok = False
    if tk != 1:
        print("  [FAIL] Expected 1 teacher key, got %d" % tk)
        ok = False
    if ltc != 2:
        print("  [FAIL] Expected 2 learning tasks, got %d" % ltc)
        ok = False
    if matching_task_limit_count != ltc:
        print("  [FAIL] Expected all learning tasks to use a %d-minute limit, got %d/%d" % (
            expected_task_limit,
            matching_task_limit_count,
            ltc,
        ))
        ok = False
    if lgtc != 2:
        print("  [FAIL] Expected 2 legacy tasks, got %d" % lgtc)
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
    if observed_layout != expected_layout:
        print("  [FAIL] Expected layout %s, got %s" % (expected_layout, observed_layout))
        ok = False
    if first_user != 1:
        print("  [FAIL] First user ID should be 1 (fresh DB), got %d" % first_user)
        ok = False
    if ok:
        print("  All checks:               PASS")


def main():
    p = argparse.ArgumentParser(description="SSRL-ESP Database Reset (clean slate)")
    p.add_argument("--dry-run", action="store_true", help="Preview only (no changes)")
    p.add_argument("--apply", action="store_true", help="Execute the reset")
    p.add_argument(
        "--keys-csv",
        default=None,
        help="Path for plaintext login keys CSV (default: next to database)",
    )
    args = p.parse_args()
    if not args.dry_run and not args.apply:
        p.print_help()
        print("\nERROR: Specify --dry-run or --apply")
        sys.exit(1)

    if args.dry_run:
        keys_csv_path = args.keys_csv or _default_keys_csv_path()
        print("DRY RUN: Would perform the following steps:")
        print("  1. Backup app database %s and Huey queue %s" % (DB_PATH, HUEY_DB_PATH))
        print("  2. Delete database/queue files + WAL/SHM journals")
        print("  3. Recreate all tables from scratch (init_db)")
        group_specs = _build_group_specs()
        print("  4. Seed teacher, sera, %d groups, %d students, teacher key" % (
            GROUP_COUNT,
            EXPECTED_STUDENT_COUNT,
        ))
        print("     Layout: %s" % _layout_summary(group_specs))
        print("  5. Seed formal fixed questionnaires")
        print("  6. Seed two formal collaborative learning tasks")
        print("  7. Sync three-stage strategy definitions")
        print("  8. Write plaintext login keys CSV: %s" % keys_csv_path)
        print("  9. Verify consistency")
        print("\n--- Current database state (before reset) ---")
        import db as _db
        _db.DB_READY = False
        _db.ensure_database_ready()
        conn = _db.db()
        try:
            tc = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='teacher'").fetchone()[0]
            sc = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='student'").fetchone()[0]
            gc = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()[0]
            fu = conn.execute("SELECT MIN(id) FROM users").fetchone()[0]
            lu = conn.execute("SELECT MAX(id) FROM users").fetchone()[0]
            print("  Current users: %d (id range: %d - %d)" % (tc + sc, fu, lu))
            print("  Current groups: %d" % gc)
        finally:
            conn.close()
        return

    # -- Apply mode --
    _backup_and_remove_db()
    _ensure_fresh_db()

    import db as _db
    conn = _db.db()
    try:
        key_rows = _seed_preset_data(conn, dry_run=False)
        _seed_questionnaires(conn, dry_run=False)
        _seed_tasks(conn, dry_run=False)
        _sync_strategy_definitions(conn, dry_run=False)
        conn.commit()
        keys_csv_path = args.keys_csv or _default_keys_csv_path()
        _write_keys_csv(key_rows, keys_csv_path)
        print("\n[APPLIED] Reset complete. Database is clean with fresh random keys.")
        _verify(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
