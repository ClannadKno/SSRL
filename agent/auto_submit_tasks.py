# -*- coding: utf-8 -*-
"""Huey tasks for auto-submit scanning (Batch 5)."""
import logging
from huey import crontab
from huey_instance import huey
from core import app
logger = logging.getLogger(__name__)
AUTO_SUBMIT_SCAN_MAX_DOCS = 100


@huey.periodic_task(crontab(minute='*'))
def auto_submit_scan():
    """Periodic scan for expired group discussion runtimes."""
    with app.app_context():
        try:
            from services.auto_submit_service import scan_and_auto_submit
            result = scan_and_auto_submit(max_docs=AUTO_SUBMIT_SCAN_MAX_DOCS)
            if result.get("submitted", 0) > 0 or result.get("errors", 0) > 0:
                logger.info(
                    "auto_submit_scan: %d group discussions, %d docs, %d submitted, %d skipped, %d errors",
                    result.get("scanned_group_discussions", 0),
                    result.get("docs_processed", 0),
                    result.get("submitted", 0),
                    result.get("skipped", 0),
                    result.get("errors", 0),
                )
            return result
        except Exception as exc:
            logger.exception("auto_submit_scan failed: %s", exc)
            return {"ok": False, "error": str(exc)}


@huey.task()
def auto_submit_smoke():
    """Smoke test for auto_submit_tasks module."""
    with app.app_context():
        return "auto_submit_smoke ok"


@huey.task()
def check_auto_submit_db():
    """Quick DB connectivity check."""
    with app.app_context():
        from db import db
        conn = db()
        try:
            conn.execute("SELECT 1").fetchone()
            conn.commit()
        finally:
            conn.close()
        return {"module": "auto_submit", "db_ok": True}


@huey.task()
def auto_submit_single_document(document_id: int, session_id: int = None):
    """Manually trigger auto-submit for a single document."""
    with app.app_context():
        try:
            from services.auto_submit_service import perform_auto_submit
            from db import query_one
            doc = query_one(
                "SELECT group_id, task_id, session_no, session_id FROM collaborative_documents WHERE id=?", (document_id,)
            )
            if not doc:
                return {"ok": False, "error": "document_not_found"}
            runtime = query_one(
                """
                SELECT
                    gsd.id AS group_discussion_id,
                    gsd.session_id,
                    gsd.group_id,
                    gsd.deadline,
                    es.session_no,
                    es.task_id
                FROM group_session_discussions gsd
                JOIN experiment_sessions es ON es.id = gsd.session_id
                WHERE gsd.group_id=?
                  AND es.status='running'
                  AND (gsd.session_id=? OR (? IS NULL AND es.task_id=? AND es.session_no=?))
                ORDER BY gsd.id DESC LIMIT 1
                """,
                (
                    doc["group_id"],
                    session_id or doc["session_id"],
                    session_id or doc["session_id"],
                    doc["task_id"],
                    doc["session_no"],
                ),
            )
            if not runtime:
                return {"ok": False, "error": "no_group_discussion_runtime_for_document"}
            result = perform_auto_submit(document_id, dict(runtime))
            return result
        except Exception as exc:
            logger.exception("auto_submit_single_document(%s) failed: %s", document_id, exc)
            return {"ok": False, "error": str(exc)}
