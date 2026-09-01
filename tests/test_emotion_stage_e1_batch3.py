# -*- coding: utf-8 -*-

import json
import sqlite3
from datetime import timedelta

import pytest

from tests.test_emotion_slot_scheduler_batch6 import (
    _ready_discussion,
    _time,
)


def _messages(start_id, count):
    return [
        {
            "id": start_id + index,
            "sequence": start_id + index,
            "user_id": (index % 4) + 1,
            "member_label": f"成员{(index % 4) + 1}",
            "content": f"围绕任务形成的有效观点 {index + 1}",
            "created_at": f"2026-08-03 12:0{index}:00",
            "low_information_message": False,
        }
        for index in range(count)
    ]


def _metrics(message_count, active_member_count, effective_message_count=None):
    effective = message_count if effective_message_count is None else effective_message_count
    return {
        "message_count": message_count,
        "effective_message_count": effective,
        "active_member_count": active_member_count,
        "effective_char_count": effective * 20,
        "reply_or_response_count": max(0, effective - 1),
        "task_related_message_count": effective,
        "short_acknowledgement_count": message_count - effective,
        "low_information_message_count": message_count - effective,
        "active_minutes": min(message_count, 5),
        "max_member_message_ratio": 0.25 if active_member_count >= 4 else 0.75,
    }


def _context(*, slot_index=2, previous_count=4, current_count=4):
    previous = _messages(101, previous_count)
    current = _messages(201, current_count)
    previous_metrics = _metrics(previous_count, min(previous_count, 4))
    current_metrics = _metrics(current_count, min(current_count, 4))
    return {
        "emotion_slot_id": None,
        "emotion_slot_index": slot_index,
        "task_title": "比较两个方案",
        "task_question": "请小组依据证据比较方案并形成共同观点。",
        "previous_student_messages": previous,
        "current_student_messages": current,
        "recent_student_messages": current,
        "frozen_input_message_ids": [item["id"] for item in previous + current],
        "recent_emotion_feedbacks": [
            {"feedback_state": "GROUP_EXCELLENT", "text": "大家的共同投入值得肯定。"}
        ],
        "participation_metrics": {
            "previous_window_start": "2026-08-03 11:50:00",
            "previous_window_end": "2026-08-03 11:55:00",
            "current_window_start": "2026-08-03 11:55:00",
            "current_window_end": "2026-08-03 12:00:00",
            "previous_metrics": previous_metrics,
            "current_metrics": current_metrics,
        },
    }


def _output(state, context, *, confidence=0.84):
    current_ids = [
        item["id"] for item in context["current_student_messages"][:2]
    ]
    return {
        "feedback_state": state,
        "confidence": confidence,
        "comparison_summary": "当前与上一时间段的群体参与变化符合所选类型。",
        "current_window_summary": "当前窗口存在任务相关观点和成员间的有效承接。",
        "previous_window_summary": "上一窗口的群体参与情况已纳入等长比较。",
        "evidence_message_ids": current_ids,
        "excluded_alternatives": [
            {
                "state": (
                    "GROUP_LOW_PARTICIPATION"
                    if state != "GROUP_LOW_PARTICIPATION"
                    else "GROUP_EXCELLENT"
                ),
                "reason": "当前窗口事实不符合该备选类型。",
            }
        ],
    }


class SequenceGateway:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def call(self, profile_name, payload, response_type="json"):
        from services.llm_gateway import LlmResult

        self.calls.append((profile_name, payload, response_type))
        output = self.outputs.pop(0)
        if isinstance(output, LlmResult):
            return output
        return LlmResult(
            success=True,
            output=output,
            raw_text=json.dumps(output, ensure_ascii=False),
            profile_name=profile_name,
            model_name="emotion-e1-test-model",
        )


@pytest.mark.parametrize(
    "slot_index,previous_count,current_count,state",
    [
        (1, 0, 5, "GROUP_EXCELLENT"),
        (1, 0, 1, "GROUP_LOW_PARTICIPATION"),
        (1, 0, 0, "GROUP_LOW_PARTICIPATION"),
        (2, 2, 7, "GROUP_IMPROVING"),
        (2, 7, 3, "GROUP_DECLINING"),
        (2, 6, 1, "GROUP_LOW_PARTICIPATION"),
        (2, 7, 7, "GROUP_SUSTAINED_EXCELLENT"),
        (2, 4, 4, "GROUP_EXCELLENT"),
    ],
)
def test_stage_e1_accepts_all_planned_classification_scenarios(
    slot_index,
    previous_count,
    current_count,
    state,
):
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context(
        slot_index=slot_index,
        previous_count=previous_count,
        current_count=current_count,
    )
    gateway = SequenceGateway([_output(state, context)])

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["success"] is True
    assert result["feedback_state"] == state
    assert result["feedback_type_code"] == state
    assert result["attempt_count"] == 1


def test_stage_e1_uses_fast_final_json_request_contract():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )
    from services.llm_gateway import BUILTIN_PROFILES

    context = _context()
    gateway = SequenceGateway([_output("GROUP_EXCELLENT", context)])

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["decision_source"] == "llm"
    payload = gateway.calls[0][1]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["_sera_final_content_only"] is True
    assert payload["_sera_compatibility_fallback_fields"] == ["thinking"]
    assert BUILTIN_PROFILES["emotion_feedback_classifier"]["read_timeout"] == 20


def test_first_slot_constraint_is_repaired_once():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context(slot_index=1, previous_count=0, current_count=5)
    invalid = _output("GROUP_IMPROVING", context)
    repaired = _output("GROUP_EXCELLENT", context)
    gateway = SequenceGateway([invalid, repaired])

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["feedback_state"] == "GROUP_EXCELLENT"
    assert result["attempt_count"] == 2
    assert "上次输出不符合 JSON schema" in gateway.calls[1][1]["messages"][1]["content"]


def test_empty_window_cannot_become_a_sixth_or_non_low_state():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context(slot_index=2, previous_count=4, current_count=0)
    invalid = _output("GROUP_DECLINING", context)
    repaired = _output("GROUP_LOW_PARTICIPATION", context)
    gateway = SequenceGateway([invalid, repaired])

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["feedback_state"] == "GROUP_LOW_PARTICIPATION"
    assert result["attempt_count"] == 2


def test_evidence_outside_frozen_windows_requires_schema_repair():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context()
    invalid = _output("GROUP_EXCELLENT", context)
    invalid["evidence_message_ids"] = [999999]
    gateway = SequenceGateway([invalid, _output("GROUP_EXCELLENT", context)])

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["success"] is True
    assert result["attempt_count"] == 2
    assert set(result["evidence_message_ids"]).issubset(
        set(context["frozen_input_message_ids"])
    )


def test_very_low_confidence_gets_one_constrained_rejudgment():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context()
    first = _output("GROUP_EXCELLENT", context, confidence=0.22)
    second = _output("GROUP_EXCELLENT", context, confidence=0.51)
    gateway = SequenceGateway([first, second])

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["success"] is True
    assert result["validation_status"] == "LOW_CONFIDENCE_AFTER_REJUDGMENT"
    assert result["attempt_count"] == 2
    assert "你必须从五类中选择最接近的一类" in gateway.calls[1][1]["messages"][1]["content"]


def test_two_structural_failures_use_deterministic_feedback_state():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context()
    gateway = SequenceGateway(
        [
            {"feedback_state": "GROUP_EXCELLENT"},
            {"feedback_state": "NO_FEEDBACK"},
        ]
    )

    result = EmotionFeedbackClassifier.classify(context, gateway=gateway)

    assert result["success"] is True
    assert result["feedback_state"] == "GROUP_SUSTAINED_EXCELLENT"
    assert result["decision_source"] == "deterministic_fallback"
    assert result["validation_status"] == "DETERMINISTIC_FALLBACK"
    assert result["attempt_count"] == 2


def test_stage_e1_prompt_excludes_state_and_strategy_context():
    from services.emotion_agent.emotion_feedback_classifier import (
        EmotionFeedbackClassifier,
    )

    context = _context()
    context["canonical_sub_state_code"] = "CANONICAL_SECRET"
    context["dominant_state"] = {"state_code": "CANONICAL_SECRET"}
    context["strategy_id"] = "STRATEGY_SECRET"
    context["allowed_strategy_ids"] = ["STRATEGY_SECRET"]

    system_prompt, user_prompt = EmotionFeedbackClassifier.build_prompt(context)
    prompt = system_prompt + user_prompt

    assert "CANONICAL_SECRET" not in prompt
    assert "STRATEGY_SECRET" not in prompt
    assert "strategy_id" not in prompt


def test_success_persists_state_confidence_model_prompt_and_raw_response(db_and_app):
    db, _app, _client = db_and_app
    seeded, scope, now = _ready_discussion(db, session_no=131)
    message = db.create_message(
        seeded["group_id"],
        seeded["students"][0][0],
        "我们已经比较了两种方案的证据，并形成了共同观点。",
        role="student",
        created_at=_time(now - timedelta(seconds=20)),
        session_id=seeded["session_id"],
        discussion_id=scope["discussion_id"],
    )
    from services.emotion_slot_service import EmotionSlotService
    from services.emotion_window_service import EmotionWindowService
    from services.emotion_agent.emotion_feedback_classifier import (
        EMOTION_FEEDBACK_PROMPT_VERSION,
        EmotionFeedbackClassifier,
    )
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    reserved = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    snapshot = EmotionWindowService.snapshot_from_slot(reserved["slot"])
    context = EmotionReflectionService.build_context(
        group_id=seeded["group_id"],
        session_id=seeded["session_id"],
        discussion_id=scope["discussion_id"],
        task_id=seeded["task_id"],
        slot_id=reserved["slot"]["id"],
        slot_index=1,
        window_start=snapshot["current_window_start"],
        window_end=snapshot["current_window_end"],
        frozen_context=snapshot,
    )
    output = _output("GROUP_EXCELLENT", context)
    output["evidence_message_ids"] = [message["id"]]

    result = EmotionFeedbackClassifier.classify(
        context, gateway=SequenceGateway([output])
    )
    assessment = db.query_one(
        "SELECT * FROM emotion_feedback_assessments WHERE slot_id=?",
        (reserved["slot"]["id"],),
    )

    assert result["success"] is True
    assert assessment["status"] == "succeeded"
    assert assessment["emotion_feedback_state"] == "GROUP_EXCELLENT"
    assert assessment["confidence"] == pytest.approx(0.84)
    assert assessment["validation_status"] == "VALID"
    assert assessment["attempt_count"] == 1
    assert assessment["prompt_version"] == EMOTION_FEEDBACK_PROMPT_VERSION
    assert assessment["model_name"] == "emotion-e1-test-model"
    assert json.loads(assessment["evidence_message_ids_json"]) == [message["id"]]
    assert json.loads(assessment["raw_response_json"])[0]["response"]["success"] is True


def test_database_rejects_a_sixth_feedback_state(db_and_app):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db, session_no=132)
    from services.emotion_slot_service import EmotionSlotService

    reserved = EmotionSlotService.ensure_latest_due_slot(scope, now=now)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE emotion_feedback_assessments SET emotion_feedback_state=? WHERE slot_id=?",
            ("NO_FEEDBACK", reserved["slot"]["id"]),
        )


def test_classifier_truncation_uses_deterministic_type_and_slot_still_sends(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    _seeded, scope, now = _ready_discussion(db, session_no=133)
    from services.llm_gateway import LlmResult
    import services.emotion_agent.emotion_reflection_service as service_module
    from services.emotion_slot_service import EmotionSlotService

    gateway = SequenceGateway(
        [
            LlmResult(
                success=False,
                output=None,
                raw_text='{"feedback_state":"GROUP_LOW_PARTICIPATION"',
                failure_type="truncated_response",
                model_name="emotion-e1-test-model",
            )
        ]
    )
    monkeypatch.setattr(service_module, "get_gateway", lambda: gateway)
    reserved = EmotionSlotService.ensure_latest_due_slot(scope, now=now)

    result = EmotionSlotService.execute_slot(reserved["enqueue_slot_id"], now=now)
    slot = db.query_one(
        "SELECT * FROM emotion_reflection_slots WHERE id=?",
        (reserved["slot"]["id"],),
    )
    assessment = db.query_one(
        "SELECT * FROM emotion_feedback_assessments WHERE slot_id=?",
        (reserved["slot"]["id"],),
    )

    assert result["status"] == "sent"
    assert slot["status"] == "sent"
    assert slot["next_retry_at"] is None
    assert assessment["status"] == "succeeded"
    assert assessment["emotion_feedback_state"] == "GROUP_LOW_PARTICIPATION"
    assert assessment["confidence"] == 0.0
    assert assessment["validation_status"] == "DETERMINISTIC_FALLBACK"
    assert assessment["failure_reason"] == "llm_call_failed:truncated_response"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM intervention_runs WHERE emotion_slot_id=?",
        (reserved["slot"]["id"],),
    )["c"] == 1
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'"
    )["c"] == 1
