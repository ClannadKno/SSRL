# -*- coding: utf-8 -*-
"""
SSRL-ESP Legacy Auth Data Cleanup Script.

Cleans duplicate groups, old test accounts, and deprecated tables.
Supports dry-run mode for safe preview.

Usage:
    python scripts/cleanup_auth_legacy.py --dry-run
    python scripts/cleanup_auth_legacy.py --apply
    python scripts/cleanup_auth_legacy.py --verify
"""
import argparse
import os
import shutil
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
   sys.path.insert(0, PROJECT_ROOT)

import db
from config import DB_PATH


def _backup_db():
   """Create a timestamped backup of the main database."""
   ts = datetime.now().strftime("%Y%m%d_%H%M%S")
   backup_path = DB_PATH + ".backup_auth_cleanup_" + ts
   shutil.copy2(DB_PATH, backup_path)
   print("[BACKUP] Database backed up to: %s" % backup_path)
   return backup_path


def _get_groups_info(conn):
   """Return list of groups with member count and reference info."""
   rows = conn.execute("""
       SELECT g.*,
           (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count,
           (SELECT COUNT(*) FROM experiment_participants ep WHERE ep.group_id = g.id) AS participant_count,
           (SELECT COUNT(*) FROM collaborative_documents cd WHERE cd.group_id = g.id) AS doc_count,
           (SELECT COUNT(*) FROM messages m WHERE m.group_id = g.id) AS msg_count
       FROM groups g ORDER BY g.id
   """).fetchall()
   return [dict(r) for r in rows]


def _get_group_references(conn, group_id):
   """Get total reference count for a group across all referencing tables."""
   tables = {
       "group_members": "group_id",
       "experiment_participants": "group_id",
       "messages": "group_id",
       "collaborative_documents": "group_id",
       "learning_tasks": None,
   }
   total = 0
   details = {}
   for table, fk_col in tables.items():
       if fk_col:
           row = conn.execute("SELECT COUNT(*) AS c FROM %s WHERE %s=?" % (table, fk_col), (group_id,)).fetchone()
           count = row["c"] if row else 0
           total += count
           details[table] = count
   return total, details


def _clean_duplicate_default_groups(conn, dry_run):
   """Remove duplicate 'Default Group' entries without group_code and no members."""
   groups = _get_groups_info(conn)
   default_groups = [g for g in groups if g["name"] == "Default Group" and not g["group_code"]]

   to_keep = None
   to_delete = []
   for g in default_groups:
       refs, details = _get_group_references(conn, g["id"])
       g["total_refs"] = refs
       g["ref_details"] = details
       if g["member_count"] > 0 or refs > 0:
           to_keep = g
       else:
           to_delete.append(g)

   print("\n=== Duplicate Default Groups ===")
   print("Found %d Default Group(s) without group_code" % len(default_groups))
   if to_keep:
       print("  [KEEP] id=%d name=%s members=%d refs=%d" % (
           to_keep["id"], to_keep["name"], to_keep["member_count"], to_keep["total_refs"]))
   for g in to_delete:
       print("  [DELETE] id=%d name=%s members=%d refs=%d (no active refs)" % (
           g["id"], g["name"], g["member_count"], g["total_refs"]))

   if not dry_run and to_delete:
       for g in to_delete:
           conn.execute("DELETE FROM groups WHERE id=?", (g["id"],))
       print("  -> Deleted %d empty Default Group(s)" % len(to_delete))

   return to_delete, to_keep


def _clean_duplicate_g01_g15(conn, dry_run):
   """Remove duplicate G01-G15 groups, keeping the latest active batch."""
   groups = _get_groups_info(conn)
   gcode_groups = {}
   for g in groups:
       gc = g.get("group_code")
       if gc and gc.startswith("G") and len(gc) == 3 and gc[1:].isdigit():
           gcode_groups.setdefault(gc, []).append(g)

   to_delete = []
   to_keep = []

   print("\n=== Duplicate G01-G15 Groups ===")
   for gc in sorted(gcode_groups.keys()):
       members = gcode_groups[gc]
       if len(members) <= 1:
           to_keep.extend(members)
           continue

       print("  group_code=%s: %d copies" % (gc, len(members)))
       scored = []
       for g in members:
           refs, details = _get_group_references(conn, g["id"])
           g["total_refs"] = refs
           g["ref_details"] = details
           score = 0
           if g["member_count"] > 0:
               score += 10
           if refs > 0:
               score += 5
           score += g["id"] / 10000.0  # newer id = slight tiebreaker
           scored.append((score, g))

       scored.sort(key=lambda x: -x[0])
       keep = scored[0][1]
       to_keep.append(keep)
       print("    [KEEP] id=%d members=%d refs=%d" % (keep["id"], keep["member_count"], keep["total_refs"]))

       for _, g in scored[1:]:
           if g["member_count"] > 0 or g["total_refs"] > 0:
               print("    [WARNING] id=%d has %d members and %d refs but is duplicate (skipping deletion)" % (
                   g["id"], g["member_count"], g["total_refs"]))
               to_keep.append(g)
           else:
               print("    [DELETE] id=%d members=%d refs=%d (no active refs)" % (
                   g["id"], g["member_count"], g["total_refs"]))
               to_delete.append(g)

   if not dry_run and to_delete:
       for g in to_delete:
           conn.execute("DELETE FROM groups WHERE id=?", (g["id"],))
       print("  -> Deleted %d duplicate G01-G15 group(s)" % len(to_delete))

   return to_delete, to_keep


def _clean_old_test_accounts(conn, dry_run):
   """Flag or remove old test accounts (t01-t04) that have no experiment_participants record."""
   test_users = conn.execute(
       "SELECT * FROM users WHERE username IN ('t01','t02','t03','t04') ORDER BY username"
   ).fetchall()

   print("\n=== Old Test Accounts (t01-t04) ===")
   if not test_users:
       print("  None found.")
       return [], []

   to_delete = []
   to_flag = []
   for u in test_users:
       u = dict(u)
       ep = conn.execute(
           "SELECT id FROM experiment_participants WHERE user_id=?", (u["id"],)
       ).fetchone()
       has_submissions = conn.execute(
           "SELECT id FROM submissions WHERE user_id=? LIMIT 1", (u["id"],)
       ).fetchone()
       has_messages = conn.execute(
           "SELECT id FROM messages WHERE user_id=? LIMIT 1", (u["id"],)
       ).fetchone()
       has_docs = conn.execute(
           "SELECT id FROM collaborative_documents WHERE created_by=? LIMIT 1", (u["id"],)
       ).fetchone()

       print("  username=%s id=%d has_ep=%s has_submissions=%s has_messages=%s has_docs=%s" % (
           u["username"], u["id"],
           "yes" if ep else "no",
           "yes" if has_submissions else "no",
           "yes" if has_messages else "no",
           "yes" if has_docs else "no",
       ))

       if not ep and not has_submissions and not has_messages and not has_docs:
           to_delete.append(u)
       else:
           to_flag.append(u)

   if to_delete:
       print("  %d account(s) to DELETE (no business references):" % len(to_delete))
       for u in to_delete:
           print("    - %s (id=%d)" % (u["username"], u["id"]))
       if not dry_run:
           ids = tuple(u["id"] for u in to_delete)
           placeholders = ",".join("?" * len(ids))
           conn.execute("DELETE FROM group_members WHERE user_id IN (%s)" % placeholders, ids)
           conn.execute("DELETE FROM users WHERE id IN (%s)" % placeholders, ids)
           print("  -> Deleted %d old test account(s)" % len(to_delete))

   if to_flag:
       print("  %d account(s) FLAGGED (has business references, not deleted):" % len(to_flag))
       for u in to_flag:
           print("    - %s (id=%d)" % (u["username"], u["id"]))

   return to_delete, to_flag


def _verify_cleanup(conn):
   """Run post-cleanup consistency checks."""
   print("\n=== Consistency Checks ===")

   # 1. Integrity check
   integrity = conn.execute("PRAGMA integrity_check").fetchone()
   print("  PRAGMA integrity_check: %s" % integrity[0] if integrity else "FAILED")

   # 2. Orphan group_members
   orphans = conn.execute("""
       SELECT gm.id FROM group_members gm
       LEFT JOIN groups g ON gm.group_id = g.id
       WHERE g.id IS NULL
   """).fetchall()
   print("  Orphan group_members: %d" % len(orphans))

   # 3. Orphan experiment_participants
   orphans_ep = conn.execute("""
       SELECT ep.id FROM experiment_participants ep
       LEFT JOIN groups g ON ep.group_id = g.id
       WHERE g.id IS NULL
   """).fetchall()
   print("  Orphan experiment_participants: %d" % len(orphans_ep))

   # 4. G01-G15 uniqueness
   dup_groups = conn.execute("""
       SELECT group_code, COUNT(*) AS cnt FROM groups
       WHERE group_code IS NOT NULL AND group_code != ''
       AND group_code LIKE 'G%' AND length(group_code)=3
       GROUP BY group_code HAVING cnt > 1
   """).fetchall()
   print("  Duplicate G01-G15 groups: %d" % len(dup_groups))
   for d in dup_groups:
       print("    %s: %d copies" % (d["group_code"], d["cnt"]))

   # 5. Empty Default Groups
   empty_default = conn.execute("""
       SELECT COUNT(*) AS cnt FROM groups g
       WHERE g.name='Default Group' AND (g.group_code IS NULL OR g.group_code='')
       AND NOT EXISTS (SELECT 1 FROM group_members gm WHERE gm.group_id = g.id)
   """).fetchone()
   print("  Empty Default Groups without group_code: %d" % (empty_default["cnt"] if empty_default else 0))


def main():
   parser = argparse.ArgumentParser(description="SSRL-ESP Legacy Auth Data Cleanup")
   parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying database")
   parser.add_argument("--apply", action="store_true", help="Execute cleanup (requires --apply flag)")
   parser.add_argument("--verify", action="store_true", help="Run consistency checks only")
   args = parser.parse_args()

   if not args.dry_run and not args.apply and not args.verify:
       parser.print_help()
       print("\nERROR: Specify --dry-run, --apply, or --verify")
       sys.exit(1)

   db.ensure_database_ready()
   conn = db.db()

   try:
       if args.verify:
           _verify_cleanup(conn)
           return

       dry_run = args.dry_run

       if not dry_run:
           _backup_db()

       print("Running in %s mode..." % ("DRY RUN" if dry_run else "APPLY"))

       _clean_duplicate_default_groups(conn, dry_run)
       _clean_duplicate_g01_g15(conn, dry_run)
       _clean_old_test_accounts(conn, dry_run)

       if not dry_run:
           conn.commit()
           print("\n[APPLIED] All changes committed to database.")
       else:
           print("\n[DRY RUN] No changes were made. Use --apply to execute.")

       _verify_cleanup(conn)

   finally:
       conn.close()


if __name__ == "__main__":
   main()
