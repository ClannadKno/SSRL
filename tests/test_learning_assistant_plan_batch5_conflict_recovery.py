# -*- coding: utf-8 -*-
"""Plan batch 5: conflict recovery and later positive collaboration."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


BASE_TIME = datetime(2026, 7, 24, 4, 0, 0)


def _rows(contents, speakers=None, roles=None):
    speakers = speakers or [(index % 4) + 1 for index in range(len(contents))]
    roles = roles or ["student"] * len(contents)
    return [
        {
            "id": index + 1,
            "sequence": index + 1,
            "role": roles[index],
            "sender_type": roles[index],
            "user_id": speakers[index],
            "username": f"s{speakers[index]}",
            "real_name": f"S{speakers[index]}",
            "content": content,
            "created_at": (
                BASE_TIME + timedelta(seconds=index * 20)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for index, content in enumerate(contents)
    ]


def _detect(contents, speakers=None, roles=None):
    from services.feature_service import extract_group_features
    from services.rule_state_service import detect_group_state_rule

    rows = _rows(contents, speakers=speakers, roles=roles)
    student_rows = [row for row in rows if row["role"] == "student"]
    context = {
        "window_start": BASE_TIME.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": (
            BASE_TIME + timedelta(seconds=max(120, len(rows) * 20))
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "window_minutes": 4,
        "window_messages": rows,
        "window_student_messages": student_rows,
        "low_window_student_messages": student_rows,
        "recent_student_messages": student_rows,
        "recent_checkins": [],
        "checkin_summary": {},
        "page_activity": {
            "active_students": 4,
            "active_duration_seconds": 240,
        },
        "participant_count": 4,
        "last_student_message_time": student_rows[-1]["created_at"],
        "current_task": {
            "title": "方案论证",
            "question": "比较方案证据并形成结论",
            "key_concepts": ["方案", "证据", "结论"],
            "expected_dimensions": ["分工", "比较", "总结"],
        },
    }
    return detect_group_state_rule(context, extract_group_features(context))


def test_repair_phrases_do_not_refresh_destructive_conflict():
    from knowledge_base import count_destructive_conflict_hits

    assert count_destructive_conflict_hits("先别争，我们争取按时完成。") == 0
    assert count_destructive_conflict_hits("停止争论，先看证据。") == 0
    assert count_destructive_conflict_hits("先别争，你这个方案不合理。") >= 1


def test_one_repair_message_enters_observation_without_erasing_conflict():
    from services.discussion_pipeline_v2.decision_fusion import DecisionFusion

    result = _detect(
        [
            "这个方案不合理。",
            "你错了，结论根本不对。",
            "别乱说，你没有看清任务要求。",
            "先别争，我们先冷静一下。",
        ],
        [1, 2, 3, 4],
    )

    assert result["winning_state_code"] == "conflict_tension"
    recent = result["signals"]["recent_conflict"]
    assert recent["has_conflict_history"] is True
    assert recent["recovery_phase"] == "observing"
    assert recent["recovery_completed"] is False
    assert result["self_regulation_detected"] is True
    assert 4 not in result["evidence"]["conflict_tension"]

    fusion = DecisionFusion.fuse(result)
    assert fusion["fused_state_code"] == "conflict_tension"
    assert fusion["self_regulation_detected"] is True
    assert fusion["should_intervene"] is False


def test_recovery_observation_uses_explicit_strategy_skip_reason():
    from services.discussion_pipeline_v2.monitoring_service import (
        _build_state_handling_decision,
    )

    decision = _build_state_handling_decision(
        final_state="conflict_tension",
        assessment_status="confirmed",
        decision_source="rule_high_confidence_fallback",
        confidence=0.82,
        trigger_type="new_message",
        persist_state_segment=True,
        schedule_strategy=True,
        should_call=False,
        call_reason="autonomous_regulation_observed",
        llm_result=None,
        evidence_sequences=[1, 2],
        new_student_sequences=[3],
        autonomous_regulation_observed=True,
    )

    assert decision["should_persist"] is False
    assert decision["persist_reason"] == "no_new_supporting_evidence"
    assert decision["should_schedule_strategy"] is False
    assert decision["schedule_reason"] == "autonomous_regulation_observed"


def test_complete_multi_member_recovery_becomes_positive_and_keeps_boundaries():
    result = _detect(
        [
            "这个方案不合理。",
            "你错了，这个判断太片面。",
            "别乱说，你没有看清任务要求。",
            "先别争，我们制定比较标准。",
            "我负责整理第一组证据。",
            "我补充第二组数据并比较方案。",
            "同意，我来汇总风险。",
            "最后形成结论并总结一下。",
        ],
        [1, 2, 3, 4, 1, 2, 3, 4],
    )

    assert result["winning_state_code"] == "positive_collaboration"
    recent = result["signals"]["recent_conflict"]
    assert recent["has_conflict_history"] is True
    assert recent["has_recent_conflict"] is False
    assert recent["recovery_phase"] == "completed"
    assert recent["recovery_speaker_count"] == 4
    assert {
        "deescalation",
        "task_structuring",
        "evidence_comparison",
        "consensus_summary",
    } <= set(recent["recovery_signal_categories"])
    assert set(result["evidence"]["conflict_tension"]) == {1, 2, 3}
    assert set(result["evidence"]["positive_collaboration"]) >= {4, 5, 6, 8}


def test_surface_repair_followed_by_attack_remains_conflict():
    result = _detect(
        [
            "这个方案不合理。",
            "先别争，我们先看依据。",
            "你错了，还是别乱说。",
        ],
        [1, 2, 3],
    )

    assert result["winning_state_code"] == "conflict_tension"
    recent = result["signals"]["recent_conflict"]
    assert recent["recovery_phase"] == "none"
    assert recent["self_regulation_detected"] is False


def test_constructive_difference_is_not_tense_conflict():
    result = _detect(
        [
            "我理解你的方案，不过可以换个角度先看依据。",
            "可以结合两种方案，比较证据后再判断。",
            "我负责整理数据，你负责列出风险。",
            "同意，最后形成共同结论。",
        ],
        [1, 2, 3, 4],
    )

    assert result["winning_state_code"] == "positive_collaboration"
    assert result["signals"]["recent_conflict"]["has_recent_conflict"] is False


def test_agent_message_does_not_change_student_recovery_timeline():
    result = _detect(
        [
            "这个方案不合理。",
            "你错了，别乱说。",
            "先别争，我们制定比较标准。",
            "你们的讨论还是一团糟。",
            "我负责整理证据。",
            "我补充数据并比较方案。",
            "最后形成结论。",
        ],
        [1, 2, 3, 99, 1, 2, 3],
        ["student", "student", "student", "agent", "student", "student", "student"],
    )

    assert result["winning_state_code"] == "positive_collaboration"
    recent = result["signals"]["recent_conflict"]
    assert recent["recovery_phase"] == "completed"
    assert 4 not in recent["recovery_evidence_message_ids"]


def _insert_message(db, context, content, *, member_index=0, role="student"):
    return db.create_message(
        context["group_id"],
        context["students"][member_index][0],
        content,
        role=role,
        session_no=context["session_no"],
        task_id=context["task_id"],
        created_at=db.now_str(),
    )


def test_monitoring_preserves_conflict_then_writes_positive_recovery_with_agent_inside(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=850, member_count=4)
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy', strategy_agent_enabled=1,
               emotion_agent_enabled=0, agent_intervention_enabled=1
         WHERE id=?
        """,
        (context["session_id"],),
    )

    import services.discussion_pipeline_v2.monitoring_service as monitoring

    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring, "AUTO_INTERVENTION_V2_ENABLED", True)
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

    for index, content in enumerate(
        [
            "这个方案不合理。",
            "你错了，这个判断太片面。",
            "别乱说，你没有看清任务要求。",
        ]
    ):
        _insert_message(db, context, content, member_index=index)
    db.execute(
        "UPDATE groups SET cutoff_sequence=last_message_sequence WHERE id=?",
        (context["group_id"],),
    )
    conflict_result = monitoring.MonitoringService.run_detection(
        context["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )
    assert conflict_result["fused_state"] == "conflict_tension"

    recovery_start = _insert_message(
        db,
        context,
        "先别争，我们制定比较标准。",
        member_index=3,
    )
    agent_message = _insert_message(
        db,
        context,
        "请继续围绕证据和比较标准推进。",
        member_index=0,
        role="agent",
    )
    for index, content in enumerate(
        [
            "我负责整理第一组证据。",
            "我补充第二组数据并比较方案。",
            "同意，我来汇总风险。",
            "最后形成结论并总结一下。",
        ]
    ):
        _insert_message(db, context, content, member_index=index)
    db.execute(
        "UPDATE groups SET cutoff_sequence=last_message_sequence WHERE id=?",
        (context["group_id"],),
    )
    recovery_result = monitoring.MonitoringService.run_detection(
        context["group_id"],
        trigger_type="new_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    assert recovery_result["fused_state"] == "positive_collaboration"
    segments = [
        dict(row)
        for row in db.query_all(
            """
            SELECT state_code, start_message_id, end_message_id, evidence_sequences
            FROM collaboration_state_segments
            WHERE group_id=? AND source='state_monitor'
            ORDER BY start_message_id, id
            """,
            (context["group_id"],),
        )
    ]
    assert [row["state_code"] for row in segments] == [
        "conflict_tension",
        "positive_collaboration",
    ]
    positive = segments[1]
    assert positive["start_message_id"] == recovery_start["sequence"]
    assert positive["start_message_id"] < agent_message["sequence"]
    assert positive["end_message_id"] > agent_message["sequence"]
    assert str(agent_message["sequence"]) not in positive["evidence_sequences"]
    assert len(scheduled) == 0
