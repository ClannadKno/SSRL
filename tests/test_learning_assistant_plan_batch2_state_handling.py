# -*- coding: utf-8 -*-
"""Batch 2: state facts persist independently from intervention guards."""

from __future__ import annotations

import json

from tests.helpers import seed_running_session


def _insert_student_message(db, context: dict, content: str, client_id: str):
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy', strategy_agent_enabled=1,
               emotion_agent_enabled=0, agent_intervention_enabled=1
         WHERE id=?
        """,
        (context["session_id"],),
    )
    message = db.create_message(
        context["group_id"],
        context["students"][0][0],
        content,
        role="student",
        client_message_id=client_id,
    )
    db.execute(
        "UPDATE groups SET cutoff_sequence=last_message_sequence WHERE id=?",
        (context["group_id"],),
    )
    return message


def _configure_rule_detection(
    monkeypatch,
    *,
    state_code: str,
    sequence: int,
    score: float = 0.9,
):
    import services.discussion_pipeline_v2.monitoring_service as monitoring

    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring, "AUTO_INTERVENTION_V2_ENABLED", True)
    rule = {
        "version": "batch2_state_handling_test",
        "assessment_status": (
            "insufficient_evidence" if state_code == "unknown" else "state_detected"
        ),
        "winning_state_code": state_code,
        "winning_state_label": state_code,
        "winning_score": score,
        "evidence_sequences": [] if state_code == "unknown" else [sequence],
        "candidates": [
            {
                "state_code": state_code,
                "score": score,
                "signals": [],
            }
        ],
    }
    monkeypatch.setattr(
        monitoring.RuleDetector,
        "detect",
        staticmethod(lambda _context, _features: rule),
    )
    monkeypatch.setattr(
        monitoring.TriggerPolicy,
        "should_run_state_detector",
        staticmethod(
            lambda *_args, **_kwargs: {
                "gate": False,
                "gate_reason": "batch2_rule_only",
                "max_rule_score": score,
                "new_student_message_count": 1,
            }
        ),
    )
    monkeypatch.setattr(
        monitoring.TriggerPolicy,
        "should_enqueue_strategy_review",
        staticmethod(
            lambda *_args, **_kwargs: (
                state_code
                not in {
                    "unknown",
                    "positive_collaboration",
                },
                (
                    "confirmed_negative_state"
                    if state_code
                    not in {
                        "unknown",
                        "positive_collaboration",
                    }
                    else "state_does_not_require_intervention"
                ),
            )
        ),
    )
    return monitoring


def _count(db, table: str, group_id: int) -> int:
    return int(
        db.query_one(
            f"SELECT COUNT(*) AS c FROM {table} WHERE group_id=?",
            (group_id,),
        )["c"]
    )


def _monitor_audit(db, monitor_run_id: int) -> dict:
    row = db.query_one(
        "SELECT rule_result_json FROM monitor_runs WHERE id=?",
        (monitor_run_id,),
    )
    return json.loads(row["rule_result_json"])["monitor_audit"]


def test_normal_message_task_enables_persistence_and_strategy_evaluation(
    db_and_app,
    monkeypatch,
):
    _db, _app, _client = db_and_app
    import services.discussion_pipeline_v2.monitoring_service as monitoring
    import services.state_assessment_scheduler as scheduler
    from agent import monitoring_tasks

    captured = {}

    def fake_detection(**kwargs):
        captured.update(kwargs)
        return {
            "rule_winning_state": "conflict_tension",
            "state_detector_gate": {"gate": False},
        }

    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(fake_detection),
    )
    monkeypatch.setattr(
        scheduler,
        "request_state_assessment_for_message",
        lambda **_kwargs: {"scheduled": False},
    )

    result = monitoring_tasks.process_new_message_task.call_local(12, 34)

    assert result["state_assessment_request"] == {"scheduled": False}
    assert captured["allow_state_llm"] is False
    assert captured["persist_state_segment"] is True
    assert captured["schedule_strategy"] is True


def test_confirmed_normal_conflict_persists_then_enters_strategy_pipeline(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=821, member_count=2)
    message = _insert_student_message(
        db,
        context,
        "这个方案不对，你的结论太片面了。",
        "batch2-normal-conflict",
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="conflict_tension",
        sequence=message["sequence"],
    )
    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(
            lambda monitor_run_id, **kwargs: (
                scheduled.append({"monitor_run_id": monitor_run_id, **kwargs})
                or {"enqueued": True}
            )
        ),
    )

    result = monitoring.MonitoringService.run_detection(
        context["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    assert result["fused_state"] == "conflict_tension"
    assert _count(db, "state_assessments", context["group_id"]) == 1
    assert _count(db, "collaboration_state_segments", context["group_id"]) == 1
    assert scheduled == []
    assert result["stage1_result"]["coarse_decision"] == "ESCALATE"
    assert result["stage1_direct_strategy_scheduling_blocked"] is True
    pipeline = db.query_one(
        """
        SELECT coarse_state_code, stage2_status, publish_status
        FROM strategy_pipeline_runs
        WHERE group_id=?
        """,
        (context["group_id"],),
    )
    assert pipeline["coarse_state_code"] == "POSSIBLE_CONFLICT"
    assert pipeline["stage2_status"] == "PENDING"
    assert pipeline["publish_status"] == "NOT_READY"
    segment = db.query_one(
        """
        SELECT state_code, source, assessment_id, session_id, session_no, task_id
        FROM collaboration_state_segments
        WHERE group_id=?
        """,
        (context["group_id"],),
    )
    assert dict(segment) == {
        "state_code": "conflict_tension",
        "source": "state_monitor",
        "assessment_id": result["state_assessment_id"],
        "session_id": context["session_id"],
        "session_no": context["session_no"],
        "task_id": context["task_id"],
    }
    decision = result["state_handling_decision"]
    assert decision["should_persist"] is True
    assert decision["should_schedule_strategy"] is True
    assert decision["persist_reason"] == "confirmed_state"
    assert decision["evidence_sequences"] == [message["sequence"]]


def test_group_switch_blocks_intervention_but_not_state_fact(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=822, member_count=1)
    message = _insert_student_message(
        db,
        context,
        "完全不对，这个方案根本不合理。",
        "batch2-disabled-agent",
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=0 WHERE id=?",
        (context["group_id"],),
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="conflict_tension",
        sequence=message["sequence"],
    )
    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(
            lambda *_args, **_kwargs: scheduled.append(True) or {"enqueued": True}
        ),
    )

    result = monitoring.MonitoringService.run_detection(
        context["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
    )

    assert result["fused_state"] == "conflict_tension"
    assert _count(db, "collaboration_state_segments", context["group_id"]) == 1
    assert scheduled == []
    audit = _monitor_audit(db, result["monitor_run_id"])
    assert audit["segment_written"] is True
    assert audit["skip_reason"] == "group_auto_intervention_disabled"


def test_active_help_guard_records_skip_without_deleting_conflict(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=823, member_count=1)
    message = _insert_student_message(
        db,
        context,
        "我卡住了，但现在又有人直接否定我的方案。",
        "batch2-help-guard",
    )
    db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, handling_status, request_text, source_message_id,
            help_request_message_sequence, created_at
        ) VALUES(?,?,?,?,?,'RUNNING','running',?,?,?,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            context["task_id"],
            context["session_no"],
            context["session_id"],
            "请帮助我们继续推进。",
            message["id"],
            message["sequence"],
            db.now_str(),
        ),
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="conflict_tension",
        sequence=message["sequence"],
    )
    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(
            lambda *_args, **_kwargs: scheduled.append(True) or {"enqueued": True}
        ),
    )

    result = monitoring.MonitoringService.run_detection(
        context["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
    )

    assert _count(db, "state_assessments", context["group_id"]) == 1
    assert _count(db, "collaboration_state_segments", context["group_id"]) == 1
    assert scheduled == []
    assert _count(db, "intervention_runs", context["group_id"]) == 0
    pipeline = db.query_one(
        """
        SELECT coarse_decision, coarse_state_code, stage2_status
        FROM strategy_pipeline_runs
        WHERE group_id=?
        """,
        (context["group_id"],),
    )
    assert pipeline["coarse_decision"] == "ESCALATE"
    assert pipeline["coarse_state_code"] == "POSSIBLE_CONFLICT"
    assert pipeline["stage2_status"] == "PENDING"
    audit = _monitor_audit(db, result["monitor_run_id"])
    assert audit["segment_written"] is True
    assert audit["skip_reason"] == "stage1_deferred_to_stage2"


def test_positive_and_unknown_state_persistence_rules(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app

    positive = seed_running_session(db, session_no=824, member_count=2)
    positive_message = _insert_student_message(
        db,
        positive,
        "我整理证据，你来比较方案，我们最后一起总结。",
        "batch2-positive",
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="positive_collaboration",
        sequence=positive_message["sequence"],
    )
    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(
            lambda *_args, **_kwargs: scheduled.append(True) or {"enqueued": True}
        ),
    )
    positive_result = monitoring.MonitoringService.run_detection(
        positive["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
    )

    assert _count(db, "collaboration_state_segments", positive["group_id"]) == 1
    assert scheduled == []
    assert positive_result["state_handling_decision"]["persist_reason"] == (
        "confirmed_positive_state"
    )
    assert positive_result["state_handling_decision"][
        "should_schedule_strategy"
    ] is False

    unknown = seed_running_session(db, session_no=825, member_count=1)
    unknown_message = _insert_student_message(
        db,
        unknown,
        "我刚刚进入讨论。",
        "batch2-unknown",
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="unknown",
        sequence=unknown_message["sequence"],
        score=0.2,
    )
    unknown_result = monitoring.MonitoringService.run_detection(
        unknown["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
    )

    assert _count(db, "state_assessments", unknown["group_id"]) == 1
    assert _count(db, "collaboration_state_segments", unknown["group_id"]) == 0
    assert unknown_result["state_handling_decision"]["should_persist"] is False
    assert unknown_result["state_handling_decision"]["persist_reason"] == (
        "final_state_unknown"
    )


def test_persist_and_schedule_flags_are_independent(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app

    persist_only = seed_running_session(db, session_no=826, member_count=1)
    persist_message = _insert_student_message(
        db,
        persist_only,
        "你这个方案完全不合理。",
        "batch2-persist-only",
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="conflict_tension",
        sequence=persist_message["sequence"],
    )
    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(
            lambda *_args, **_kwargs: scheduled.append(True) or {"enqueued": True}
        ),
    )
    persisted = monitoring.MonitoringService.run_detection(
        persist_only["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=False,
    )

    assert _count(
        db,
        "collaboration_state_segments",
        persist_only["group_id"],
    ) == 1
    assert scheduled == []
    assert persisted["state_handling_decision"]["should_persist"] is True
    assert persisted["state_handling_decision"][
        "should_schedule_strategy"
    ] is False

    schedule_only = seed_running_session(db, session_no=827, member_count=1)
    schedule_message = _insert_student_message(
        db,
        schedule_only,
        "别乱说，这个结论完全不对。",
        "batch2-schedule-only",
    )
    monitoring = _configure_rule_detection(
        monkeypatch,
        state_code="conflict_tension",
        sequence=schedule_message["sequence"],
    )
    scheduled.clear()
    scheduled_result = monitoring.MonitoringService.run_detection(
        schedule_only["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
        persist_state_segment=False,
        schedule_strategy=True,
    )

    assert _count(
        db,
        "collaboration_state_segments",
        schedule_only["group_id"],
    ) == 0
    assert scheduled == []
    assert scheduled_result["stage1_direct_strategy_scheduling_blocked"] is True
    assert scheduled_result["state_handling_decision"]["should_persist"] is False
    assert scheduled_result["state_handling_decision"][
        "should_schedule_strategy"
    ] is True
