# -*- coding: utf-8 -*-
"""Regression tests for assistant recency and stale-context handling."""

from datetime import datetime, timedelta

from tests.helpers import attach_state_assessment_to_monitor, create_group, create_student


def test_review_message_states_do_not_backfill_detector_window():
    from services.teacher_emotion_review_service import _assign_state_to_messages

    messages = [
        {
            "role": "student",
            "created_at": "2026-01-01 10:00:00",
            "sequence": 1,
        },
        {
            "role": "student",
            "created_at": "2026-01-01 10:04:45",
            "sequence": 2,
        },
        {
            "role": "student",
            "created_at": "2026-01-01 10:06:00",
            "sequence": 3,
        },
    ]
    state_segments = [
        {
            "id": 10,
            "state_code": "task_detached",
            "canonical_sub_state_code": "off_topic_unregulated",
            "state_label": "任务脱离",
            "start_message_id": 2,
            "end_message_id": 2,
            "start_at": "2026-01-01 10:04:45",
            "end_at": "2026-01-01 10:04:45",
            "evidence_message_ids": [2],
            "confidence": 0.8,
            "source": "strategy_llm",
            "is_finalized": True,
        }
    ]

    _assign_state_to_messages(messages, state_segments)

    assert messages[0]["state_code"] == "observing"
    assert messages[1]["state_code"] == "off_topic_unregulated"
    assert messages[2]["state_code"] == "off_topic_unregulated"
    assert messages[2]["inferred"] is True


def test_review_summary_uses_latest_explicit_segment_state():
    from services.teacher_emotion_review_service import _summary

    messages = [
        {
            "role": "student",
            "user_id": 1,
            "created_at": "2026-01-01 10:42:00",
            "state_code": "positive_collaboration",
            "state_label": "Positive",
            "state_confidence": 0.72,
        },
        {
            "role": "agent",
            "created_at": "2026-01-01 10:45:00",
            "state_code": "negative_silence",
            "state_label": "Silence",
            "state_confidence": 0.8,
        },
    ]
    trend_summary = {
        "latest_state_code": "positive_collaboration",
        "latest_state_label": "积极协作",
        "latest_state_confidence": 0.72,
        "latest_state_source": "latest_state_segment",
    }

    summary = _summary(messages, [], [], [], trend_summary)

    assert summary["latest_state_code"] == "positive_collaboration"
    assert summary["latest_state_source"] == "latest_state_segment"


def test_emotion_context_uses_newest_student_messages(db_and_app):
    db, _app_module, _client = db_and_app
    group_id = create_group(db, name="Emotion Recent Group", code="G-EMO-RECENT")
    student_id, _login_key = create_student(db, group_id)
    base = datetime(2026, 1, 1, 10, 0, 0)

    for index in range(8):
        created_at = (base + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S")
        db.create_message(
            group_id,
            student_id,
            f"msg-{index}",
            role="student",
            client_message_id=f"recent-{index}",
            created_at=created_at,
        )

    from services.emotion_agent.emotion_reflection_service import EmotionReflectionService

    rows = EmotionReflectionService._get_recent_messages(
        group_id,
        "2026-01-01 10:00:00",
        "2026-01-01 10:10:00",
        max_messages=3,
        max_chars=120,
    )

    assert [row["content"] for row in rows] == ["msg-5", "msg-6", "msg-7"]


def test_strategy_intervention_skips_stale_cutoff_without_publishing(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    group_id = create_group(db, name="Stale Strategy Group", code="G-STALE")
    student_id, _login_key = create_student(db, group_id)
    first = db.create_message(
        group_id,
        student_id,
        "old conflict",
        role="student",
        client_message_id="stale-old",
    )
    db.create_message(
        group_id,
        student_id,
        "new repair",
        role="student",
        client_message_id="stale-new",
    )
    cutoff = first["sequence"]
    now = db.now_str()
    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, final_state, confidence,
            status, analyzer_version, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            cutoff,
            "new_message",
            "conflict_tension",
            0.86,
            "completed",
            "test",
            now,
            now,
        ),
    )
    attach_state_assessment_to_monitor(
        db,
        monitor_run_id,
        group_id=group_id,
        state_code="conflict_tension",
        confidence=0.86,
        cutoff_sequence=cutoff,
    )

    import services.intervention_pipeline_v2.intervention_service as service_module
    import services.intervention_pipeline_v2.agent_research_helper as research_helper
    import services.session_lifecycle as session_lifecycle
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    monkeypatch.setattr(research_helper, "check_strategy_agent_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(session_lifecycle, "check_agent_allowed", lambda *_args, **_kwargs: (True, None))
    monkeypatch.setattr(
        service_module,
        "review_strategy_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale run called strategy review LLM")),
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["skipped"] is True
    assert result["reason"] == "stale_assessment"
    run = db.query_one(
        "SELECT status, decision, skip_reason FROM intervention_runs WHERE group_id=?",
        (group_id,),
    )
    assert dict(run) == {
        "status": "STALE",
        "decision": "SKIPPED",
        "skip_reason": "stale_assessment",
    }
    assert db.query_all("SELECT * FROM messages WHERE group_id=? AND role='agent'", (group_id,)) == []


def test_recent_interventions_include_generated_message_for_duplicate_avoidance(db_and_app):
    db, _app_module, _client = db_and_app
    group_id = create_group(db, name="Duplicate Avoidance Group", code="G-DUP")
    now = db.now_str()
    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, trigger_type, status,
            strategy_id, generated_message, fallback_used, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            3,
            "strategy",
            "new_message",
            "PUBLISHED",
            "v2_offtask_next_action",
            "previous visible strategy message",
            0,
            now,
            now,
        ),
    )

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    recent = ContextBuilder._get_recent_interventions(group_id)

    assert recent[0]["generated_message"] == "previous visible strategy message"
