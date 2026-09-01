#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Database Health Check Utility for SSRL-ESP."""
import os
import sys
import sqlite3
import json
from datetime import datetime

def fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"

def _get_file_sizes(*paths):
    result = {}
    for p in paths:
        try:
            result[p] = os.path.getsize(p)
        except OSError:
            result[p] = None
    return result

def _count_rows(conn, table):
    try:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM [{table}]").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return -1

def _get_max_text_length(conn, table, column):
    try:
        row = conn.execute(
            f"SELECT COALESCE(MAX(LENGTH(COALESCE([{column}],''))),0) AS max_len FROM [{table}]"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return -1

def _huey_stats(tasks_db_path):
    stats = {"pending": -1, "scheduled": -1, "tasks_db_size": None}
    if not tasks_db_path or not os.path.exists(tasks_db_path):
        return stats
    stats["tasks_db_size"] = os.path.getsize(tasks_db_path)
    try:
        conn = sqlite3.connect(tasks_db_path)
        conn.row_factory = sqlite3.Row
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        stats["tables"] = tables
        for table_name in ("task", "tasks", "huey_task"):
            if table_name in tables:
                try:
                    stats["total_tasks"] = int(
                        conn.execute(f"SELECT COUNT(*) AS c FROM [{table_name}]").fetchone()[0]
                    )
                except Exception:
                    pass
                for col in ("status", "state"):
                    try:
                        rows = conn.execute(
                            f"SELECT [{col}], COUNT(*) AS c FROM [{table_name}] GROUP BY [{col}]"
                        ).fetchall()
                        stats[f"by_{col}"] = {r[col]: int(r["c"]) for r in rows}
                    except Exception:
                        pass
        for t in tables:
            try:
                stats[f"table_{t}_rows"] = int(
                    conn.execute(f"SELECT COUNT(*) AS c FROM [{t}]").fetchone()[0]
                )
            except Exception:
                pass
        conn.close()
    except Exception as e:
        stats["error"] = str(e)
    return stats

def check_health(db_path, tasks_db_path=None):
    health = {"timestamp": datetime.now().isoformat(), "db_path": db_path, "tasks_db_path": tasks_db_path}
    wal_path = db_path + "-wal"
    shm_path = db_path + "-shm"
    file_paths = [db_path, wal_path, shm_path]
    if tasks_db_path:
        file_paths += [tasks_db_path, tasks_db_path + "-wal", tasks_db_path + "-shm"]
    health["file_sizes"] = _get_file_sizes(*file_paths)
    if not os.path.exists(db_path):
        health["error"] = f"Database not found: {db_path}"
        return health
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        health["page_size"] = conn.execute("PRAGMA page_size").fetchone()[0]
        health["page_count"] = conn.execute("PRAGMA page_count").fetchone()[0]
        health["freelist_count"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
        health["file_size_estimate"] = health["page_size"] * health["page_count"]
        health["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        try:
            cp = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            health["wal_checkpoint"] = {"busy": int(cp[0]), "log": int(cp[1]), "checkpointed": int(cp[2])}
        except Exception as e:
            health["wal_checkpoint"] = {"error": str(e)}
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            health["integrity_check"] = [row[0] for row in integrity]
        except Exception:
            health["integrity_check"] = "error"
        try:
            fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            health["foreign_key_check"] = [{"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]} for r in fk_issues]
        except Exception:
            health["foreign_key_check"] = "error"
        tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        health["tables"] = tables
        row_counts = {}
        for table in tables:
            row_counts[table] = _count_rows(conn, table)
        health["row_counts"] = row_counts
        dist = {}
        for table, col in (("monitor_runs","status"),("intervention_runs","status"),("help_requests","status"),("messages","role"),("groups","state")):
            if table in tables:
                try:
                    rows = conn.execute(f"SELECT [{col}], COUNT(*) AS c FROM [{table}] GROUP BY [{col}]").fetchall()
                    dist[f"{table}.{col}"] = {r[col]: int(r["c"]) for r in rows}
                except Exception:
                    pass
        health["distributions"] = dist
        text_columns = [
            ("monitor_runs","rule_result_json"),("monitor_runs","llm_result_json"),("monitor_runs","failure_reason"),
            ("state_assessments","rule_result_json"),("state_assessments","llm_result_json"),("state_assessments","fusion_json"),
            ("intervention_runs","generated_message"),("intervention_runs","failure_reason"),("intervention_runs","candidate_strategies"),
            ("agent_suggestions","llm_analysis_json"),("help_requests","failure_reason"),("help_requests","response_message"),
        ]
        ml = {}
        for table, col in text_columns:
            if table in tables:
                v = _get_max_text_length(conn, table, col)
                if v >= 0:
                    ml[f"{table}.{col}"] = v
        health["max_field_lengths"] = ml
        conn.close()
    except Exception as e:
        health["db_error"] = str(e)
    if tasks_db_path:
        health["huey_stats"] = _huey_stats(tasks_db_path)
    return health

def format_report(health, verbose=False):
    lines = []
    lines.append("="*60)
    lines.append(f"  Database Health Report  ({health.get('timestamp','N/A')})")
    lines.append("="*60)
    lines.append("\n--- File Sizes ---")
    for path, size in health.get("file_sizes",{}).items():
        label = os.path.basename(path) if path else "?"
        if size is not None:
            lines.append(f"  {label:20s}  {fmt_size(size):>10s}  ({size:,} bytes)")
        else:
            lines.append(f"  {label:20s}  {'N/A':>10s}")
    lines.append("\n--- SQLite Page Info ---")
    lines.append(f"  page_size:       {health.get('page_size','N/A')}")
    lines.append(f"  page_count:      {health.get('page_count','N/A')}")
    lines.append(f"  freelist_count:  {health.get('freelist_count','N/A')}")
    lines.append(f"  file_estimate:   {fmt_size(health.get('file_size_estimate',0))}")
    lines.append(f"  journal_mode:    {health.get('journal_mode','N/A')}")
    integ = health.get("integrity_check",[])
    lines.append(f"  integrity_check: {'ok' if integ==['ok'] else integ}")
    fk = health.get("foreign_key_check",[])
    lines.append(f"  foreign_key_check: {'ok' if len(fk)==0 else fk}")
    if "wal_checkpoint" in health:
        lines.append(f"  wal_checkpoint:  {health['wal_checkpoint']}")
    lines.append("\n--- Table Row Counts ---")
    for table, count in sorted(health.get("row_counts",{}).items()):
        lines.append(f"  {table:30s}  {count:,}")
    lines.append("\n--- Status Distributions ---")
    for key, val in sorted(health.get("distributions",{}).items()):
        lines.append(f"  {key:40s}  {val}")
    lines.append("\n--- Max JSON/TEXT Field Lengths ---")
    for field, length in sorted(health.get("max_field_lengths",{}).items()):
        lines.append(f"  {field:50s}  {length:,} chars ({fmt_size(length)})")
    if "huey_stats" in health:
        hs = health["huey_stats"]
        lines.append("\n--- Huey Task Queue Stats ---")
        lines.append(f"  tasks_db size:  {fmt_size(hs.get('tasks_db_size',0)) if hs.get('tasks_db_size') else 'N/A'}")
        for key, val in hs.items():
            if key not in ("tasks_db_size","tables","error"):
                lines.append(f"  {key}: {val}")
        if "tables" in hs:
            lines.append(f"  tables: {hs['tables']}")
        if "error" in hs:
            lines.append(f"  ERROR: {hs['error']}")
    if "error" in health:
        lines.append(f"\n  ERROR: {health['error']}")
    lines.append("\n" + "="*60)
    return "\n".join(lines)

def main():
    import argparse
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="SSRL-ESP DB Health Check")
    parser.add_argument("--db-path", default=os.path.join(project_root,"ssrl_esp.db"))
    parser.add_argument("--tasks-db-path", default=os.path.join(project_root,"data","tasks.db"))
    args = parser.parse_args()
    health = check_health(args.db_path, args.tasks_db_path)
    print(format_report(health, verbose=args.verbose if hasattr(args,'verbose') else False))

if __name__ == "__main__":
    main()
