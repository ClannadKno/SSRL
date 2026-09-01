# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timedelta

from services.llm_gateway import LlmResult
from tests.helpers import seed_running_session


class FakeGateway:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def call(self, profile_name, payload, response_type="json"):
        self.calls.append({
            "profile_name": profile_name,
            "payload": payload,
            "response_type": response_type,
        })
        if isinstance(self.output, LlmResult):
            return self.output
        return LlmResult(success=True, output=self.output, profile_name=profile_name)


def _insert_message(
    db,
    ctx,
    *,
    sequence: int,
    content: str = "message",
    user_index: int = 0,
    role: str = "student",
    agent_type: str = None,
    group_id: int = None,
    session_id: int = None,
    session_no: int = None,
    task_id: int = None,
):
    gid = group_id if group_id is not None else ctx["group_id"]
    uid = ctx["students"][user_index % len(ctx["students"])][0]
    sid = session_id if session_id is not None else ctx["session_id"]
    sno = session_no if session_no is not None else ctx["session_no"]
    tid = task_id if task_id is not None else ctx["task_id"]
    sender_type = "agent" if role == "agent" else role
    db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, agent_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            gid,
            uid,
            content,
            sequence,
            sender_type,
            role,
            sno,
            tid,
            sid,
            agent_type,
            db.now_str(),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=MAX(COALESCE(last_message_sequence, 0), ?)
        WHERE id=?
        """,
        (sequence, gid),
    )


def _end_session(db, ctx):
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE id=?", (ctx["session_id"],))


def _doc(db, ctx, *, status="editing"):
    return db.execute(
        """
        INSERT INTO collaborative_documents(
            group_id, task_id, session_no, session_id, title, status, created_by,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            ctx["task_id"],
            ctx["session_no"],
            ctx["session_id"],
            "Doc",
            status,
            ctx["students"][0][0],
            db.now_str(),
            db.now_str(),
        ),
    )


def _segment_output():
    return {
        "state_segments": [
            {
                "state": "conflict_tension",
                "start_message_id": 24,
                "end_message_id": 26,
                "evidence_message_ids": [24, 26],
                "confidence": 0.86,
            },
            {
                "state": "positive_collaboration",
                "start_message_id": 27,
                "end_message_id": 36,
                "evidence_message_ids": [27, 28, 31, 36],
                "confidence": 0.92,
            },
        ]
    }


def test_finalization_classifies_conflict_recovery_and_sends_no_agent_message(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=401, member_count=2)
    messages = {
        24: "你这个方案完全没依据，别再坚持了。",
        25: "不是我坚持，是你的比较标准根本不清楚。",
        26: "这样争下去没有结果。",
        27: "先停止争论，我们建立预算、距离和可维护性三个比较标准。",
        28: "我负责补预算证据，你整理距离和访谈反馈。",
        29: "方案A预算低，但维护风险要注明。",
        30: "方案B距离近，我补了访谈里支持这个点的证据。",
        31: "先整合成阶段结论：A成本优势，B使用便利。",
        33: "再补一个风险：A后期维护可能占人力。",
        34: "我把访谈摘要放进证据列。",
        35: "比较表按标准排好了。",
        36: "最终结论写成优先B，同时说明A的成本备选价值。",
    }
    for seq, content in messages.items():
        _insert_message(db, ctx, sequence=seq, content=content, user_index=seq)
    _end_session(db, ctx)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    gateway = FakeGateway(_segment_output())
    before_agent_count = db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"]
    result = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "teacher_close", gateway=gateway)
    after_agent_count = db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"]

    assert result["ok"] is True
    assert result["should_intervene"] is False
    assert len(gateway.calls) == 1
    assert before_agent_count == after_agent_count
    rows = db.query_all(
        """
        SELECT state_code, start_message_id, end_message_id, is_finalized, source
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        ORDER BY start_message_id
        """,
        (ctx["group_id"], ctx["session_id"]),
    )
    assert [dict(row) for row in rows] == [
        {
            "state_code": "conflict_tension",
            "start_message_id": 24,
            "end_message_id": 26,
            "is_finalized": 1,
            "source": "session_finalizer",
        },
        {
            "state_code": "positive_collaboration",
            "start_message_id": 27,
            "end_message_id": 36,
            "is_finalized": 1,
            "source": "session_finalizer",
        },
    ]


def test_repeated_finalization_success_is_idempotent(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=402, member_count=2)
    _insert_message(db, ctx, sequence=24, content="你这个不对。")
    _insert_message(db, ctx, sequence=25, content="先列标准再比较。", user_index=1)
    _end_session(db, ctx)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    first_gateway = FakeGateway({
        "state_segments": [
            {
                "state": "positive_collaboration",
                "start_message_id": 25,
                "end_message_id": 25,
                "evidence_message_ids": [25],
                "confidence": 0.9,
            }
        ]
    })
    second_gateway = FakeGateway(_segment_output())

    first = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "student_submit", gateway=first_gateway)
    second = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "teacher_close", gateway=second_gateway)

    assert first["ok"] is True
    assert second["skipped"] is True
    assert second["reason"] == "already_finalized"
    assert len(second_gateway.calls) == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM collaboration_state_finalizations")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM collaboration_state_segments")["c"] == 1


def test_no_student_messages_does_not_call_llm(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=403)
    _end_session(db, ctx)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    gateway = FakeGateway(_segment_output())
    result = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "session_end", gateway=gateway)

    assert result["skipped"] is True
    assert result["reason"] == "no_student_messages"
    assert len(gateway.calls) == 0
    row = db.query_one("SELECT status FROM collaboration_state_finalizations")
    assert row["status"] == "succeeded"


def test_all_finalized_tail_does_not_call_llm(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=404)
    _insert_message(db, ctx, sequence=1, content="我们已经分工。")
    _end_session(db, ctx)

    from services.collaboration_state_segment_service import CollaborationStateSegmentService
    from services.collaboration_state_finalization_service import finalize_collaboration_states

    CollaborationStateSegmentService.save_finalization_segments(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_segments=[
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.9,
            }
        ],
        source_run_id=99,
        analysis_anchor_message_id=1,
        analysis_window_start_message_id=1,
        analysis_window_end_message_id=1,
        prompt_version="test",
    )
    gateway = FakeGateway(_segment_output())
    result = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "teacher_close", gateway=gateway)

    assert result["skipped"] is True
    assert result["reason"] == "all_student_messages_finalized"
    assert len(gateway.calls) == 0


def test_llm_json_failure_is_retryable_without_duplicate_finalization_rows(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=405)
    _insert_message(db, ctx, sequence=1, content="我负责找证据。")
    _end_session(db, ctx)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    bad_gateway = FakeGateway("not-json")
    failed = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "teacher_close", gateway=bad_gateway)
    assert failed["ok"] is False
    row = db.query_one("SELECT id, status, retry_count FROM collaboration_state_finalizations")
    assert row["status"] == "failed"
    first_id = row["id"]

    good_gateway = FakeGateway({
        "state_segments": [
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.91,
            }
        ]
    })
    retried = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "teacher_close", gateway=good_gateway)

    assert retried["ok"] is True
    row = db.query_one("SELECT id, status, retry_count FROM collaboration_state_finalizations")
    assert row["id"] == first_id
    assert row["status"] == "succeeded"
    assert row["retry_count"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM collaboration_state_finalizations")["c"] == 1


def test_agent_evidence_rejects_only_its_segment(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=406)
    _insert_message(db, ctx, sequence=1, content="先列标准。")
    _insert_message(db, ctx, sequence=2, content="情绪提示", role="agent", agent_type="emotion")
    _insert_message(db, ctx, sequence=3, content="继续补充学生证据。")
    _end_session(db, ctx)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    gateway = FakeGateway({
        "state_segments": [
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 3,
                "evidence_message_ids": [2],
                "confidence": 0.9,
            }
        ]
    })
    result = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "teacher_close", gateway=gateway)

    assert result["ok"] is True
    assert result["segment_results"]["saved_count"] == 0
    assert result["segment_results"]["rejected_count"] == 1
    assert result["segment_results"]["rejected"][0]["reason"] == (
        "state_segment_evidence_not_student"
    )
    assert db.query_one("SELECT COUNT(*) AS c FROM collaboration_state_segments")["c"] == 0
    assert db.query_one("SELECT status FROM collaboration_state_finalizations")["status"] == "succeeded"


def test_group_session_isolation(db_and_app):
    db, _app, _client = db_and_app
    ctx1 = seed_running_session(db, session_no=407, member_count=1)
    ctx2 = seed_running_session(db, session_no=408, member_count=1)
    _insert_message(db, ctx1, sequence=1, content="我们先分工。")
    _insert_message(db, ctx2, sequence=1, content="另一个课次消息。")
    _end_session(db, ctx1)
    _end_session(db, ctx2)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    gateway = FakeGateway({
        "state_segments": [
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.88,
            }
        ]
    })
    result = finalize_collaboration_states(ctx1["group_id"], ctx1["session_id"], "teacher_close", gateway=gateway)

    assert result["ok"] is True
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_segments WHERE group_id=? AND session_id=?",
        (ctx1["group_id"], ctx1["session_id"]),
    )["c"] == 1
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_segments WHERE group_id=? AND session_id=?",
        (ctx2["group_id"], ctx2["session_id"]),
    )["c"] == 0


def test_running_discussion_does_not_call_llm(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=409)
    _insert_message(db, ctx, sequence=1, content="还在讨论。")
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    runtime = enter_group_discussion_stage(ctx["session_id"], ctx["group_id"], ctx["students"][0][0])
    db.execute(
        "UPDATE group_session_discussions SET status='running', deadline=?, updated_at=? WHERE id=?",
        (
            (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            db.now_str(),
            runtime["id"],
        ),
    )

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    gateway = FakeGateway(_segment_output())
    result = finalize_collaboration_states(ctx["group_id"], ctx["session_id"], "student_submit", gateway=gateway)

    assert result["ok"] is False
    assert result["reason"] == "discussion_still_running"
    assert len(gateway.calls) == 0


def test_student_submit_and_room_freeze_paths_request_finalization(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=410)
    document_id = _doc(db, ctx)
    calls = []

    import services.collaboration_state_finalization_service as finalizer

    def fake_request(doc_id, reason):
        calls.append((doc_id, reason))
        return {"queued": True}

    monkeypatch.setattr(finalizer, "safe_request_finalization_for_document", fake_request)
    from services.collaborative_service import set_document_status

    set_document_status(document_id, "submitted", ctx["students"][0][0])
    db.execute("UPDATE collaborative_documents SET status='editing', submitted_at=NULL WHERE id=?", (document_id,))
    set_document_status(document_id, "locked", ctx["students"][0][0])

    assert calls == [(document_id, "student_submit"), (document_id, "room_freeze")]


def test_finalization_request_failure_does_not_block_submit(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=411)
    document_id = _doc(db, ctx)

    import services.collaboration_state_finalization_service as finalizer

    def boom(_doc_id, _reason):
        raise RuntimeError("queue down")

    monkeypatch.setattr(finalizer, "safe_request_finalization_for_document", boom)
    from services.collaborative_service import set_document_status

    set_document_status(document_id, "submitted", ctx["students"][0][0])

    doc = db.query_one("SELECT status FROM collaborative_documents WHERE id=?", (document_id,))
    assert doc["status"] == "submitted"


def test_teacher_close_requests_all_session_group_finalizations(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=412)
    _insert_message(db, ctx, sequence=1, content="我们先分工。")
    calls = []

    import services.collaboration_state_finalization_service as finalizer

    def fake_session_request(session_id, reason):
        calls.append((session_id, reason))
        return {"requested": 1}

    monkeypatch.setattr(finalizer, "safe_request_finalization_for_session_groups", fake_session_request)
    from services.teacher_session_service import end_session

    end_session(session_id=ctx["session_id"], operator_id=1)

    assert calls == [(ctx["session_id"], "teacher_close")]


def test_timeout_auto_submit_requests_finalization(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=413)
    document_id = _doc(db, ctx)
    deadline = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    calls = []

    import services.collaboration_state_finalization_service as finalizer

    def fake_request(group_id, session_id, reason):
        calls.append((group_id, session_id, reason))
        return {"queued": True}

    monkeypatch.setattr(finalizer, "safe_request_collaboration_state_finalization", fake_request)
    monkeypatch.setattr("services.collaborative_internal.freeze_document", lambda _doc_id: {"ok": True})
    monkeypatch.setattr("services.collaborative_internal.flush_document", lambda _doc_id: {"ok": True, "state_revision": 1})
    monkeypatch.setattr("services.collaborative_internal.close_document", lambda _doc_id: {"ok": True})

    from services.auto_submit_service import perform_auto_submit

    result = perform_auto_submit(document_id, {
        "session_id": ctx["session_id"],
        "group_discussion_id": None,
        "group_id": ctx["group_id"],
        "task_id": ctx["task_id"],
        "session_no": ctx["session_no"],
        "deadline": deadline,
    })

    assert result["ok"] is True
    assert calls == [(ctx["group_id"], ctx["session_id"], "timeout_auto_submit")]
