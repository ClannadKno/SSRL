# -*- coding: utf-8 -*-
"""Collaborative database monitoring module (Batch 3).

Read-only health checks for the SSRL database with focus on:
- Database file size, WAL size, SHM size
- Snapshot table row counts and per-room stats
- Idle room write detection
- Content anomaly detection (Base64 in text)
- Same-state repetition detection
- Snapshot size tracking
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


# Threshold values
_CONTENT_BASE64_WARN_CHARS = int(os.environ.get("COLLAB_BASE64_WARN_CHARS", "500"))
_CONTENT_BASE64_BLOCK_CHARS = int(os.environ.get("COLLAB_BASE64_BLOCK_CHARS", "5000"))
_MAX_SNAPSHOT_BYTES = int(os.environ.get("COLLAB_MAX_SNAPSHOT_BYTES", "1048576"))
_MAX_IDLE_WRITES_PER_HOUR = int(os.environ.get("COLLAB_MAX_IDLE_WRITES", "60"))
_MAX_SAME_STATE_REPEATS = int(os.environ.get("COLLAB_MAX_SAME_STATE", "10"))
_WAL_GROWTH_ALERT_MB = int(os.environ.get("COLLAB_WAL_ALERT_MB", "500"))


def get_db_path():
    return os.environ.get("SSRL_ESP_DB_PATH",
                          os.path.join(os.path.dirname(__file__), "..", "ssrl_esp.db"))


def _connect(db_path=None):
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def check_file_sizes(db_path=None):
    path = db_path or get_db_path()
    result = {}
    for suffix, label in [("", "db"), ("-wal", "wal"), ("-shm", "shm")]:
        fpath = path + suffix
        size = os.path.getsize(fpath) if os.path.exists(fpath) else 0
        result[label] = size
        result[label + "_fmt"] = fmt_size(size)
    return result


def check_snapshot_stats(db_path=None):
    conn = _connect(db_path)
    try:
        doc_count = conn.execute("SELECT COUNT(*) AS c FROM collaborative_documents").fetchone()[0]
        cp_count = conn.execute("SELECT COUNT(*) AS c FROM collaborative_document_checkpoints").fetchone()[0]
        total_y_state = conn.execute("SELECT COALESCE(SUM(LENGTH(y_state)),0) FROM collaborative_documents").fetchone()[0]
        max_y_state = conn.execute("SELECT COALESCE(MAX(LENGTH(y_state)),0) FROM collaborative_documents").fetchone()[0]
        max_text = conn.execute("SELECT COALESCE(MAX(LENGTH(content_text)),0) FROM collaborative_documents").fetchone()[0]
        max_html = conn.execute("SELECT COALESCE(MAX(LENGTH(content_html)),0) FROM collaborative_documents").fetchone()[0]
        total_revision = conn.execute("SELECT COALESCE(SUM(state_revision),0) FROM collaborative_documents").fetchone()[0]
        return {
            "doc_count": doc_count,
            "checkpoint_count": cp_count,
            "total_y_state_bytes": total_y_state,
            "max_y_state_bytes": max_y_state,
            "max_text_chars": max_text,
            "max_html_chars": max_html,
            "total_revision": total_revision,
        }
    finally:
        conn.close()


def check_per_room_stats(db_path=None):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, group_id, state_revision, LENGTH(y_state) AS y_len, "
            "LENGTH(content_text) AS text_len, LENGTH(content_html) AS html_len, "
            "state_size_bytes, y_state_hash, status, updated_at "
            "FROM collaborative_documents ORDER BY id"
        ).fetchall()
        rooms = {}
        for r in rows:
            rooms[str(r["id"])] = {
                "group_id": r["group_id"],
                "state_revision": r["state_revision"],
                "y_state_bytes": r["y_len"] or 0,
                "text_chars": r["text_len"] or 0,
                "html_chars": r["html_len"] or 0,
                "state_size_bytes": r["state_size_bytes"],
                "y_state_hash_prefix": (r["y_state_hash"] or "")[:12],
                "status": r["status"],
                "updated_at": r["updated_at"],
            }
        return rooms
    finally:
        conn.close()


def check_duplicate_hash_rows(db_path=None):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT y_state_hash, COUNT(*) AS cnt FROM collaborative_documents "
            "WHERE y_state_hash IS NOT NULL AND y_state_hash !=  "
            "GROUP BY y_state_hash HAVING cnt > 1"
        ).fetchall()
        result = {}
        for r in rows:
            result[r["y_state_hash"][:12]] = r["cnt"]
        return result
    finally:
        conn.close()


def _find_base64_candidates(text, min_len=100):
    candidates = []
    for match in re.finditer(r"[A-Za-z0-9+/=]{100,}", text):
        length = len(match.group())
        if length >= min_len:
            candidates.append({
                "length": length,
                "prefix": match.group()[:30],
            })
    return candidates


def check_content_anomalies(db_path=None):
    conn = _connect(db_path)
    anomalies = []
    try:
        rows = conn.execute(
            "SELECT id, group_id, LENGTH(content_text) AS text_len, "
            "LENGTH(content_html) AS html_len, content_text "
            "FROM collaborative_documents "
            "WHERE content_text IS NOT NULL AND content_text != "
        ).fetchall()
        for r in rows:
            text = r["content_text"] or ""
            base64_candidates = _find_base64_candidates(text)
            for bc in base64_candidates:
                anomalies.append({
                    "type": "base64_in_text",
                    "document_id": r["id"],
                    "length": bc["length"],
                    "prefix": bc["prefix"],
                })
            if r["text_len"] > 100000:
                anomalies.append({
                    "type": "oversized_text",
                    "document_id": r["id"],
                    "chars": r["text_len"],
                })
            if r["html_len"] > 500000:
                anomalies.append({
                    "type": "oversized_html",
                    "document_id": r["id"],
                    "chars": r["html_len"],
                })
        return anomalies
    finally:
        conn.close()


def check_wal_growth(db_path=None, previous_wal_size=0):
    path = db_path or get_db_path()
    wal_path = path + "-wal"
    current = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    growth = current - previous_wal_size
    alert = None
    if growth > _WAL_GROWTH_ALERT_MB * 1024 * 1024:
        alert = "WAL grew by %s since last check" % fmt_size(growth)
    return {
        "current_size": current,
        "current_fmt": fmt_size(current),
        "growth": growth,
        "growth_fmt": fmt_size(growth),
        "alert": alert,
    }


def full_report(db_path=None, previous_wal_size=0):
    report = {
        "timestamp": datetime.now().isoformat(),
        "file_sizes": check_file_sizes(db_path),
        "snapshot_stats": check_snapshot_stats(db_path),
        "per_room": check_per_room_stats(db_path),
        "duplicate_hashes": check_duplicate_hash_rows(db_path),
        "content_anomalies": check_content_anomalies(db_path),
        "wal": check_wal_growth(db_path, previous_wal_size),
        "alerts": [],
    }
    ss = report["snapshot_stats"]
    if ss["max_y_state_bytes"] > _MAX_SNAPSHOT_BYTES:
        report["alerts"].append({
            "type": "oversized_snapshot",
            "detail": "Max y_state: %s bytes" % ss["max_y_state_bytes"],
        })
    for anom in report["content_anomalies"]:
        if anom["type"] == "base64_in_text" and anom["length"] > _CONTENT_BASE64_BLOCK_CHARS:
            report["alerts"].append({
                "type": "base64_anomaly",
                "detail": "Doc %s has Base64-like content: %d chars" % (anom["document_id"], anom["length"]),
            })
    if report["wal"].get("alert"):
        report["alerts"].append(report["wal"]["alert"])
    return report


def format_report(report):
    lines = []
    sep = "=" * 60
    lines.append(sep)
    lines.append("  Collaborative DB Monitor  (%s)" % report["timestamp"])
    lines.append(sep)
    lines.append("")
    lines.append("--- File Sizes ---")
    for key in ("db", "wal", "shm"):
        if key in report.get("file_sizes", {}):
            lines.append("  %s: %s" % (key, report["file_sizes"].get(key + "_fmt", "")))
    lines.append("")
    lines.append("--- Snapshot Stats ---")
    for key, val in report.get("snapshot_stats", {}).items():
        if isinstance(val, int):
            label = key
            if "byte" in key:
                label = fmt_size(val)
            lines.append("  %s: %s" % (key, label))
    lines.append("")
    lines.append("--- WAL Growth ---")
    wal = report.get("wal", {})
    lines.append("  current: %s" % wal.get("current_fmt", "N/A"))
    lines.append("  growth:  %s" % wal.get("growth_fmt", "N/A"))
    lines.append("")
    lines.append("--- Alerts ---")
    if report.get("alerts"):
        for a in report["alerts"]:
            lines.append("  [!] %s: %s" % (a["type"], a["detail"]))
    else:
        lines.append("  None")
    lines.append("")
    lines.append("--- Per-Room Summary ---")
    for doc_id, info in sorted(report.get("per_room", {}).items()):
        lines.append("  doc %s: rev=%s, y_state=%s bytes, hash=%s, status=%s" % (
            doc_id, info["state_revision"], info["y_state_bytes"],
            info["y_state_hash_prefix"], info["status"],
        ))
    lines.append("")
    lines.append("--- Content Anomalies ---")
    if report.get("content_anomalies"):
        for a in report["content_anomalies"]:
            lines.append("  [%s] doc=%s: %s" % (a["type"], a["document_id"], a.get("chars", a.get("length"))))
    else:
        lines.append("  None")
    lines.append(sep)
    return "".join(lines)
