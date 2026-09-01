# -*- coding: utf-8 -*-

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.helpers import create_group, create_student, seed_running_session


def _create_document(db, seeded):
    now = db.now_str()
    user_id, _login_key = seeded["students"][0]
    return db.execute(
        """
        INSERT INTO collaborative_documents(
            group_id, task_id, session_no, session_id, created_by,
            created_at, updated_at, content_text
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"],
            seeded["task_id"],
            seeded["session_no"],
            seeded["session_id"],
            user_id,
            now,
            now,
            "final answer",
        ),
    )


def _expired_deadline():
    return (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")


def test_perform_auto_submit_creates_submission_checkpoint_and_event(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=31, member_count=1)
    document_id = _create_document(db, seeded)
    session_ctx = {
        "session_id": seeded["session_id"],
        "task_id": seeded["task_id"],
        "session_no": seeded["session_no"],
        "deadline": _expired_deadline(),
    }

    from services.auto_submit_service import perform_auto_submit

    with patch("services.collaborative_internal.freeze_document", return_value={"ok": True}), \
         patch("services.collaborative_internal.flush_document", return_value={"ok": True, "state_revision": 2}), \
         patch("services.collaborative_internal.close_document", return_value={"ok": True}):
        result = perform_auto_submit(document_id, session_ctx)

    assert result["ok"] is True
    doc = db.query_one("SELECT status FROM collaborative_documents WHERE id=?", (document_id,))
    assert doc["status"] == "submitted"
    assert db.query_one(
        "SELECT id FROM collaborative_document_checkpoints WHERE document_id=? AND reason='submitted'",
        (document_id,),
    )
    submission = db.query_one(
        "SELECT submitted_by, submission_source, timeout_at FROM submissions WHERE group_id=?",
        (seeded["group_id"],),
    )
    assert submission["submitted_by"] == "auto_timeout"
    assert submission["submission_source"] == "auto_timeout"
    assert submission["timeout_at"] is not None
    event = db.query_one("SELECT payload_json FROM process_events WHERE event_type='auto_timeout_submission'")
    assert json.loads(event["payload_json"])["document_id"] == document_id


def test_perform_auto_submit_is_idempotent(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=32, member_count=1)
    document_id = _create_document(db, seeded)
    session_ctx = {
        "session_id": seeded["session_id"],
        "task_id": seeded["task_id"],
        "session_no": seeded["session_no"],
        "deadline": _expired_deadline(),
    }

    from services.auto_submit_service import perform_auto_submit

    with patch("services.collaborative_internal.freeze_document", return_value={"ok": True}), \
         patch("services.collaborative_internal.flush_document", return_value={"ok": True, "state_revision": 2}), \
         patch("services.collaborative_internal.close_document", return_value={"ok": True}):
        first = perform_auto_submit(document_id, session_ctx)
        second = perform_auto_submit(document_id, session_ctx)

    assert first["ok"] is True
    assert second == {"ok": False, "document_id": document_id, "reason": "already_submitted"}
    assert len(db.query_all("SELECT id FROM submissions WHERE group_id=?", (seeded["group_id"],))) == 1


def test_scan_auto_submits_only_expired_group_discussions(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=33, member_count=1)
    user_id, _headers = seeded["students"][0]
    expired_doc = _create_document(db, seeded)
    now = db.now_str()
    runtime_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, expected_student_ids_json, ready_student_ids_json,
            expected_student_count, ready_student_count, started_at, deadline, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seeded["session_id"],
            seeded["group_id"],
            "running",
            json.dumps([user_id]),
            json.dumps([user_id]),
            1,
            1,
            now,
            _expired_deadline(),
            now,
            now,
        ),
    )

    future_group_id = create_group(db, name="Future Group", code="G_FUTURE")
    future_user_id, _future_login_key = create_student(db, future_group_id, index=1, username_prefix="future")
    future_doc = db.execute(
        """
        INSERT INTO collaborative_documents(
            group_id, task_id, session_no, session_id, created_by,
            created_at, updated_at, content_text
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            future_group_id,
            seeded["task_id"],
            seeded["session_no"],
            seeded["session_id"],
            future_user_id,
            now,
            now,
            "future answer",
        ),
    )
    db.execute(
        "INSERT INTO group_session_discussions(session_id, group_id, status, deadline, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (
            seeded["session_id"],
            future_group_id,
            "running",
            (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            now,
            now,
        ),
    )

    from services.auto_submit_service import scan_and_auto_submit

    with patch("services.collaborative_internal.freeze_document", return_value={"ok": True}), \
         patch("services.collaborative_internal.flush_document", return_value={"ok": True, "state_revision": 2}), \
         patch("services.collaborative_internal.close_document", return_value={"ok": True}):
        result = scan_and_auto_submit(max_docs=100)

    assert result["submitted"] == 1
    assert db.query_one("SELECT status FROM collaborative_documents WHERE id=?", (expired_doc,))["status"] == "submitted"
    assert db.query_one("SELECT status FROM collaborative_documents WHERE id=?", (future_doc,))["status"] == "editing"
    runtime = db.query_one("SELECT status, auto_submitted_at FROM group_session_discussions WHERE id=?", (runtime_id,))
    assert runtime["status"] == "submitted"
    assert runtime["auto_submitted_at"] is not None
