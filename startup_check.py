import os
import sys

import config
from db import db, query_one


def check_app_db():
    """Check app.db (business DB) is readable and writable."""
    path = config.DB_PATH
    if not os.path.exists(path):
        print(f"[startup-check] ERROR: app.db not found: {path}")
        return False
    try:
        row = query_one("SELECT 1 AS ok")
        assert row and int(row["ok"]) == 1
        print(f"[startup-check] app.db OK ({path})")
        return True
    except Exception as e:
        print(f"[startup-check] ERROR: app.db read/write failed: {e}")
        return False


def check_tasks_db():
    """Check tasks.db (Huey queue DB) is readable and writable."""
    path = config.HUEY_DB_PATH
    if not os.path.exists(path):
        print(f"[startup-check] WARNING: tasks.db not yet created (auto-created on first use): {path}")
    try:
        import sqlite3
        conn = sqlite3.connect(path, timeout=3)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        print(f"[startup-check] tasks.db OK ({path})")
        return True
    except Exception as e:
        print(f"[startup-check] ERROR: tasks.db read/write failed: {e}")
        return False


def check_huey_config():
    """Check Huey configuration."""
    if not config.HUEY_ENABLED:
        print("[startup-check] WARNING: HUEY_ENABLED=False - background task queue disabled")
        return False
    if not config.HUEY_DB_PATH:
        print("[startup-check] ERROR: HUEY_DB_PATH not configured")
        return False
    print(
        "[startup-check] Huey config OK "
        f"(workers={config.HUEY_WORKERS}, immediate={config.HUEY_IMMEDIATE}, "
        f"sqlite_timeout={config.HUEY_SQLITE_TIMEOUT_SECONDS}s)"
    )
    return True


def check_llm_config():
    """Check LLM profile configuration."""
    if not config.SERA_LLM_ENABLED and not config.USE_LLM_ANALYSIS:
        print("[startup-check] WARNING: LLM disabled (SERA_LLM_ENABLED=false)")
        return True
    api_key = config.SERA_LLM_API_KEY
    base_url = config.SERA_LLM_BASE_URL
    model = config.SERA_LLM_MODEL
    if not api_key:
        print("[startup-check] WARNING: LLM enabled but SERA_LLM_API_KEY not set (V2 will use fallback template)")
    if not base_url:
        print("[startup-check] ERROR: SERA_LLM_BASE_URL not configured")
        return False
    print(f"[startup-check] LLM config OK (model={model}, url={base_url[:60]}...)")
    return True


def run_all_checks():
    """Run all startup checks. Returns True if all pass."""
    print("=" * 48)
    print("  SSRL-ESP Startup Self-Check (Batch 8)")
    print("=" * 48)
    checks = [
        ("app.db read/write", check_app_db()),
        ("tasks.db read/write", check_tasks_db()),
        ("Huey config", check_huey_config()),
        ("LLM config", check_llm_config()),
    ]
    all_ok = True
    critical_failures = []
    for name, ok in checks:
        if not ok:
            all_ok = False
            critical_failures.append(name)
    if all_ok:
        print("[startup-check] All checks passed!")
    else:
        print(f"[startup-check] FAILED: {', '.join(critical_failures)}")
        print("[startup-check] Please fix issues before restarting service")
    print("=" * 48)
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if run_all_checks() else 1)
