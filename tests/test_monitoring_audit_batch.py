# -*- coding: utf-8 -*-
"""Structured monitoring audit coverage for the V2 state pipeline."""

import json

from tests.helpers import seed_running_session


AUDIT_STATES = {
    "positive_collaboration",
    "negative_silence",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
}


def _enable_monitoring(monkeypatch, *, auto=False):
    import services.discussion_pipeline_v2.monitoring_service as ms

    monkeypatch.setattr(ms, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(ms, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(ms, "AUTO_INTERVENTION_V2_ENABLED", auto)
    return ms


def _audit_from_run(db, monitor_run_id):
    row = db.query_one(
        "SELECT * FROM monitor_runs WHERE id=?",
        (monitor_run_id,),
    )
    payload = json.loads(row["rule_result_json"])
    return row, payload, payload["monitor_audit"]


def test_student_message_detection_writes_structured_monitor_audit_without_llm(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ms = _enable_monitoring(monkeypatch, auto=False)

    from services.discussion_pipeline_v2.llm_state_detector import LLMStateDetector

    monkeypatch.setattr(
        LLMStateDetector,
        "detect",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("state detector LLM called"))),
    )

    ctx = seed_running_session(db, session_no=701, member_count=2)
    message = db.create_message(
        ctx["group_id"],
        ctx["students"][0][0],
        "我先打开资料，准备讨论证据。",
        role="student",
        client_message_id="audit-normal-message-1",
    )

    result = ms.MonitoringService.run_detection(ctx["group_id"], trigger_type="new_message")

    assert result["steps"]["completed"] is True
    assert result["monitor_run_id"] is not None
    row, payload, audit = _audit_from_run(db, result["monitor_run_id"])
    assert row["status"] == "completed"
    assert payload["winning_state_code"] == row["final_state"]
    assert AUDIT_STATES <= set(audit["rule_scores"])
    assert audit["group_id"] == ctx["group_id"]
    assert audit["session_id"] == ctx["session_id"]
    assert audit["session_no"] == ctx["session_no"]
    assert audit["task_id"] == ctx["task_id"]
    assert audit["cutoff_sequence"] == message["sequence"]
    assert audit["context_message_ids"] == [message["id"]]
    assert audit["state_llm_called"] is False
    assert audit["final_state"] == row["final_state"]
    assert audit["state_assessment_written"] is True
    assert audit["state_assessment_id"] is not None
    assert audit["group_state_written"] is True
    assert audit["segment_write_attempted"] is False
    assert audit["strategy_review_enqueued"] is False

    assessment = db.query_one(
        "SELECT session_id, task_id, session_no FROM state_assessments WHERE id=?",
        (audit["state_assessment_id"],),
    )
    assert dict(assessment) == {
        "session_id": ctx["session_id"],
        "task_id": ctx["task_id"],
        "session_no": ctx["session_no"],
    }

    diagnosis = ms.MonitoringService.diagnose_detection(
        ctx["group_id"],
        session_id=ctx["session_id"],
        cutoff_sequence=message["sequence"],
    )
    assert diagnosis["existing_monitor_run_id"] == result["monitor_run_id"]
    assert diagnosis["existing_monitor_audit"]["final_state"] == row["final_state"]


def test_state_assessment_failure_is_recorded_with_explicit_reason(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ms = _enable_monitoring(monkeypatch, auto=False)
    ctx = seed_running_session(db, session_no=702, member_count=1)
    db.create_message(
        ctx["group_id"],
        ctx["students"][0][0],
        "我还在理解任务。",
        role="student",
        client_message_id="audit-persist-fail-1",
    )

    def fail_persist(*_args, **_kwargs):
        raise RuntimeError("state insert failed")

    monkeypatch.setattr(ms, "persist_state_assessment", fail_persist)

    result = ms.MonitoringService.run_detection(ctx["group_id"], trigger_type="new_message")

    assert result["steps"]["completed"] is True
    _row, _payload, audit = _audit_from_run(db, result["monitor_run_id"])
    assert audit["state_assessment_written"] is False
    assert audit["group_state_written"] is False
    assert audit["skip_reason"] == "state_persistence_failed"
    assert "state insert failed" in audit["state_persistence_error"]
    assert db.query_one("SELECT COUNT(*) AS c FROM state_assessments WHERE group_id=?", (ctx["group_id"],))["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM group_states WHERE group_id=?", (ctx["group_id"],))["c"] == 0


def test_segment_audit_distinguishes_unknown_skip_and_persistence_failure(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ms = _enable_monitoring(monkeypatch, auto=False)

    unknown_ctx = seed_running_session(db, session_no=703, member_count=1)
    db.create_message(
        unknown_ctx["group_id"],
        unknown_ctx["students"][0][0],
        "我刚进入页面。",
        role="student",
        client_message_id="audit-unknown-1",
    )
    unknown = ms.MonitoringService.run_detection(unknown_ctx["group_id"], trigger_type="new_message")
    _row, _payload, unknown_audit = _audit_from_run(db, unknown["monitor_run_id"])
    assert unknown_audit["final_state"] == "unknown"
    assert unknown_audit["segment_write_attempted"] is False
    assert unknown_audit["segment_written"] is False
    assert unknown_audit["segment_skip_reason"] == "final_state_unknown"

    silence_ctx = seed_running_session(db, session_no=704, member_count=1)
    message = db.create_message(
        silence_ctx["group_id"],
        silence_ctx["students"][0][0],
        "上一条学生消息。",
        role="student",
        client_message_id="audit-silence-1",
    )

    rule = {
        "version": "test_rule",
        "assessment_status": "state_detected",
        "winning_state_code": "negative_silence",
        "winning_state_label": "消极沉默",
        "winning_score": 0.8,
        "candidates": [
            {"state_code": "negative_silence", "score": 0.8, "signals": []},
        ],
    }
    monkeypatch.setattr(
        ms.ContextService,
        "collect",
        staticmethod(
            lambda group_id, **_kwargs: {
                "group_id": group_id,
                "session_id": silence_ctx["session_id"],
                "session_no": silence_ctx["session_no"],
                "task_id": silence_ctx["task_id"],
                "window_messages": [],
                "window_student_messages": [],
                "recent_student_messages": [],
            }
        ),
    )
    monkeypatch.setattr(ms.FeatureService, "extract", staticmethod(lambda _context: {"behavior": {}, "text": {}}))
    monkeypatch.setattr(ms.RuleDetector, "detect", staticmethod(lambda _context, _features: rule))
    monkeypatch.setattr(ms.TriggerPolicy, "should_call_llm", staticmethod(lambda *_args, **_kwargs: (False, "test_no_review")))

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    monkeypatch.setattr(
        CollaborationStateSegmentService,
        "record_negative_silence_if_applicable",
        staticmethod(lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("segment db locked"))),
    )

    silence = ms.MonitoringService.run_detection(
        silence_ctx["group_id"],
        trigger_type="silence_check",
        silence_expected_sequence=message["sequence"],
    )
    _row, _payload, silence_audit = _audit_from_run(db, silence["monitor_run_id"])
    assert silence_audit["final_state"] == "negative_silence"
    assert silence_audit["segment_write_attempted"] is True
    assert silence_audit["segment_written"] is False
    assert silence_audit["segment_skip_reason"] == "segment_persistence_failed"
    assert silence_audit["skip_reason"] == "segment_persistence_failed"


def test_duplicate_cutoff_does_not_duplicate_persistent_records(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ms = _enable_monitoring(monkeypatch, auto=False)
    ctx = seed_running_session(db, session_no=705, member_count=1)
    db.create_message(
        ctx["group_id"],
        ctx["students"][0][0],
        "我先整理证据。",
        role="student",
        client_message_id="audit-duplicate-1",
    )

    first = ms.MonitoringService.run_detection(ctx["group_id"], trigger_type="new_message")
    counts_after_first = {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in [
            "monitor_runs",
            "state_assessments",
            "group_states",
            "collaboration_state_segments",
        ]
    }
    second = ms.MonitoringService.run_detection(ctx["group_id"], trigger_type="new_message")
    counts_after_second = {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in counts_after_first
    }

    assert first["monitor_run_id"] is not None
    assert second["skipped"] is True
    assert second["reason"] == "duplicate_cutoff"
    assert counts_after_second == counts_after_first


def test_detection_scope_uses_trigger_message_session_not_current_global_session(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ms = _enable_monitoring(monkeypatch, auto=False)
    ctx_a = seed_running_session(db, session_no=706, member_count=1)
    message = db.create_message(
        ctx_a["group_id"],
        ctx_a["students"][0][0],
        "第一课次的消息不能串到下一课次。",
        role="student",
        client_message_id="audit-scope-a",
    )
    ctx_b = seed_running_session(db, session_no=707, member_count=1)
    assert ctx_b["session_id"] != ctx_a["session_id"]

    result = ms.MonitoringService.run_detection(ctx_a["group_id"], trigger_type="new_message")
    _row, _payload, audit = _audit_from_run(db, result["monitor_run_id"])
    assert audit["cutoff_sequence"] == message["sequence"]
    assert audit["session_id"] == ctx_a["session_id"]
    assert audit["session_no"] == ctx_a["session_no"]
    assert audit["task_id"] == ctx_a["task_id"]

    assessment = db.query_one(
        "SELECT session_id, task_id, session_no FROM state_assessments WHERE id=?",
        (audit["state_assessment_id"],),
    )
    assert dict(assessment) == {
        "session_id": ctx_a["session_id"],
        "task_id": ctx_a["task_id"],
        "session_no": ctx_a["session_no"],
    }


def test_huey_monitoring_task_exception_returns_error_without_crashing(db_and_app, monkeypatch):
    _db, _app, _client = db_and_app

    from agent import monitoring_tasks
    import services.discussion_pipeline_v2.monitoring_service as ms

    monkeypatch.setattr(
        ms.MonitoringService,
        "run_detection",
        staticmethod(lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))),
    )

    result = monitoring_tasks.process_new_message_task.call_local(123, 7)

    assert result == {"error": "boom"}
