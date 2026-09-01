# -*- coding: utf-8 -*-
"""Inspect the local SqliteHuey queue for unreadable task payloads.

By default this script is read-only. Use --purge-bad to remove rows that
cannot be deserialized by the current code and dependency versions.
"""
import argparse
import os
import sqlite3
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def iter_rows(conn):
    for table in ("task", "schedule"):
        try:
            rows = conn.execute(
                f"SELECT id, queue, data FROM {table} ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"{table}: unavailable ({exc})")
            continue
        for row_id, queue, data in rows:
            yield table, row_id, queue, data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.environ.get("HUEY_DB_PATH")
        or os.path.join(ROOT, "data", "tasks.db"),
        help="Path to SqliteHuey tasks.db",
    )
    parser.add_argument(
        "--purge-bad",
        action="store_true",
        help="Delete rows that cannot be deserialized",
    )
    args = parser.parse_args()

    os.environ["HUEY_DB_PATH"] = args.db

    from huey_instance import huey

    conn = sqlite3.connect(args.db)
    bad = []
    ok = 0
    try:
        for table, row_id, queue, data in iter_rows(conn):
            try:
                task = huey.deserialize_task(data)
            except Exception as exc:
                bad.append((table, row_id, queue, type(exc).__name__, str(exc)))
                print(f"BAD {table} id={row_id} queue={queue}: {type(exc).__name__}: {exc}")
            else:
                ok += 1
                print(
                    f"OK  {table} id={row_id} queue={queue}: "
                    f"{task.name} args={task.args!r} kwargs={task.kwargs!r}"
                )

        print(f"summary: ok={ok} bad={len(bad)} db={args.db}")

        if args.purge_bad and bad:
            for table, row_id, _queue, _etype, _msg in bad:
                conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
            conn.commit()
            print(f"purged bad rows: {len(bad)}")
    finally:
        conn.close()

    return 1 if bad and not args.purge_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
