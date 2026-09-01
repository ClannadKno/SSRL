# -*- coding: utf-8 -*-
"""Batch 5 regressions for emotion context and dominant-state fusion."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


def _ts(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _ready_scope(db, *, session_no: int, member_count: int = 2):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    context = seed_running_session(
        db,
        session_no=session_no,
        member_count=member_count,
        limit_minutes=30,
    )
    runtime = None
    for user_id, _login_key in context["students"]:
        runtime = enter_group_discussion_stage(
            context["session_id"], context["group_id"], user_id
        )
    return context, runtime["id"]


def _student_message(
    db,
    context: dict,
    *,
    content: str,
    created_at: datetime,
    member_index: int = 0,
):
    return db.create_message(
        context["group_id"],
        context["students"][member_index][0],
        content,
        role="student",
        session_no=context["session_no"],
        task_id=context["task_id"],
        created_at=_ts(created_at),
    )


def _monitor_segment(
    db,
    context: dict,
    *,
    message: dict,
    state_code: str,
    detected_at: datetime,
    confidence: float = 0.84,
):
    timestamp = _ts(detected_at)
    return db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id,
            state_code, segment_kind,
            start_message_id, end_message_id,
            start_sequence, end_sequence,
            detected_at,
            evidence_message_ids_json, evidence_sequences,
            confidence, source, assessment_status,
            is_finalized, dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["session_no"],
            context["task_id"],
            state_code,
            "message_range",
            message["id"],
            message["id"],
            message["sequence"],
            message["sequence"],
            timestamp,
            f"[{message['id']}]",
            f"[{message['sequence']}]",
            confidence,
            "state_monitor",
            "confirmed",
            1,
            (
                f"batch5-monitor:{context['group_id']}:{context['session_id']}:"
                f"{state_code}:{message['sequence']}"
            ),
            timestamp,
            timestamp,
        ),
    )


def _batch(
    db,
    context: dict,
    discussion_id: int,
    *,
    start_sequence: int,
    end_sequence: int,
    completed_at: datetime,
    segments: list[dict],
):
    timestamp = _ts(completed_at)
    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            context_start_sequence, context_end_sequence,
            trigger_type, trigger_sequence, window_key,
            status, completed_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            discussion_id,
            start_sequence,
            end_sequence,
            start_sequence,
            end_sequence,
            "new_message",
            end_sequence,
            (
                f"batch5:{context['group_id']}:{context['session_id']}:"
                f"{start_sequence}:{end_sequence}:{timestamp}"
            ),
            "succeeded",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    for order, segment in enumerate(segments):
        evidence = list(segment["evidence_sequences"])
        db.execute(
            """
            INSERT INTO collaboration_state_segments(
                group_id, session_id, session_no, task_id,
                state_code, segment_kind,
                start_message_id, end_message_id,
                assessment_batch_id, start_sequence, end_sequence,
                evidence_message_ids_json, evidence_sequences,
                confidence, source, assessment_status, segment_order,
                is_active_at_batch_end, trigger_type, is_finalized,
                dedupe_key, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                context["group_id"],
                context["session_id"],
                context["session_no"],
                context["task_id"],
                segment["state_code"],
                "message_range",
                segment["start_sequence"],
                segment["end_sequence"],
                batch_id,
                segment["start_sequence"],
                segment["end_sequence"],
                str(evidence).replace(" ", ""),
                str(evidence).replace(" ", ""),
                segment.get("confidence", 0.9),
                "llm",
                "confirmed",
                order,
                1 if segment.get("active") else 0,
                "new_message",
                1,
                f"batch5-segment:{batch_id}:{order}",
                timestamp,
                timestamp,
            ),
        )
    return batch_id


def test_e2_prompt_excludes_student_evidence_and_canonical_risk_state(
    db_and_app,
):
    db, _app, _client = db_and_app
    context, discussion_id = _ready_scope(db, session_no=1101)
    now = datetime.now().replace(microsecond=0)
    messages = []
    for index in range(6):
        messages.append(
            _student_message(
                db,
                context,
                content=f"第{index + 1}条学生原话",
                created_at=now - timedelta(seconds=60 - index * 8),
                member_index=index % 2,
            )
        )
    _monitor_segment(
        db,
        context,
        message=messages[0],
        state_code="blocked_frustration",
        detected_at=now - timedelta(seconds=4),
    )

    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    built = EmotionReflectionService.build_context(
        group_id=context["group_id"],
        session_id=context["session_id"],
        discussion_id=discussion_id,
        task_id=context["task_id"],
        window_start=_ts(now - timedelta(minutes=2)),
        window_end=_ts(now),
    )
    built["participation_feedback"] = {
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
    }
    _system_prompt, user_prompt = EmotionReflectionService.build_prompt(built)

    assert len(built["recent_student_messages"]) == 6
    assert built["recent_student_messages"][0]["content"] == "第1条学生原话"
    assert {row["member_label"] for row in built["recent_student_messages"]} == {
        "成员1",
        "成员2",
    }
    for message in messages:
        assert message["content"] not in user_prompt
        assert str(message["sequence"]) not in user_prompt
        assert message["created_at"] not in user_prompt
    assert built["dominant_state"]["state_code"] == "blocked_frustration"
    assert "blocked_frustration" not in user_prompt


def test_new_monitor_conflict_overrides_older_batch_frustration(db_and_app):
    db, _app, _client = db_and_app
    context, discussion_id = _ready_scope(db, session_no=1102)
    now = datetime.now().replace(microsecond=0)
    old = _student_message(
        db,
        context,
        content="我还是卡住了。",
        created_at=now - timedelta(seconds=130),
    )
    new = _student_message(
        db,
        context,
        content="我不同意这个判断。",
        created_at=now - timedelta(seconds=15),
        member_index=1,
    )
    _batch(
        db,
        context,
        discussion_id,
        start_sequence=old["sequence"],
        end_sequence=old["sequence"],
        completed_at=now - timedelta(seconds=120),
        segments=[
            {
                "state_code": "frustration_stuck",
                "start_sequence": old["sequence"],
                "end_sequence": old["sequence"],
                "evidence_sequences": [old["sequence"]],
                "active": True,
            }
        ],
    )
    _monitor_segment(
        db,
        context,
        message=new,
        state_code="conflict_tension",
        detected_at=now - timedelta(seconds=8),
    )

    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    fused = EmotionReflectionService._get_latest_dominant_state(
        context["group_id"],
        session_id=context["session_id"],
        discussion_id=discussion_id,
        current_time=_ts(now),
    )

    assert fused["latest_batch_state"]["state_code"] == "blocked_frustration"
    assert fused["latest_monitor_state"]["state_code"] == "conflict_tension"
    assert fused["dominant_state"]["state_code"] == "conflict_tension"
    assert fused["dominant_state"]["state_source"] == "state_monitor"
    assert fused["dominant_state"]["freshness_seconds"] == 8
    assert fused["state_has_recovered"] is False


def test_new_batch_positive_with_inactive_conflict_marks_recovery(db_and_app):
    db, _app, _client = db_and_app
    context, discussion_id = _ready_scope(db, session_no=1103)
    now = datetime.now().replace(microsecond=0)
    conflict = _student_message(
        db,
        context,
        content="这个方案不对。",
        created_at=now - timedelta(seconds=90),
    )
    recovery = _student_message(
        db,
        context,
        content="我们已经把分歧说明白并继续论证了。",
        created_at=now - timedelta(seconds=20),
        member_index=1,
    )
    _monitor_segment(
        db,
        context,
        message=conflict,
        state_code="conflict_tension",
        detected_at=now - timedelta(seconds=85),
    )
    batch_id = _batch(
        db,
        context,
        discussion_id,
        start_sequence=conflict["sequence"],
        end_sequence=recovery["sequence"],
        completed_at=now - timedelta(seconds=10),
        segments=[
            {
                "state_code": "conflict_tension",
                "start_sequence": conflict["sequence"],
                "end_sequence": conflict["sequence"],
                "evidence_sequences": [conflict["sequence"]],
                "active": False,
            },
            {
                "state_code": "positive_collaboration",
                "start_sequence": recovery["sequence"],
                "end_sequence": recovery["sequence"],
                "evidence_sequences": [recovery["sequence"]],
                "active": True,
            },
        ],
    )

    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    fused = EmotionReflectionService._get_latest_dominant_state(
        context["group_id"],
        session_id=context["session_id"],
        discussion_id=discussion_id,
        current_time=_ts(now),
    )

    assert fused["latest_batch_state"]["assessment_batch_id"] == batch_id
    assert fused["latest_batch_state"]["inactive_risk_states"] == [
        "conflict_tension"
    ]
    assert fused["dominant_state"]["state_code"] == "positive_collaboration"
    assert fused["dominant_state"]["state_source"] == "assessment_batch"
    assert fused["state_has_recovered"] is True
    assert fused["recovery_evidence_sequences"] == [recovery["sequence"]]


def test_state_older_than_freshness_window_degrades_to_unknown(db_and_app):
    db, _app, _client = db_and_app
    context, discussion_id = _ready_scope(db, session_no=1104)
    now = datetime.now().replace(microsecond=0)
    message = _student_message(
        db,
        context,
        content="我们刚才有分歧。",
        created_at=now - timedelta(seconds=200),
    )
    _monitor_segment(
        db,
        context,
        message=message,
        state_code="conflict_tension",
        detected_at=now - timedelta(seconds=181),
    )

    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    fused = EmotionReflectionService._get_latest_dominant_state(
        context["group_id"],
        session_id=context["session_id"],
        discussion_id=discussion_id,
        current_time=_ts(now),
    )

    assert fused["latest_monitor_state"]["state_code"] == "conflict_tension"
    assert fused["dominant_state"]["state_code"] == "unknown"
    assert fused["dominant_state"]["state_source"] == "stale"
    assert fused["dominant_state"]["stale_state_code"] == "conflict_tension"
    assert fused["dominant_state"]["freshness_seconds"] == 181
