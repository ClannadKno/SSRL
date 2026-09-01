# -*- coding: utf-8 -*-
"""Backend auto-submit service (Batch 5)."""
from __future__ import annotations
import json
import logging
import uuid
from datetime import datetime
from db import db, execute, now_str, parse_dt, query_one, query_all, record_process_event

logger = logging.getLogger(__name__)

_AGENT_USER_ID_CACHE = None


def _get_agent_user_id():
    global _AGENT_USER_ID_CACHE
    if _AGENT_USER_ID_CACHE is not None:
        return _AGENT_USER_ID_CACHE
    row = query_one("SELECT id FROM users WHERE role='agent' ORDER BY id ASC LIMIT 1")
    _AGENT_USER_ID_CACHE = int(row["id"]) if row else 0
    return _AGENT_USER_ID_CACHE


def _deadline_reached(deadline, now_dt=None):
    deadline_dt = parse_dt(deadline) if isinstance(deadline, str) else deadline
    if not deadline_dt:
        return False
    return (now_dt or datetime.now()) >= deadline_dt


def _mark_group_discussion(group_discussion_id, status, now):
    if not group_discussion_id:
        return
    if status == "submitted":
        execute(
            """
            UPDATE group_session_discussions
            SET status='submitted',
                submitted_at=COALESCE(submitted_at, ?),
                auto_submitted_at=COALESCE(auto_submitted_at, ?),
                updated_at=?
            WHERE id=? AND status IN ('running','timed_out')
            """,
            (now, now, now, group_discussion_id),
        )
    elif status == "timed_out":
        execute(
            """
            UPDATE group_session_discussions
            SET status='timed_out', updated_at=?
            WHERE id=? AND status='running'
            """,
            (now, group_discussion_id),
        )


def _has_submitted_group_document(session_id, group_id, task_id, session_no):
    row = query_one(
        """
        SELECT id FROM collaborative_documents
        WHERE group_id=? AND status='submitted'
          AND (
            session_id=?
            OR (session_id IS NULL AND task_id=? AND session_no=?)
          )
        ORDER BY id DESC LIMIT 1
        """,
        (group_id, session_id, task_id, session_no),
    )
    return bool(row)


def _final_content(doc, final_content):
    """Return a validated display snapshot and its provenance."""
    content = {
        "content_json": doc.get("content_json") or "",
        "content_html": doc.get("content_html") or "",
        "content_text": doc.get("content_text") or "",
    }
    if not isinstance(final_content, dict):
        return content, "stored_snapshot"
    supplied = any(key in final_content for key in content)
    if not supplied:
        return content, "stored_snapshot"
    candidate = {
        key: final_content.get(key, content[key]) or ""
        for key in content
    }
    try:
        from services.collaborative_service import (
            sanitize_html_content,
            sanitize_json_content,
            validate_content,
        )

        validate_content(
            candidate["content_text"],
            candidate["content_html"],
            candidate["content_json"],
        )
        candidate["content_html"] = sanitize_html_content(candidate["content_html"])
        candidate["content_json"] = sanitize_json_content(candidate["content_json"])
        return candidate, "client_final_snapshot"
    except Exception as exc:
        logger.warning("forced_submit ignored invalid final snapshot: %s", exc)
        return content, "stored_snapshot"


def _release_room_after_failed_submission(document_id, freeze_ok, *, submitted=False):
    if not freeze_ok:
        return
    try:
        if submitted:
            from services.collaborative_internal import close_document

            close_document(document_id)
        else:
            from services.collaborative_internal import unfreeze_document

            unfreeze_document(document_id)
    except Exception as exc:
        logger.warning("forced_submit[%s]: failed-room cleanup error: %s", document_id, exc)


def _perform_forced_submission(
    document_id,
    session_ctx,
    *,
    submission_source,
    submit_reason,
    event_type,
    finalization_reason,
    allowed_statuses,
    final_content=None,
    actor_user_id=None,
):
    """Freeze, flush and atomically persist one terminal document snapshot."""
    doc_row = query_one(
        "SELECT id, session_id, group_id, task_id, session_no, status, state_revision, "
        "content_json, content_html, content_text, y_state, created_by "
        "FROM collaborative_documents WHERE id=?",
        (document_id,),
    )
    if not doc_row:
        return {"ok": False, "document_id": document_id, "reason": "not_found"}
    doc = dict(doc_row)
    if doc["status"] == "submitted":
        return {"ok": False, "document_id": document_id, "reason": "already_submitted"}
    if doc["status"] not in set(allowed_statuses):
        return {"ok": False, "document_id": document_id, "reason": "invalid_status"}

    ctx_session_id = session_ctx.get("session_id") or session_ctx.get("id")
    if doc.get("session_id") and ctx_session_id and int(doc["session_id"]) != int(ctx_session_id):
        return {"ok": False, "document_id": document_id, "reason": "session_mismatch"}

    group_id = doc["group_id"]
    task_id = doc["task_id"] or session_ctx.get("task_id")
    session_no = doc["session_no"] or session_ctx.get("session_no")
    freeze_ok = False
    flush_ok = False
    final_state_revision = int(doc.get("state_revision") or 0)

    try:
        from services.collaborative_internal import freeze_document

        freeze_result = freeze_document(document_id)
        freeze_ok = bool(freeze_result.get("ok"))
        if not freeze_ok:
            logger.info("forced_submit[%s]: room freeze unavailable: %s", document_id, freeze_result.get("error"))
    except Exception as exc:
        logger.warning("forced_submit[%s]: freeze exception: %s", document_id, exc)

    try:
        from services.collaborative_internal import flush_document

        for attempt in range(2):
            flush_result = flush_document(document_id)
            if flush_result.get("ok"):
                flush_ok = True
                final_state_revision = int(
                    flush_result.get("state_revision", final_state_revision) or final_state_revision
                )
                break
            if attempt == 0:
                logger.info("forced_submit[%s]: flush retry", document_id)
    except Exception as exc:
        logger.warning("forced_submit[%s]: flush exception: %s", document_id, exc)

    # Reload after the collaboration server flushes its latest Yjs state.
    refreshed = query_one(
        "SELECT id, session_id, group_id, task_id, session_no, status, state_revision, "
        "content_json, content_html, content_text, y_state, created_by "
        "FROM collaborative_documents WHERE id=?",
        (document_id,),
    )
    if refreshed:
        doc = dict(refreshed)
        final_state_revision = int(doc.get("state_revision") or final_state_revision)
    content, content_source = _final_content(doc, final_content)
    now = now_str()
    freeze_id = uuid.uuid4().hex
    created_by = int(actor_user_id or _get_agent_user_id() or doc.get("created_by") or 0)
    timeout_at = now if submission_source == "auto_timeout" else None
    sid = None

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status, session_id, state_revision, y_state FROM collaborative_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if not current or current["status"] not in set(allowed_statuses):
            conn.rollback()
            reason = "already_submitted" if current and current["status"] == "submitted" else "invalid_status"
            _release_room_after_failed_submission(
                document_id,
                freeze_ok,
                submitted=reason == "already_submitted",
            )
            return {"ok": False, "document_id": document_id, "reason": reason}
        cur = conn.execute(
            "UPDATE collaborative_documents "
            "SET session_id=COALESCE(session_id, ?), status='submitted', "
            "content_json=?, content_html=?, content_text=?, "
            "submitted_at=COALESCE(submitted_at, ?), updated_at=? "
            "WHERE id=? AND status=?",
            (
                ctx_session_id,
                content["content_json"],
                content["content_html"],
                content["content_text"],
                now,
                now,
                document_id,
                current["status"],
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            _release_room_after_failed_submission(document_id, freeze_ok, submitted=True)
            return {"ok": False, "document_id": document_id, "reason": "already_submitted"}
        final_state_revision = int(current["state_revision"] or final_state_revision)
        conn.execute(
            "INSERT INTO submission_prepares(document_id, freeze_id, state_revision, committed, created_by, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (document_id, freeze_id, final_state_revision, 1, created_by, now),
        )
        conn.execute(
            "INSERT INTO collaborative_document_checkpoints "
            "(document_id, state_revision, reason, y_state, content_json, content_html, content_text, created_by, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                final_state_revision,
                "submitted",
                current["y_state"],
                content["content_json"],
                content["content_html"],
                content["content_text"],
                created_by,
                now,
            ),
        )
        submission = conn.execute(
            "INSERT INTO submissions "
            "(group_id, user_id, task_id, session_no, content, file_name, stored_file_name, "
            "file_path, file_size, submission_mode, submitted_by, submission_source, "
            "submit_reason, timeout_at, submitted_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                group_id,
                created_by,
                task_id,
                session_no,
                content["content_text"],
                None,
                None,
                None,
                0,
                "text",
                submission_source,
                submission_source,
                submit_reason,
                timeout_at,
                now,
                now,
            ),
        )
        sid = submission.lastrowid
        conn.commit()
    except Exception as exc:
        conn.rollback()
        _release_room_after_failed_submission(document_id, freeze_ok)
        logger.exception("forced_submit[%s]: atomic persistence failed", document_id)
        return {"ok": False, "document_id": document_id, "reason": "claim_error", "error": str(exc)}
    finally:
        conn.close()

    try:
        payload = {
            "document_id": document_id,
            "session_id": ctx_session_id,
            "group_discussion_id": session_ctx.get("group_discussion_id"),
            "task_id": task_id,
            "group_id": group_id,
            "deadline": session_ctx.get("deadline"),
            "submitted_at": now,
            "state_revision": final_state_revision,
            "freeze_ok": freeze_ok,
            "flush_ok": flush_ok,
            "submission_id": sid,
            "submission_source": submission_source,
            "content_source": content_source,
        }
        if not flush_ok:
            payload["flush_warning"] = "flush failed, used last persisted Yjs revision"
        record_process_event(
            event_type=event_type,
            source="system",
            group_id=group_id,
            session_no=session_no,
            task_id=task_id,
            related_table="submissions",
            related_id=sid,
            payload=payload,
        )
    except Exception as exc:
        logger.warning("forced_submit[%s]: process event failed: %s", document_id, exc)

    try:
        from services.collaborative_internal import close_document

        close_document(document_id)
    except Exception as exc:
        logger.warning("forced_submit[%s]: close exception: %s", document_id, exc)

    try:
        from services.collaboration_state_finalization_service import safe_request_collaboration_state_finalization

        safe_request_collaboration_state_finalization(group_id, ctx_session_id, finalization_reason)
    except Exception as exc:
        logger.warning("forced_submit[%s]: state finalization request failed: %s", document_id, exc)

    if submission_source == "auto_timeout" and session_ctx.get("group_discussion_id"):
        _mark_group_discussion(session_ctx["group_discussion_id"], "submitted", now)

    return {
        "ok": True,
        "document_id": document_id,
        "state_revision": final_state_revision,
        "submission_id": sid,
        "freeze_ok": freeze_ok,
        "flush_ok": flush_ok,
        "content_source": content_source,
        "submission_source": submission_source,
    }


def perform_auto_submit(document_id, session_ctx, final_content=None):
    """Submit one document after its group discussion deadline."""
    deadline_str = session_ctx.get("deadline")
    if not deadline_str:
        return {"ok": False, "document_id": document_id, "reason": "no_deadline"}
    deadline_dt = parse_dt(deadline_str) if isinstance(deadline_str, str) else deadline_str
    if not deadline_dt:
        return {"ok": False, "document_id": document_id, "reason": "invalid_deadline"}
    if not _deadline_reached(deadline_dt):
        return {"ok": False, "document_id": document_id, "reason": "deadline_not_reached"}
    return _perform_forced_submission(
        document_id,
        session_ctx,
        submission_source="auto_timeout",
        submit_reason="任务截止时间已到，系统自动提交",
        event_type="auto_timeout_submission",
        finalization_reason="timeout_auto_submit",
        allowed_statuses={"editing"},
        final_content=final_content,
    )


def submit_session_documents(session_id, operator_id=None):
    """Submit every unfinished collaborative document before a teacher ends a session."""
    session = query_one(
        "SELECT id, session_no, task_id, status FROM experiment_sessions WHERE id=?",
        (session_id,),
    )
    if not session:
        return {"ok": False, "session_id": session_id, "reason": "session_not_found"}
    session_ctx = dict(session)
    session_ctx["session_id"] = session_ctx["id"]
    documents = query_all(
        "SELECT id, group_id FROM collaborative_documents "
        "WHERE status IN ('editing','returned','locked') "
        "AND (session_id=? OR (session_id IS NULL AND task_id=? AND session_no=?)) "
        "ORDER BY group_id, id",
        (session_id, session_ctx.get("task_id"), session_ctx.get("session_no")),
    )
    results = []
    submitted_groups = set()
    for document in documents:
        result = _perform_forced_submission(
            document["id"],
            session_ctx,
            submission_source="teacher_end",
            submit_reason="教师结束课次，系统自动提交协作成果",
            event_type="teacher_end_submission",
            finalization_reason="teacher_close",
            allowed_statuses={"editing", "returned", "locked"},
            actor_user_id=operator_id,
        )
        results.append(result)
        if result.get("ok") or result.get("reason") == "already_submitted":
            submitted_groups.add(int(document["group_id"]))

    now = now_str()
    runtimes = query_all(
        "SELECT id, group_id FROM group_session_discussions WHERE session_id=?",
        (session_id,),
    )
    for runtime in runtimes:
        if int(runtime["group_id"]) in submitted_groups or _has_submitted_group_document(
            session_id,
            runtime["group_id"],
            session_ctx.get("task_id"),
            session_ctx.get("session_no"),
        ):
            execute(
                "UPDATE group_session_discussions SET status='submitted', "
                "submitted_at=COALESCE(submitted_at, ?), updated_at=? WHERE id=?",
                (now, now, runtime["id"]),
            )
        else:
            execute(
                "UPDATE group_session_discussions SET status='closed', updated_at=? "
                "WHERE id=? AND status IN ('waiting','running','timed_out')",
                (now, runtime["id"]),
            )

    submitted = sum(1 for result in results if result.get("ok"))
    errors = sum(
        1
        for result in results
        if not result.get("ok") and result.get("reason") != "already_submitted"
    )
    return {
        "ok": errors == 0,
        "session_id": session_id,
        "documents_found": len(documents),
        "submitted": submitted,
        "errors": errors,
        "details": results,
    }


def scan_and_auto_submit(max_docs=100):
    """Scan expired group discussion runtimes and auto-submit their documents."""
    now = now_str()
    now_dt = datetime.now()
    results = []
    group_discussions = query_all(
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
        WHERE gsd.status='running'
          AND gsd.deadline IS NOT NULL
          AND gsd.deadline <= ?
          AND es.status='running'
          AND es.task_id IS NOT NULL
        ORDER BY gsd.deadline ASC, gsd.id ASC
        LIMIT ?
        """,
        (now, max(1, int(max_docs))),
    )
    total_submitted = 0
    total_skipped = 0
    total_errors = 0
    docs_processed = 0

    for runtime in group_discussions:
        runtime_ctx = dict(runtime)
        if not _deadline_reached(runtime_ctx.get("deadline"), now_dt):
            continue
        docs = query_all(
            "SELECT cd.id, cd.session_id, cd.group_id, cd.status, cd.state_revision "
            "FROM collaborative_documents cd "
            "WHERE cd.group_id=? AND cd.status='editing' "
            "AND (cd.session_id=? OR (cd.session_id IS NULL AND cd.task_id=? AND cd.session_no=?)) "
            "ORDER BY cd.id ASC LIMIT ?",
            (
                runtime_ctx["group_id"],
                runtime_ctx["session_id"],
                runtime_ctx["task_id"],
                runtime_ctx["session_no"],
                max_docs - docs_processed,
            ),
        )
        group_submitted = 0
        group_errors = 0
        for doc in docs:
            if docs_processed >= max_docs:
                break
            result = perform_auto_submit(doc["id"], runtime_ctx)
            docs_processed += 1
            summary = {
                "document_id": doc["id"],
                "group_id": doc["group_id"],
                "session_id": runtime_ctx["session_id"],
                "group_discussion_id": runtime_ctx["group_discussion_id"],
            }
            if result.get("ok"):
                summary["status"] = "submitted"
                group_submitted += 1
                total_submitted += 1
            elif result.get("reason") == "already_submitted":
                summary["status"] = "skipped"
                total_skipped += 1
            else:
                summary["status"] = "error"
                summary["reason"] = result.get("reason", "unknown")
                group_errors += 1
                total_errors += 1
            results.append(summary)
        if group_submitted > 0:
            _mark_group_discussion(runtime_ctx["group_discussion_id"], "submitted", now)
            try:
                from services.collaboration_state_finalization_service import safe_request_collaboration_state_finalization

                safe_request_collaboration_state_finalization(
                    runtime_ctx["group_id"],
                    runtime_ctx["session_id"],
                    "timeout_auto_submit",
                )
            except Exception as e:
                logger.warning(
                    "auto_submit_scan: state finalization request failed group=%s session=%s: %s",
                    runtime_ctx["group_id"],
                    runtime_ctx["session_id"],
                    e,
                )
        elif group_errors == 0:
            status = "submitted" if _has_submitted_group_document(
                runtime_ctx["session_id"],
                runtime_ctx["group_id"],
                runtime_ctx["task_id"],
                runtime_ctx["session_no"],
            ) else "timed_out"
            _mark_group_discussion(runtime_ctx["group_discussion_id"], status, now)
            if status in {"submitted", "timed_out"}:
                try:
                    from services.collaboration_state_finalization_service import safe_request_collaboration_state_finalization

                    safe_request_collaboration_state_finalization(
                        runtime_ctx["group_id"],
                        runtime_ctx["session_id"],
                        "timeout_auto_submit",
                    )
                except Exception as e:
                    logger.warning(
                        "auto_submit_scan: state finalization request failed group=%s session=%s: %s",
                        runtime_ctx["group_id"],
                        runtime_ctx["session_id"],
                        e,
                    )
        if docs_processed >= max_docs:
            break

    if total_submitted > 0 or total_errors > 0:
        logger.info(
            "auto_submit_scan: %d group discussions, %d docs, %d submitted, %d skipped, %d errors",
            len(group_discussions), docs_processed, total_submitted, total_skipped, total_errors,
        )
    return {
        "ok": True,
        "scanned_group_discussions": len(group_discussions),
        "docs_processed": docs_processed,
        "submitted": total_submitted, "skipped": total_skipped, "errors": total_errors,
        "details": results,
    }
