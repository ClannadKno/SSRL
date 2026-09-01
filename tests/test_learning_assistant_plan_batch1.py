# -*- coding: utf-8 -*-
"""Batch 1 regression baseline for the message-flow repair plan.

The strict xfails describe the intended behavior.  They must be removed, not
weakened, when the corresponding implementation batch makes the assertion pass.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta

from tests.helpers import seed_running_session


def _time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _seed_completed_help(db, context: dict) -> int:
    """Create a completed frustration help request covering sequences 21-23."""
    student_id = context["students"][0][0]
    source_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            student_id,
            "这里卡住了，请帮忙看看。",
            21,
            "student",
            "student",
            context["session_no"],
            context["task_id"],
            context["session_id"],
            db.now_str(),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=25, cutoff_sequence=25
        WHERE id=?
        """,
        (context["group_id"],),
    )
    intervention_run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, task_id, cutoff_sequence, status,
            trigger_type, detected_state, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["task_id"],
            21,
            "PUBLISHED",
            "student_help_request",
            "blocked_frustration",
            db.now_str(),
            db.now_str(),
        ),
    )
    return db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, request_text, source_message_id,
            help_request_message_sequence, covered_until_sequence,
            intervention_run_id, handling_status, handled_at,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,'COMPLETED',?,?,?,?,?,'handled',?,?,?)
        """,
        (
            context["group_id"],
            student_id,
            context["task_id"],
            context["session_no"],
            context["session_id"],
            "这里卡住了，请帮忙看看。",
            source_message_id,
            21,
            23,
            intervention_run_id,
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )


def _help_candidate(context: dict, *, state_code: str, start: int, end: int) -> dict:
    return {
        "batch": {
            "group_id": context["group_id"],
            "session_id": context["session_id"],
        },
        "segment": {
            "id": 901,
            "state_code": state_code,
            "start_sequence": start,
            "end_sequence": end,
            "evidence_sequences": [end],
        },
    }


def test_completed_help_still_blocks_the_same_frustration_issue(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=801, member_count=1)
    help_request_id = _seed_completed_help(db, context)

    from services.assessment_batch_intervention_service import _help_guard

    conn = db.db()
    try:
        guard = _help_guard(
            conn,
            _help_candidate(
                context,
                state_code="blocked_frustration",
                start=21,
                end=23,
            ),
        )
    finally:
        conn.close()

    assert guard["allowed"] is False
    assert guard["help_request_id"] == help_request_id


def test_completed_frustration_help_does_not_block_a_new_conflict(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=802, member_count=1)
    _seed_completed_help(db, context)

    from services.assessment_batch_intervention_service import _help_guard

    conn = db.db()
    try:
        guard = _help_guard(
            conn,
            _help_candidate(
                context,
                state_code="conflict_tension",
                start=21,
                end=25,
            ),
        )
    finally:
        conn.close()

    assert guard["allowed"] is True


def test_185_second_silence_is_persisted_and_enters_strategy_schedule(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=803, member_count=1, limit_minutes=30)
    now = datetime.now().replace(microsecond=0)

    from services.group_discussion_runtime_service import enter_group_discussion_stage

    runtime = enter_group_discussion_stage(
        context["session_id"],
        context["group_id"],
        context["students"][0][0],
    )
    db.execute(
        """
        UPDATE group_session_discussions
        SET status='running', started_at=?, deadline=?, updated_at=?
        WHERE id=?
        """,
        (
            _time(now - timedelta(minutes=5)),
            _time(now + timedelta(minutes=20)),
            _time(now),
            runtime["id"],
        ),
    )
    message = db.create_message(
        context["group_id"],
        context["students"][0][0],
        "我们先想一想。",
        role="student",
        created_at=_time(now - timedelta(seconds=185)),
        client_message_id="plan-batch1-silence",
    )

    import services.discussion_pipeline_v2.monitoring_service as monitoring

    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring, "AUTO_INTERVENTION_V2_ENABLED", True)
    rule_result = {
        "version": "plan_batch1_baseline",
        "assessment_status": "state_detected",
        "winning_state_code": "negative_silence",
        "winning_state_label": "消极沉默",
        "winning_score": 0.9,
        "candidates": [
            {
                "state_code": "negative_silence",
                "score": 0.9,
                "signals": ["silent_seconds=185"],
            }
        ],
    }
    monkeypatch.setattr(
        monitoring.RuleDetector,
        "detect",
        staticmethod(lambda _context, _features: rule_result),
    )
    monkeypatch.setattr(
        monitoring.TriggerPolicy,
        "should_enqueue_strategy_review",
        staticmethod(lambda *_args, **_kwargs: (True, "negative_silence_confirmed")),
    )
    monkeypatch.setattr(
        monitoring,
        "_silence_strategy_precheck",
        lambda **_kwargs: {
            "allowed": True,
            "reason": "silence_strategy_precheck_passed",
        },
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

    from agent.monitoring_tasks import check_room_silence

    result = check_room_silence.call_local(
        context["group_id"],
        message["sequence"],
    )

    assert result.get("error") is None
    assert result["fused_state"] == "negative_silence"
    segment_count = db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=? AND state_code='negative_silence'
        """,
        (context["group_id"], context["session_id"]),
    )["c"]
    assert (segment_count, len(scheduled)) == (1, 0)


def test_emotion_e2_prompt_excludes_student_evidence_and_canonical_state():
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    evidence = {
        "sequence": 32,
        "created_at": "2026-07-23 16:00:08",
        "member_label": "成员3",
        "content": "我还是不知道这条证据怎么支持结论。",
    }
    context = {
        "task_title": "证据论证任务",
        "task_question": "说明证据如何支持结论",
        "session_no": 1,
        "recent_messages": [evidence],
        "recent_student_messages": [evidence],
        "message_count": 1,
        "message_summary": "最近1条讨论消息",
        "interaction_summary": "讨论较为安静",
        "state_summary": "挫败卡住",
        "latest_group_state": {
            "state_code": "blocked_frustration",
            "state_label": "挫败卡住",
        },
        "participation_feedback": {
            "feedback_type_code": "GROUP_LOW_PARTICIPATION",
            "feedback_type_label": "群体低参与",
            "comparison_summary": "当前窗口整体参与较少。",
            "current_window_summary": "小组交流尚未充分展开。",
            "reference_templates": [
                {
                    "template_id": "E2-LOW-TEST",
                    "text": "小组暂时还比较安静，大家不必有压力。🌿",
                }
            ],
        },
        "recent_emotion_feedbacks": [],
    }

    _system_prompt, user_prompt = EmotionReflectionService.build_prompt(context)

    assert evidence["content"] not in user_prompt
    assert str(evidence["sequence"]) not in user_prompt
    assert evidence["created_at"] not in user_prompt
    assert "blocked_frustration" not in user_prompt
    assert set(json.loads(user_prompt)) == {
        "feedback_state",
        "comparison_summary",
        "current_window_summary",
        "reference_templates",
        "recent_emotion_messages",
    }


def test_emotion_validator_allows_plural_group_pronoun():
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    result = EmotionReflectionService.validate_message(
        "你们正在认真交流不同看法，保持这样的投入就很好。🌿"
    )

    assert result["valid"] is True


def test_positive_context_never_selects_a_negative_fallback(monkeypatch):
    import services.emotion_agent.emotion_reflection_service as emotion

    monkeypatch.setattr(
        emotion.random,
        "choice",
        lambda messages: next(
            (
                message
                for message in messages
                if "现在的节奏可能有些紧" in message
            ),
            messages[0],
        ),
    )
    fallback_method = emotion.EmotionReflectionService._get_fallback
    if inspect.signature(fallback_method).parameters:
        message = fallback_method("positive_collaboration")
    else:
        message = fallback_method()

    negative_markers = ("卡顿", "节奏可能有些紧", "停顿", "别着急", "放轻松")
    assert not any(marker in message for marker in negative_markers)
