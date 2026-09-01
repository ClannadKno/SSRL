# -*- coding: utf-8 -*-
"""
Huey tasks for periodic maintenance (DB vacuum, stale-data cleanup, etc.).

Each task pushes its own Flask application context and creates an
independent database session.
"""
from huey_instance import huey
from core import app


@huey.task()
def maintenance_smoke():
    """Smoke test for maintenance_tasks module."""
    with app.app_context():
        return "maintenance_smoke ok"


@huey.task()
def check_maintenance_db():
    """Business database connectivity check for maintenance context."""
    with app.app_context():
        from db import db
        conn = db()
        try:
            conn.execute("SELECT 1").fetchone()
            conn.commit()
        finally:
            conn.close()
        return {"module": "maintenance", "db_ok": True}
