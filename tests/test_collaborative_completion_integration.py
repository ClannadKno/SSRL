# -*- coding: utf-8 -*-

import io
import zipfile
from unittest.mock import patch

import pytest

from tests.helpers import expire_group_discussion, login_with_key, seed_running_session


def _deliverable_markdown(zip_data):
    with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
        path = next(name for name in archive.namelist() if name.endswith("/deliverable.md"))
        return archive.read(path).decode("utf-8")


def test_real_document_creation_binds_session_and_legacy_row_still_exports(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=301, member_count=1)
    user_id, _login_key = seeded["students"][0]

    from services.collaborative_service import get_or_create_document, save_snapshot
    from services.research_export_service import build_research_export

    document = get_or_create_document(
        seeded["group_id"],
        seeded["task_id"],
        seeded["session_no"],
        user_id,
        session_id=seeded["session_id"],
    )
    assert document["session_id"] == seeded["session_id"]
    save_snapshot(document["id"], '{"type":"doc"}', "<p>legacy compatible</p>", "legacy compatible")

    # Simulate a pre-fix submitted row. Export must resolve the old task/session_no scope.
    db.execute(
        "UPDATE collaborative_documents SET session_id=NULL, status='submitted', submitted_at=? WHERE id=?",
        (db.now_str(), document["id"]),
    )
    result = build_research_export("deliverables")
    assert "legacy compatible" in _deliverable_markdown(result["zip_data"])
    assert result["manifest"]["excluded_rows"]["deliverables"] == 0


def test_migration_backfills_legacy_document_session_idempotently(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=305, member_count=1)
    user_id, _login_key = seeded["students"][0]
    document_id = db.execute(
        "INSERT INTO collaborative_documents(group_id, task_id, session_no, created_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            seeded["group_id"],
            seeded["task_id"],
            seeded["session_no"],
            user_id,
            db.now_str(),
            db.now_str(),
        ),
    )

    import migrations

    conn = db.db()
    try:
        migrations.run_pending_migrations(conn)
        migrations.run_pending_migrations(conn)
        conn.commit()
    finally:
        conn.close()

    assert db.query_one(
        "SELECT session_id FROM collaborative_documents WHERE id=?", (document_id,)
    )["session_id"] == seeded["session_id"]
    assert any(
        row["name"] == "idx_collab_docs_session_group"
        for row in db.query_all("PRAGMA index_list('collaborative_documents')")
    )


def test_timeout_endpoint_persists_latest_client_snapshot_and_exports(db_and_app):
    db, _app, client = db_and_app
    seeded = seed_running_session(db, session_no=302, member_count=1)
    user_id, login_key = seeded["students"][0]
    headers = login_with_key(client, login_key)

    entered = client.post("/api/discussion/enter", headers=headers)
    assert entered.status_code == 200
    document_id = entered.get_json()["document"]["id"]
    old_snapshot = client.post(
        f"/api/collaborative-documents/{document_id}/snapshot",
        headers=headers,
        json={
            "content_json": '{"type":"doc","version":"old"}',
            "content_html": "<p>old snapshot</p>",
            "content_text": "old snapshot",
        },
    )
    assert old_snapshot.status_code == 200
    runtime_id = expire_group_discussion(
        db, seeded["session_id"], seeded["group_id"], user_id
    )

    with patch("services.collaborative_internal.freeze_document", return_value={"ok": True}), \
         patch("services.collaborative_internal.flush_document", return_value={"ok": True, "state_revision": 3}), \
         patch("services.collaborative_internal.close_document", return_value={"ok": True}), \
         patch("services.collaboration_state_finalization_service.safe_request_collaboration_state_finalization", return_value={"ok": True}):
        response = client.post(
            f"/api/collaborative-documents/{document_id}/submit/auto-timeout",
            headers=headers,
            json={
                "content_json": '{"type":"doc","version":"final"}',
                "content_html": "<p>latest timeout text</p>",
                "content_text": "latest timeout text",
            },
        )

    assert response.status_code == 200
    document = db.query_one(
        "SELECT session_id, status, content_text FROM collaborative_documents WHERE id=?",
        (document_id,),
    )
    assert dict(document) == {
        "session_id": seeded["session_id"],
        "status": "submitted",
        "content_text": "latest timeout text",
    }
    submission = db.query_one(
        "SELECT content, submission_source FROM submissions WHERE group_id=? ORDER BY id DESC LIMIT 1",
        (seeded["group_id"],),
    )
    assert submission["content"] == "latest timeout text"
    assert submission["submission_source"] == "auto_timeout"
    assert db.query_one(
        "SELECT status FROM group_session_discussions WHERE id=?", (runtime_id,)
    )["status"] == "submitted"

    from services.research_export_service import build_research_export

    exported = build_research_export("deliverables")
    assert "latest timeout text" in _deliverable_markdown(exported["zip_data"])


def test_teacher_end_submits_documents_before_session_closes(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=303, member_count=1)
    user_id, _login_key = seeded["students"][0]

    from services.collaborative_service import get_or_create_document, save_snapshot
    from services.group_discussion_runtime_service import enter_group_discussion_stage
    from services.teacher_session_service import end_session

    runtime = enter_group_discussion_stage(
        seeded["session_id"], seeded["group_id"], user_id
    )
    document = get_or_create_document(
        seeded["group_id"],
        seeded["task_id"],
        seeded["session_no"],
        user_id,
        session_id=seeded["session_id"],
    )
    save_snapshot(
        document["id"],
        '{"type":"doc"}',
        "<p>teacher finalized text</p>",
        "teacher finalized text",
    )

    with patch("services.collaborative_internal.freeze_document", return_value={"ok": True}), \
         patch("services.collaborative_internal.flush_document", return_value={"ok": True, "state_revision": 4}), \
         patch("services.collaborative_internal.close_document", return_value={"ok": True}), \
         patch("services.collaboration_state_finalization_service.safe_request_collaboration_state_finalization", return_value={"ok": True}), \
         patch("services.collaboration_state_finalization_service.safe_request_finalization_for_session_groups", return_value={"ok": True}):
        ended = end_session(session_id=seeded["session_id"], operator_id=user_id)

    assert ended["status"] == "ended"
    assert ended["document_submission"]["submitted"] == 1
    assert ended["document_submission"]["errors"] == 0
    final_document = db.query_one(
        "SELECT session_id, status, content_text FROM collaborative_documents WHERE id=?",
        (document["id"],),
    )
    assert dict(final_document) == {
        "session_id": seeded["session_id"],
        "status": "submitted",
        "content_text": "teacher finalized text",
    }
    assert db.query_one(
        "SELECT submission_source FROM submissions WHERE group_id=? ORDER BY id DESC LIMIT 1",
        (seeded["group_id"],),
    )["submission_source"] == "teacher_end"
    assert db.query_one(
        "SELECT status FROM group_session_discussions WHERE id=?", (runtime["id"],)
    )["status"] == "submitted"

    from services.research_export_service import build_research_export

    exported = build_research_export("deliverables")
    assert "teacher finalized text" in _deliverable_markdown(exported["zip_data"])


def test_teacher_end_keeps_session_running_when_document_persistence_fails(db_and_app):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=304, member_count=1)

    from services.teacher_session_service import end_session

    with patch(
        "services.auto_submit_service.submit_session_documents",
        return_value={
            "ok": False,
            "session_id": seeded["session_id"],
            "documents_found": 1,
            "submitted": 0,
            "errors": 1,
            "details": [{"ok": False, "reason": "claim_error"}],
        },
    ):
        with pytest.raises(ValueError, match="COLLABORATIVE_DOCUMENT_SUBMISSION_FAILED"):
            end_session(session_id=seeded["session_id"], operator_id=1)

    assert db.query_one(
        "SELECT status FROM experiment_sessions WHERE id=?", (seeded["session_id"],)
    )["status"] == "running"
