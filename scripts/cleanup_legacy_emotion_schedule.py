# -*- coding: utf-8 -*-
"""Inspect or remove only legacy recursive emotion tasks from SqliteHuey.

The default is a read-only dry run.  Pass ``--apply`` to delete rows whose
deserialized task name is exactly one of the retired emotion entry points.
Other Huey tasks and schedule rows are never selected by SQL name guesses.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LEGACY_TASK_NAMES = frozenset(
    {"execute_emotion_reflection_tick", "schedule_emotion_reflection_for_session"}
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.environ.get("HUEY_DB_PATH") or os.path.join(ROOT, "data", "tasks.db"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    os.environ["HUEY_DB_PATH"] = db_path
    from huey_instance import huey

    conn = sqlite3.connect(db_path)
    matches = []
    unreadable = []
    total = {"task": 0, "schedule": 0}
    try:
        for table in ("task", "schedule"):
            rows = conn.execute(f"SELECT id, data FROM {table} ORDER BY id").fetchall()
            total[table] = len(rows)
            for row_id, payload in rows:
                try:
                    task = huey.deserialize_task(payload)
                except Exception as exc:
                    unreadable.append((table, row_id, type(exc).__name__))
                    continue
                if task.name in LEGACY_TASK_NAMES:
                    matches.append((table, int(row_id), task.name))

        for table, row_id, name in matches:
            print(f"MATCH {table} id={row_id} task={name}")
        for table, row_id, error_type in unreadable:
            print(f"UNREADABLE {table} id={row_id} error={error_type}")

        deleted = 0
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
            for table, row_id, _name in matches:
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE id=?", (row_id,)
                ).rowcount
            conn.commit()
        print(
            "summary "
            f"task_rows={total['task']} schedule_rows={total['schedule']} "
            f"matched={len(matches)} unreadable={len(unreadable)} "
            f"deleted={deleted} mode={'apply' if args.apply else 'dry-run'} db={db_path}"
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 1 if unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
