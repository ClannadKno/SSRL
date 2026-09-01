# -*- coding: utf-8 -*-
"""Batch 5 isolation tests for canonical state-only monitoring."""

from __future__ import annotations

import json

import pytest

from tests.helpers import seed_running_session


def _state_only_scope(db, *, session_no: int, agent_mode: str, research: bool):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    context = seed_running_session(db, session_no=session_no, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
        SET agent_mode=?,
            strategy_agent_enabled=?,
            emotion_agent_enabled=?,
            agent_intervention_enabled=?,
            research_state_monitoring_enabled=?
        WHERE id=?
        """,
        (
            agent_mode,
            1 if agent_mode == "strategy" else 0,
            1 if agent_mode == "emotion" else 0,
            1 if agent_mode == "strategy" else 0,
            1 if research else 0,
            context["session_id"],
        ),
    )
    discussion = enter_group_discussion_stage(
        context["session_id"],
        context["group_id"],
        context["students"][0][0],
    )
    context["discussion_id"] = discussion["id"]
    message = db.create_message(
        context["group_id"],
        context["students"][0][0],
        "我们还在比较两种方案的依据。",
        role="student",
        session_no=context["session_no"],
        task_id=context["task_id"],
    )
    context["message"] = message
    return context


def _stage2_payload(sequence: int):
    return {
        "schema_version": "stage2.v1",
        "analysis_scope": {
            "candidate_start_sequence": sequence,
            "candidate_end_sequence": sequence,
            "input_cutoff_student_sequence": sequence,
        },
        "segments": [
            {
                "raw_sub_state": "frustration",
                "canonical_sub_state": "frustration",
                "secondary_tags": ["high_intensity_overload"],
                "start_sequence": sequence,
                "end_sequence": sequence,
                "confidence": 0.87,
                "evidence_message_ids": [sequence],
                "reason": "小组仍在尝试，但明确表达负荷较高。",
                "is_active_at_window_end": True,
                "detected_self_regulation": False,
            }
        ],
        "active_segment_index": 0,
        "active_sub_state": {
            "raw_sub_state": "frustration",
            "canonical_sub_state": "frustration",
            "secondary_tags": ["high_intensity_overload"],
            "start_sequence": sequence,
            "end_sequence": sequence,
            "confidence": 0.87,
            "evidence_message_ids": [sequence],
            "reason": "小组仍在尝试，但明确表达负荷较高。",
            "detected_self_regulation": False,
        },
        # State-only persistence must ignore all strategy-bearing model fields.
        "should_intervene": True,
        "candidate_strategy_ids": ["v2_frustration_identify"],
        "inhibition": {
            "is_inhibited": False,
            "strategy_id": None,
            "reason": None,
        },
        "decision_reason": "canonical state assessment only",
    }


def _seed_stage1(db, context: dict):
    sequence = context["message"]["sequence"]
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, pipeline_mode,
            group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority,
            input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence,
            stage1_status, stage1_started_at, stage1_completed_at,
            coarse_decision, coarse_state_code, coarse_should_escalate,
            coarse_confidence, coarse_evidence_message_ids_json,
            stage2_status, publish_status, final_status,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"batch5-stage1-{context['session_id']}",
            "state_only",
            context["group_id"],
            context["session_id"],
            context["session_no"],
            context["discussion_id"],
            context["task_id"],
            "new_message",
            200,
            sequence,
            sequence,
            sequence,
            "SUCCEEDED",
            db.now_str(),
            db.now_str(),
            "ESCALATE",
            "POSSIBLE_BLOCKED",
            1,
            0.87,
            json.dumps([context["message"]["id"]]),
            "PENDING",
            "NOT_READY",
            "PENDING_STAGE2",
            f"batch5-stage1:{context['session_id']}:{sequence}",
            db.now_str(),
            db.now_str(),
        ),
    )


@pytest.mark.parametrize(
    ("agent_mode", "research", "session_no"),
    (("emotion", False, 1501), ("none", True, 1502)),
)
def test_state_only_runs_stage1_and_stage2_without_strategy_side_effects(
    db_and_app, monkeypatch, agent_mode, research, session_no
):
    db, _app, _client = db_and_app
    context = _state_only_scope(
        db, session_no=session_no, agent_mode=agent_mode, research=research
    )
    pipeline_id = _seed_stage1(db, context)

    import services.state_assessment_scheduler as scheduler
    from services.discussion_pipeline_v2.monitoring_service import MonitoringService
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
    from services.three_stage_stage3 import Stage3PipelineService

    monkeypatch.setattr(scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 1)
    monkeypatch.setattr(scheduler, "_enqueue_batch", lambda *_args, **_kwargs: None)
    calls = []

    def fake_detection(**kwargs):
        calls.append(kwargs)
        payload = _stage2_payload(context["message"]["sequence"])
        return {
            "monitor_run_id": 501,
            "state_llm_result": payload,
            "state_llm_meta": {
                "success": True,
                "analysis_failed": False,
                "analysis_skipped": False,
                "model_name": "batch5-state-model",
                "model_version": "test",
                "prompt_version": "batch5-state-only",
                "raw_response": json.dumps(payload, ensure_ascii=False),
            },
        }

    monkeypatch.setattr(MonitoringService, "run_detection", staticmethod(fake_detection))
    monkeypatch.setattr(
        RoomLeaseService,
        "claim_strategy_pipeline",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("state-only must not claim the strategy room lease")
            )
        ),
    )
    monkeypatch.setattr(
        Stage3PipelineService,
        "execute_for_pipeline",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("state-only must not run Stage 3")
            )
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "record_observation_assessment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("state-only must not enter strategy observation")
        ),
    )

    requested = scheduler.request_state_assessment(
        group_id=context["group_id"],
        session_id=context["session_id"],
        discussion_id=context["discussion_id"],
        trigger_type="message_count_periodic",
        trigger_sequence=context["message"]["sequence"],
    )
    assert requested["pipeline_mode"] == "state_only"
    outcome = scheduler.execute_state_assessment_batch(
        requested["assessment_batch_id"]
    )

    assert outcome["succeeded"] is True
    assert calls[0]["pipeline_mode"] == "state_only"
    assert calls[0]["schedule_strategy"] is False
    assert outcome["stage2_initial_lease"] is None
    assert outcome["stage2_pipeline"]["should_enter_stage3"] is False
    assert outcome["intervention"]["reason"] == "state_only_completed"

    pipeline = db.query_one(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,)
    )
    assert pipeline["pipeline_mode"] == "state_only"
    assert pipeline["stage1_status"] == "SUCCEEDED"
    assert pipeline["stage2_status"] == "SUCCEEDED"
    assert pipeline["stage3_status"] == "SKIPPED"
    assert pipeline["canonical_sub_state_code"] == "frustration"
    assert json.loads(pipeline["secondary_sub_state_tags_json"]) == [
        "high_intensity_overload"
    ]
    assert pipeline["sub_state_confidence"] == pytest.approx(0.87)
    assert json.loads(pipeline["sub_state_evidence_message_ids_json"]) == [
        context["message"]["sequence"]
    ]
    assert pipeline["sub_state_start_sequence"] == context["message"]["sequence"]
    assert pipeline["sub_state_end_sequence"] == context["message"]["sequence"]
    assert pipeline["assessment_batch_id"] == requested["assessment_batch_id"]
    assert pipeline["should_intervene"] is None
    assert json.loads(pipeline["strategy_candidate_ids_json"]) == []
    assert json.loads(pipeline["strategy_pool_json"]) == []
    assert pipeline["selected_strategy_id"] is None
    assert pipeline["room_lock_token"] is None
    assert pipeline["publish_status"] == "SKIPPED"
    assert pipeline["final_status"] == "STATE_ONLY_COMPLETED"

    segment = db.query_one(
        "SELECT * FROM collaboration_state_segments WHERE assessment_batch_id=?",
        (requested["assessment_batch_id"],),
    )
    assert segment["canonical_sub_state_code"] == "frustration"
    assert json.loads(segment["secondary_tags_json"]) == [
        "high_intensity_overload"
    ]
    assert segment["should_intervene"] is None
    assert segment["selected_strategy_id"] is None
    assert db.query_all(
        "SELECT * FROM intervention_runs WHERE group_id=?",
        (context["group_id"],),
    ) == []
    assert db.query_all(
        "SELECT * FROM messages WHERE group_id=? AND role='agent'",
        (context["group_id"],),
    ) == []
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (context["group_id"],),
    )
    assert group["state"] != "AI_INTERVENING"
    assert group["lock_token"] is None
    assert group["active_intervention_run_id"] is None


def test_none_without_research_monitoring_does_not_create_a_batch(db_and_app):
    db, _app, _client = db_and_app
    context = _state_only_scope(
        db, session_no=1503, agent_mode="none", research=False
    )

    from services.state_assessment_scheduler import request_state_assessment

    result = request_state_assessment(
        group_id=context["group_id"],
        session_id=context["session_id"],
        discussion_id=context["discussion_id"],
        trigger_type="message_count_periodic",
        trigger_sequence=context["message"]["sequence"],
    )
    assert result["skipped"] is True
    assert result["reason"] == "state_monitoring_disabled"
    assert db.query_all(
        "SELECT * FROM state_assessment_batches WHERE session_id=?",
        (context["session_id"],),
    ) == []


def test_research_monitoring_switch_is_independent_of_agent_mode(db_and_app):
    db, _app, _client = db_and_app
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, created_at) VALUES(?,?,?)",
        ("State-only research", "Discuss", db.now_str()),
    )

    from services.agent_mode_service import pipeline_mode_from_session
    from services.teacher_session_service import (
        create_session,
        update_session_agent_config,
    )

    session = create_session(
        operator_id=1,
        session_no=1504,
        task_id=task_id,
        agent_mode="none",
        research_state_monitoring_enabled=True,
    )
    assert session["agent_mode"] == "none"
    assert session["research_state_monitoring_enabled"] is True
    assert session["state_monitoring_enabled"] is True
    assert pipeline_mode_from_session(session) == "state_only"

    disabled = update_session_agent_config(
        session_id=session["id"],
        operator_id=1,
        research_state_monitoring_enabled=False,
    )
    assert disabled["agent_mode"] == "none"
    assert disabled["research_state_monitoring_enabled"] is False
    assert disabled["state_monitoring_enabled"] is False
    assert pipeline_mode_from_session(disabled) is None


def test_emotion_e1_prompt_is_invariant_to_canonical_state():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = {
        "emotion_slot_index": 2,
        "task_title": "证据比较",
        "task_question": "哪项主张更可靠？",
        "previous_student_messages": [
            {
                "id": 1,
                "created_at": "2026-08-03 10:00:00",
                "member_label": "成员A",
                "content": "我先列出第一项依据。",
            }
        ],
        "current_student_messages": [
            {
                "id": 2,
                "created_at": "2026-08-03 10:05:00",
                "member_label": "成员B",
                "content": "我补充第二项依据并比较来源。",
            }
        ],
        "participation_metrics": {
            "previous_window_start": "2026-08-03 10:00:00",
            "previous_window_end": "2026-08-03 10:05:00",
            "current_window_start": "2026-08-03 10:05:00",
            "current_window_end": "2026-08-03 10:10:00",
            "previous_metrics": {"message_count": 1},
            "current_metrics": {"message_count": 1},
        },
        "recent_emotion_feedbacks": [],
    }
    first = {
        **context,
        "canonical_sub_state_code": "frustration",
        "canonical_overlay": ["high_intensity_overload"],
        "canonical_evidence_message_ids": [2],
    }
    second = {
        **context,
        "canonical_sub_state_code": "interpersonal_conflict",
        "canonical_overlay": ["psychological_safety_risk"],
        "canonical_evidence_message_ids": [1, 2],
    }

    assert EmotionFeedbackClassifier.build_prompt(
        first
    ) == EmotionFeedbackClassifier.build_prompt(second)
