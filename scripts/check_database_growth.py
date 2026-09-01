#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Database growth measurement utility for SSRL-ESP.

Records database file size and key table row counts at named measurement points.
Usage:
    from scripts.check_database_growth import DbGrowthTracker
    tracker = DbGrowthTracker("/path/to/db")
    tracker.measure("before test")
    # ... do things ...
    tracker.measure("after login")
    tracker.report()
"""

import os
import sqlite3
from datetime import datetime


KEY_TABLES = [
    "users",
    "groups",
    "group_members",
    "experiment_participants",
    "teacher_access_keys",
    "client_sessions",
    "messages",
    "emotion_checkins",
    "help_requests",
    "collaborative_documents",
    "collaborative_document_checkpoints",
    "submissions",
]


class DbGrowthTracker:
    """Measure and record database size and row counts at named points."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.points = []  # list of (label, timestamp, file_size, {table: row_count})

    def measure(self, label):
        """Take a measurement at the current state."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        row_counts = self._get_row_counts()
        self.points.append((label, timestamp, file_size, row_counts))
        print(f"[DbGrowth] {label}: DB={self._fmt_size(file_size)}, tables={row_counts}")
        return file_size, row_counts

    def _get_row_counts(self):
        """Query row counts for all key tables."""
        counts = {}
        try:
            conn = sqlite3.connect(self.db_path, timeout=3)
            conn.row_factory = sqlite3.Row
            for table in KEY_TABLES:
                try:
                    row = conn.execute("SELECT COUNT(*) AS cnt FROM [" + table + "]").fetchone()
                    counts[table] = row["cnt"] if row else 0
                except Exception:
                    counts[table] = -1  # table may not exist yet
            conn.close()
        except Exception as e:
            print("[DbGrowth] WARNING: cannot query db: " + str(e))
            for table in KEY_TABLES:
                counts[table] = -2
        return counts

    @staticmethod
    def _fmt_size(bytes_val):
        """Format bytes to human-readable string."""
        if bytes_val < 1024:
            return str(bytes_val) + " B"
        elif bytes_val < 1024 * 1024:
            return "{:.1f} KB".format(bytes_val / 1024)
        else:
            return "{:.1f} MB".format(bytes_val / (1024 * 1024))

    def report(self):
        """Print a formatted growth report."""
        if len(self.points) < 2:
            print("[DbGrowth] Need at least 2 measurement points for a report.")
            return

        print("")
        print("=" * 78)
        print("  DATABASE GROWTH REPORT")
        print("=" * 78)

        # Header
        header = "{:<30} {:<12} {:<12}".format("Point", "DB Size", "Delta")
        for t in KEY_TABLES:
            header += " {:<20}".format(t)
        print(header)
        print("-" * len(header))

        prev_size = None
        prev_counts = None
        for label, ts, fsize, counts in self.points:
            delta = ""
            if prev_size is not None:
                d = fsize - prev_size
                delta = "+{}".format(d) if d >= 0 else str(d)
            size_str = self._fmt_size(fsize)
            row = "{:<30} {:<12} {:<12}".format(label, size_str, delta)
            for t in KEY_TABLES:
                c = counts.get(t, -3)
                if prev_counts and c >= 0 and prev_counts.get(t, -1) >= 0:
                    diff = c - prev_counts.get(t, 0)
                    if diff != 0:
                        row += " {} ({:+d})".format(c, diff).ljust(20)
                    else:
                        row += " {}".format(c).ljust(20)
                else:
                    row += " {}".format(c).ljust(20)
            print(row)
            prev_size = fsize
            prev_counts = counts

        print("-" * len(header))

        # Summary
        first_size = self.points[0][2]
        last_size = self.points[-1][2]
        total_growth = last_size - first_size
        print("")
        print("Total DB growth: {} -> {} (+{})".format(
            self._fmt_size(first_size), self._fmt_size(last_size), self._fmt_size(total_growth)))

        first_counts = self.points[0][3]
        last_counts = self.points[-1][3]
        print("")
        print("Table row growth:")
        for t in KEY_TABLES:
            fc = first_counts.get(t, 0)
            lc = last_counts.get(t, 0)
            if lc != fc:
                print("  {:<40} {} -> {} ({:+d})".format(t, fc, lc, lc - fc))
        print("=" * 78)

    def check_anomalies(self):
        """Check for common database growth anomalies. Returns list of warning strings."""
        warnings = []
        if len(self.points) < 2:
            return warnings

        first_counts = self.points[0][3]
        last_counts = self.points[-1][3]

        # 1. groups should not grow beyond expected
        groups_first = first_counts.get("groups", 0)
        groups_last = last_counts.get("groups", 0)
        groups_growth = groups_last - groups_first
        if groups_growth > 0:
            warnings.append("ANOMALY: groups grew by {} (expected 0)".format(groups_growth))
        if groups_last > 20:
            warnings.append("NOTE: groups count is {} (may include pre-existing data)".format(groups_last))

        # 2. experiment_participants should not grow during test
        ep_growth = last_counts.get("experiment_participants", 0) - first_counts.get("experiment_participants", 0)
        if ep_growth > 0:
            warnings.append("ANOMALY: experiment_participants grew by {} (expected 0)".format(ep_growth))

        # 3. users should not grow from repeat logins
        users_growth = last_counts.get("users", 0) - first_counts.get("users", 0)
        if users_growth > 0:
            warnings.append("ANOMALY: users grew by {} (expected 0 during login)".format(users_growth))

        # 4. group_members should not grow from repeat logins
        gm_growth = last_counts.get("group_members", 0) - first_counts.get("group_members", 0)
        if gm_growth > 0:
            warnings.append("ANOMALY: group_members grew by {} (expected 0)".format(gm_growth))

        # 5. DB size should not grow excessively
        first_size = self.points[0][2]
        last_size = self.points[-1][2]
        growth_bytes = last_size - first_size
        if growth_bytes > 10 * 1024 * 1024:  # 10 MB
            warnings.append("ANOMALY: DB grew by {} (excessive)".format(self._fmt_size(growth_bytes)))

        return warnings


def main():
    """CLI entry point: measure a single database and print report."""
    import sys
    if len(sys.argv) < 2:
        print("Usage: python scripts/check_database_growth.py <db_path>")
        sys.exit(1)
    db_path = sys.argv[1]
    tracker = DbGrowthTracker(db_path)
    tracker.measure("snapshot")
    tracker.report()
    anomalies = tracker.check_anomalies()
    if anomalies:
        print("")
        print("Anomalies found:")
        for w in anomalies:
            print("  ! " + w)


if __name__ == "__main__":
    main()
