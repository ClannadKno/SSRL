# -*- coding: utf-8 -*-
"""Batch 4: evidence boundaries and session-finalizer partial success."""

from __future__ import annotations

import json

from services.llm_gateway import LlmResult
from tests.helpers import seed_running_session


class FakeGateway:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def call(self, profile_name, payload, response_type="json"):
        self.calls.append(
            {
                "profile_name": profile_name,
                "payload": payload,
                "response_type": response_type,
            }
        )
        return LlmResult(
            success=True,
            output=self.output,
            profile_name=profile_name,
        )


def _ensure_discussion(db, context: dict) -> int:
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy', strategy_agent_enabled=1,
               emotion_agent_enabled=0, agent_intervention_enabled=1
         WHERE id=?
        """,
        (context["session_id"],),
    )
    if context.get("discussion_id") is not None:
        return int(context["discussion_id"])
    existing = db.query_one(
        """
        SELECT id
        FROM group_session_discussions
        WHERE session_id=? AND group_id=?
        """,
        (context["session_id"], context["group_id"]),
    )
    if existing:
        context["discussion_id"] = int(existing["id"])
        return context["discussion_id"]
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (
            context["session_id"],
            context["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    context["discussion_id"] = int(discussion_id)
    return context["discussion_id"]


def _insert_message(
    db,
    context: dict,
    *,
    sequence: int,
    content: str,
    role: str = "student",
    agent_type: str = None,
) -> int:
    discussion_id = _ensure_discussion(db, context)
    message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, agent_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            content,
            sequence,
            role,
            role,
            context["session_no"],
            context["task_id"],
            context["session_id"],
            discussion_id,
            agent_type,
            db.now_str(),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=MAX(COALESCE(last_message_sequence, 0), ?),
            cutoff_sequence=MAX(COALESCE(cutoff_sequence, 0), ?)
        WHERE id=?
        """,
        (sequence, sequence, context["group_id"]),
    )
    return int(message_id)


def _completed_cutoff(db, monitoring, context: dict, cutoff: int) -> None:
    db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, status,
            analyzer_version, shadow, created_at, completed_at
        ) VALUES(?,?,'new_message','completed',?,0,?,?)
        """,
        (
            context["group_id"],
            cutoff,
            monitoring.PIPELINE_V2_ANALYZER_VERSION,
            db.now_str(),
            db.now_str(),
        ),
    )


def _configure_rule_only(
    monkeypatch,
    *,
    state_code: str,
    evidence_message_ids: list[int],
):
    import services.discussion_pipeline_v2.monitoring_service as monitoring

    rule = {
        "version": "batch4_evidence_test",
        "assessment_status": "state_detected",
        "winning_state_code": state_code,
        "winning_state_label": state_code,
        "winning_score": 0.9,
        "evidence": {state_code: evidence_message_ids},
        "candidates": [
            {
                "state_code": state_code,
                "score": 0.9,
                "signals": [{"reason": "explicit_test_evidence"}],
            }
        ],
    }
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring, "AUTO_INTERVENTION_V2_ENABLED", True)
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
                "gate_reason": "batch4_rule_only",
                "max_rule_score": 0.9,
                "new_student_message_count": 1,
            }
        ),
    )
    monkeypatch.setattr(
        monitoring.TriggerPolicy,
        "should_enqueue_strategy_review",
        staticmethod(lambda *_args, **_kwargs: (True, "confirmed_negative_state")),
    )
    return monitoring


def test_old_evidence_cannot_be_replaced_by_new_progress_message(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=941, member_count=2)
    evidence_ids = [
        _insert_message(
            db,
            context,
            sequence=sequence,
            content=content,
        )
        for sequence, content in (
            (11, "太无聊了，先聊游戏。"),
            (12, "食堂今天吃什么？"),
            (14, "任务先放着吧。"),
        )
    ]
    _insert_message(
        db,
        context,
        sequence=18,
        content="回到任务，我已经把方案比较表整理好了。",
    )
    monitoring = _configure_rule_only(
        monkeypatch,
        state_code="task_detached",
        evidence_message_ids=evidence_ids,
    )
    _completed_cutoff(db, monitoring, context, 14)
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

    decision = result["state_handling_decision"]
    assert decision["evidence_sequences"] == [11, 12, 14]
    assert decision["new_evidence_sequences"] == []
    assert decision["should_persist"] is False
    assert decision["persist_reason"] == "no_new_supporting_evidence"
    assert decision["should_schedule_strategy"] is False
    assert decision["schedule_reason"] == "no_new_supporting_evidence"
    assert result["strategy_review_candidate"] is False
    assert scheduled == []
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_segments WHERE group_id=?",
        (context["group_id"],),
    )["c"] == 0
    monitor_row = db.query_one(
        """
        SELECT evidence_sequences_json
        FROM monitor_runs
        WHERE group_id=? AND cutoff_sequence=18
        """,
        (context["group_id"],),
    )
    assert json.loads(monitor_row["evidence_sequences_json"]) == [11, 12, 14]


def test_new_explicit_evidence_anchors_covered_range_and_intervention(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=942, member_count=2)
    _insert_message(
        db,
        context,
        sequence=14,
        content="上一轮已经处理完成。",
    )
    _insert_message(
        db,
        context,
        sequence=18,
        content="继续按任务标准比较方案。",
    )
    evidence_id = _insert_message(
        db,
        context,
        sequence=19,
        content="算了先打游戏，任务以后再说。",
    )
    monitoring = _configure_rule_only(
        monkeypatch,
        state_code="task_detached",
        evidence_message_ids=[evidence_id],
    )
    _completed_cutoff(db, monitoring, context, 14)
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

    segment = db.query_one(
        """
        SELECT start_message_id, end_message_id, evidence_message_ids_json,
               trigger_sequence, analysis_window_start_message_id,
               analysis_window_end_message_id
        FROM collaboration_state_segments
        WHERE group_id=?
        """,
        (context["group_id"],),
    )
    assert segment["start_message_id"] == 19
    assert segment["end_message_id"] == 19
    assert json.loads(segment["evidence_message_ids_json"]) == [19]
    assert segment["trigger_sequence"] == 19
    assert segment["analysis_window_start_message_id"] <= 19
    assert segment["analysis_window_end_message_id"] == 19
    assert result["state_handling_decision"]["new_evidence_sequences"] == [19]
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
    assert pipeline["coarse_state_code"] == "POSSIBLE_DETACHMENT"
    assert pipeline["stage2_status"] == "PENDING"
    assert pipeline["publish_status"] == "NOT_READY"


def _end_session(db, context: dict) -> None:
    db.execute(
        "UPDATE experiment_sessions SET status='ended' WHERE id=?",
        (context["session_id"],),
    )


def _seed_unfinalized_anchor(context: dict, sequence: int) -> None:
    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )

    CollaborationStateSegmentService.save_strategy_llm_segments(
        group_id=context["group_id"],
        session_id=context["session_id"],
        session_no=context["session_no"],
        task_id=context["task_id"],
        state_segments=[
            {
                "state": "positive_collaboration",
                "start_message_id": sequence,
                "end_message_id": sequence,
                "evidence_message_ids": [sequence],
                "confidence": 0.8,
            }
        ],
        source_run_id=9000 + sequence,
        analysis_anchor_message_id=sequence,
        analysis_window_start_message_id=sequence,
        analysis_window_end_message_id=sequence,
        prompt_version="batch4_anchor",
    )


def test_finalizer_allows_agent_messages_inside_student_boundaries(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=943, member_count=2)
    _insert_message(db, context, sequence=25, content="先确定比较标准。")
    _insert_message(
        db,
        context,
        sequence=27,
        content="策略支持消息。",
        role="agent",
        agent_type="strategy",
    )
    _insert_message(
        db,
        context,
        sequence=34,
        content="情绪支持消息。",
        role="agent",
        agent_type="emotion",
    )
    _insert_message(
        db,
        context,
        sequence=38,
        content="求助支持消息。",
        role="agent",
        agent_type="help",
    )
    _insert_message(db, context, sequence=39, content="证据齐了，形成结论。")
    _seed_unfinalized_anchor(context, 25)
    _end_session(db, context)

    from services.collaboration_state_finalization_service import (
        finalize_collaboration_states,
    )

    gateway = FakeGateway(
        {
            "state_segments": [
                {
                    "state": "positive_collaboration",
                    "start_message_id": 25,
                    "end_message_id": 39,
                    "evidence_message_ids": [25, 39],
                    "confidence": 0.92,
                }
            ]
        }
    )
    result = finalize_collaboration_states(
        context["group_id"],
        context["session_id"],
        "teacher_close",
        gateway=gateway,
    )

    assert result["ok"] is True
    assert result["segment_results"]["saved_count"] == 1, result
    assert result["segment_results"]["rejected"] == []
    assert result["segment_results"]["agent_message_sequences_inside_range"] == [
        27,
        34,
        38,
    ]
    row = db.query_one(
        """
        SELECT start_message_id, end_message_id, evidence_message_ids_json
        FROM collaboration_state_segments
        WHERE group_id=?
        """,
        (context["group_id"],),
    )
    assert (row["start_message_id"], row["end_message_id"]) == (25, 39)
    assert json.loads(row["evidence_message_ids_json"]) == [25, 39]


def test_finalizer_normalizes_agent_boundary_and_partially_rejects(
    db_and_app,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=944, member_count=2)
    _insert_message(db, context, sequence=25, content="先保留冲突事实。")
    _insert_message(
        db,
        context,
        sequence=27,
        content="策略支持消息。",
        role="agent",
        agent_type="strategy",
    )
    _insert_message(db, context, sequence=28, content="我们重新制定标准。")
    _insert_message(db, context, sequence=30, content="我接受新分工。")
    _insert_message(db, context, sequence=32, content="开始补证据。")
    _insert_message(
        db,
        context,
        sequence=33,
        content="情绪支持消息。",
        role="agent",
        agent_type="emotion",
    )
    _insert_message(db, context, sequence=34, content="形成阶段结论。")
    _seed_unfinalized_anchor(context, 25)
    _end_session(db, context)

    from services.collaboration_state_finalization_service import (
        finalize_collaboration_states,
    )

    gateway = FakeGateway(
        {
            "current_state": "conflict_tension",
            "state_segments": [
                {
                    "state": "conflict_tension",
                    "start_message_id": 25,
                    "end_message_id": 25,
                    "evidence_message_ids": [25],
                    "confidence": 0.86,
                },
                {
                    "state": "positive_collaboration",
                    "start_message_id": 27,
                    "end_message_id": 30,
                    "evidence_message_ids": [28, 30],
                    "confidence": 0.91,
                },
                {
                    "state": "positive_collaboration",
                    "start_message_id": 32,
                    "end_message_id": 34,
                    "evidence_message_ids": [33],
                    "confidence": 0.88,
                },
            ]
        }
    )
    result = finalize_collaboration_states(
        context["group_id"],
        context["session_id"],
        "teacher_close",
        gateway=gateway,
    )

    assert result["ok"] is True
    segment_results = result["segment_results"]
    assert segment_results["proposed_count"] == 3
    assert segment_results["normalized_count"] == 2, result
    assert segment_results["saved_count"] == 2
    assert [item["segment_order"] for item in segment_results["rejected"]] == [2]
    assert segment_results["rejected"][0]["reason"] == (
        "state_segment_evidence_not_student"
    )
    assert segment_results["current_state_normalization_reason"] == (
        "derived_from_latest_accepted_segment_after_partial_rejection"
    )
    normalization = segment_results["normalized"][1]["boundary_normalization"]
    assert normalization == {
        "original_start": 27,
        "original_end": 30,
        "normalized_start": 28,
        "normalized_end": 30,
        "start_reason": "mapped_forward_to_student",
        "end_reason": "already_student",
    }
    rows = db.query_all(
        """
        SELECT state_code, start_message_id, end_message_id
        FROM collaboration_state_segments
        WHERE group_id=?
        ORDER BY start_message_id
        """,
        (context["group_id"],),
    )
    assert [dict(row) for row in rows] == [
        {
            "state_code": "conflict_tension",
            "start_message_id": 25,
            "end_message_id": 25,
        },
        {
            "state_code": "positive_collaboration",
            "start_message_id": 28,
            "end_message_id": 30,
        },
    ]
    audit = db.query_one(
        """
        SELECT after_value
        FROM audit_logs
        WHERE action_type='collaboration_state_finalization.succeeded'
          AND target_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (result["finalization_id"],),
    )
    metadata = json.loads(audit["after_value"])
    assert metadata["saved_segment_ids"] == segment_results["saved_segment_ids"]
    assert metadata["rejected_segment_orders"] == [2]
    assert metadata["rejection_reasons"] == [
        "state_segment_evidence_not_student"
    ]
