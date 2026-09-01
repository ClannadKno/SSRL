# -*- coding: utf-8 -*-
"""Batch 6 regressions for emotion validation, fallback and audit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from tests.helpers import seed_running_session


def _time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _ready_emotion_scope(db, *, session_no: int):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    context = seed_running_session(
        db,
        session_no=session_no,
        member_count=2,
        limit_minutes=30,
    )
    db.execute(
        "UPDATE experiment_sessions SET emotion_agent_enabled=1 WHERE id=?",
        (context["session_id"],),
    )
    runtime = None
    for user_id, _login_key in context["students"]:
        runtime = enter_group_discussion_stage(
            context["session_id"], context["group_id"], user_id
        )
    now = datetime.now().replace(microsecond=0)
    message = db.create_message(
        context["group_id"],
        context["students"][0][0],
        "我们正在认真梳理彼此的观点。",
        role="student",
        session_no=context["session_no"],
        task_id=context["task_id"],
        created_at=_time(now - timedelta(seconds=10)),
    )
    return context, runtime["id"], message, now


def _fake_gateway(monkeypatch, output):
    from services.llm_gateway import LlmResult
    import services.emotion_agent.emotion_reflection_service as service_module

    class Gateway:
        def call(self, profile_name, payload, response_type="json"):
            result_output = output
            if profile_name == "emotion_feedback_classifier":
                result_output = {
                    "feedback_state": "GROUP_EXCELLENT",
                    "confidence": 0.82,
                    "comparison_summary": "当前完整时间槽存在正常的群体参与。",
                    "current_window_summary": "当前窗口包含任务相关交流。",
                    "previous_window_summary": "第一槽不使用上一窗口作比较判断。",
                    "evidence_message_ids": [],
                    "excluded_alternatives": [
                        {
                            "state": "GROUP_LOW_PARTICIPATION",
                            "reason": "当前已有正常的任务相关交流。",
                        }
                    ],
                }
            elif (
                isinstance(result_output, dict)
                and "final_text" in result_output
                and "message_situation" not in result_output
            ):
                result_output = {
                    **result_output,
                    "message_situation": "POSITIVE_ENGAGEMENT",
                    "situation_summary": "学生正在表达和回应彼此观点。",
                }
            return LlmResult(
                success=True,
                output=result_output,
                raw_text=json.dumps(result_output, ensure_ascii=False),
                profile_name=profile_name,
                model_name="batch6-fixed-emotion-mock",
            )

    monkeypatch.setattr(service_module, "get_gateway", lambda: Gateway())


def _execute(db, context, discussion_id, now):
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    return EmotionReflectionService.execute_once(
        group_id=context["group_id"],
        session_id=context["session_id"],
        discussion_id=discussion_id,
        task_id=context["task_id"],
        scheduled_at=_time(now),
        tick_index=1,
        slot_id=601,
        window_start=_time(now - timedelta(minutes=2)),
        window_end=_time(now),
    )


def test_validator_allows_group_pronoun_and_rejects_personal_or_task_direction():
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    allowed = EmotionReflectionService.validate_message(
        "你们正在认真交流不同看法，保持这样的投入就很好。🌿"
    )
    singular = EmotionReflectionService.validate_message(
        "你刚才的想法特别好。🌿"
    )
    member = EmotionReflectionService.validate_message(
        "成员2刚才表现得最好。🌿"
    )
    named_member = EmotionReflectionService.validate_message(
        "小明刚才表现得最好。🌿",
        member_labels=["小明"],
    )
    task_advice = EmotionReflectionService.validate_message(
        "你们应该先讨论证据再写下结论。🌿"
    )

    assert allowed["valid"] is True
    assert singular["reason"] == "contains_personal_pronoun_you"
    assert member["reason"] == "contains_personal_targeting"
    assert named_member["reason"] == "contains_personal_targeting"
    assert "contains_suggestion_words" in task_advice["failure_codes"]


def test_state_fallbacks_validate_and_positive_never_uses_negative_language():
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
        FALLBACK_MESSAGES,
    )

    for state_code, messages in FALLBACK_MESSAGES.items():
        for message in messages:
            result = EmotionReflectionService.validate_message(
                message,
                dominant_state=state_code,
            )
            assert result["valid"] is True, (state_code, message, result)

    positive = FALLBACK_MESSAGES["positive_collaboration"]
    negative_markers = (
        "卡住", "卡顿", "节奏可能有些紧", "停顿", "别着急",
        "放轻松", "压力", "分歧", "冲突",
    )
    assert all(
        not any(marker in message for marker in negative_markers)
        for message in positive
    )


def test_state_consistency_and_previous_message_deduplication():
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    contradictory = EmotionReflectionService.validate_message(
        "大家现在有些紧张，慢慢来就好。🌿",
        dominant_state="positive_collaboration",
    )
    duplicate = EmotionReflectionService.validate_message(
        "大家讨论得很积极，继续保持！💪",
        dominant_state="positive_collaboration",
        previous_message="大家配合得很好，继续保持！💪",
    )
    distinct_fallback = EmotionReflectionService._get_fallback(
        "positive_collaboration",
        previous_message="大家的投入和互相补充很清晰，稳稳保持就好 😊",
    )

    assert contradictory["reason"] == (
        "state_semantic_conflict:positive_collaboration"
    )
    assert duplicate["reason"] == "duplicate_previous_emotion_message"
    assert distinct_fallback
    assert not EmotionReflectionService._messages_are_too_similar(
        distinct_fallback,
        "大家的投入和互相补充很清晰，稳稳保持就好 😊",
    )


def test_duplicate_model_output_uses_distinct_state_fallback_and_saves_full_audit(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context, discussion_id, student_message, now = _ready_emotion_scope(
        db, session_no=1201
    )
    previous_text = "大家配合得很好，继续保持！💪"
    previous_run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, discussion_id, task_id, cutoff_sequence,
            agent_type, trigger_type, status, dominant_state,
            actual_published_at, published_at, completed_at, created_at
        ) VALUES(?,?,?,?,?,'emotion','emotion_time_slot','PUBLISHED',
                 'positive_collaboration',?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            discussion_id,
            context["task_id"],
            0,
            _time(now - timedelta(minutes=5)),
            _time(now - timedelta(minutes=5)),
            _time(now - timedelta(minutes=5)),
            _time(now - timedelta(minutes=5)),
        ),
    )
    previous_message = db.create_message(
        context["group_id"],
        db.get_sera_user_id(),
        previous_text,
        role="agent",
        session_no=context["session_no"],
        task_id=context["task_id"],
        intervention_run_id=previous_run_id,
        created_at=_time(now - timedelta(minutes=5)),
    )
    db.execute(
        "UPDATE messages SET agent_type='emotion' WHERE id=?",
        (previous_message["id"],),
    )
    db.execute(
        "UPDATE intervention_runs SET message_id=? WHERE id=?",
        (previous_message["id"], previous_run_id),
    )

    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )

    built = EmotionReflectionService.build_context(
        group_id=context["group_id"],
        session_id=context["session_id"],
        discussion_id=discussion_id,
        task_id=context["task_id"],
        slot_id=601,
        slot_index=1,
        window_start=_time(now - timedelta(minutes=2)),
        window_end=_time(now),
    )
    assert built["previous_emotion_run_id"] == previous_run_id
    built["latest_monitor_state"] = {
        "state_code": "conflict_tension",
        "evidence_sequences": [student_message["sequence"]],
    }
    built["latest_batch_state"] = {
        "state_code": "positive_collaboration",
        "evidence_sequences": [student_message["sequence"]],
    }
    built["dominant_state"] = {
        "state_code": "positive_collaboration",
        "state_source": "assessment_batch",
        "freshness_seconds": 6,
        "state_has_recovered": True,
    }
    built["latest_group_state"] = built["dominant_state"]
    built["state_has_recovered"] = True
    monkeypatch.setattr(
        EmotionReflectionService,
        "build_context",
        staticmethod(lambda **_kwargs: built),
    )
    model_message = "大家讨论得很积极，继续保持！💪"
    _fake_gateway(
        monkeypatch,
        {
            "feedback_state": "GROUP_EXCELLENT",
            "final_text": model_message,
        },
    )

    result = _execute(db, context, discussion_id, now)

    assert result["status"] == "published"
    assert result["message"] == model_message
    run = dict(
        db.query_one(
            "SELECT * FROM intervention_runs WHERE id=?",
            (result["run_id"],),
        )
    )
    assert run["emotion_slot_id"] == 601
    assert run["context_student_sequence_start"] == student_message["sequence"]
    assert run["context_student_sequence_end"] == student_message["sequence"]
    assert json.loads(run["context_student_sequences_json"]) == [
        student_message["sequence"]
    ]
    assert run["dominant_state"] == "positive_collaboration"
    assert run["dominant_state_source"] == "assessment_batch"
    assert run["state_freshness_seconds"] == 6
    assert run["state_has_recovered"] == 1
    assert run["previous_emotion_run_id"] == previous_run_id
    assert run["model_raw_message"] == model_message
    assert json.loads(run["validation_failure_codes_json"]) == []
    assert run["fallback_state_code"] is None
    assert run["fallback_message"] is None
    assert run["final_visible_message"] == result["message"]
    assert run["final_disposition"] == "model_published"
    assert run["validation_result"] == "passed"
    audit = json.loads(run["emotion_audit_json"])
    assert audit["slot_id"] == 601
    assert audit["final_visible_message"] == result["message"]
    validation = json.loads(run["validation_json"])
    assert validation["model_validation"]["valid"] is True
    assert validation["model_validation"]["checks"]["validation_scope"] == "structure_only"
    assert validation["length_checked"] is False
    assert validation["fallback_validation"] is None
    assert validation["final_passed"] is True


def test_structurally_valid_fallback_is_published_without_local_content_gate(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context, discussion_id, _student_message, now = _ready_emotion_scope(
        db, session_no=1202
    )
    _fake_gateway(
        monkeypatch,
        {"should_send": True, "message": "你应该先写下答案。🌿"},
    )
    db.execute(
        """
        INSERT INTO emotion_reflection_slots(
            id, group_id, session_id, discussion_id, slot_index,
            scheduled_at, prompt_version, status, created_at, updated_at
        ) VALUES(601,?,?,?,?,?,'emotion_slot_windows_v1','running',?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            discussion_id,
            1,
            _time(now),
            _time(now),
            _time(now),
        ),
    )
    from services.emotion_agent.emotion_feedback_generator import (
        EmotionFeedbackGenerator,
    )

    monkeypatch.setattr(
        EmotionFeedbackGenerator,
        "select_fallback",
        staticmethod(
            lambda feedback_state, recent_emotion_messages=(), student_messages=(): {
                "template_id": "unsafe-test-template",
                "text": "你们应该先讨论证据再写答案。🌿",
            }
        ),
    )

    result = _execute(db, context, discussion_id, now)

    assert result["status"] == "fallback"
    assert result["message"] == "你们应该先讨论证据再写答案。🌿"
    generation = dict(
        db.query_one(
            "SELECT * FROM emotion_feedback_generations WHERE slot_id=601"
        )
    )
    assert generation["status"] == "PUBLISHED"
    assert generation["final_text"] == result["message"]
    assert generation["published_message_id"] == result["message_id"]
    assert generation["failure_reason"] == "invalid_output_schema"
    assert db.query_one(
        """
        SELECT COUNT(*) AS c FROM messages
        WHERE group_id=? AND agent_type='emotion'
        """,
        (context["group_id"],),
    )["c"] == 1


def test_emotion_audit_schema_migration_is_idempotent(db_and_app):
    db, _app, _client = db_and_app

    db.init_db()
    db.init_db()
    columns = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(intervention_runs)")
    }

    assert {
        "emotion_slot_id",
        "context_student_sequence_start",
        "context_student_sequence_end",
        "context_student_sequences_json",
        "latest_monitor_state_json",
        "latest_batch_state_json",
        "dominant_state",
        "dominant_state_source",
        "state_freshness_seconds",
        "state_has_recovered",
        "previous_emotion_run_id",
        "model_raw_message",
        "validation_failure_codes_json",
        "fallback_state_code",
        "fallback_message",
        "final_visible_message",
        "final_disposition",
        "emotion_audit_json",
        "emotion_feedback_schema_version",
        "emotion_feedback_type_code",
        "emotion_feedback_type_label",
        "emotion_feedback_classification_json",
        "emotion_reference_template_ids_json",
        "emotion_feedback_output_json",
    }.issubset(columns)


def test_two_stage_feedback_classification_generation_and_persistence(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context, discussion_id, _student_message, now = _ready_emotion_scope(
        db, session_no=1203
    )
    calls = []
    final_text = "大家认真交流彼此观点，这份共同投入值得肯定！👍"

    from services.llm_gateway import LlmResult
    import services.emotion_agent.emotion_reflection_service as service_module

    class TwoStageGateway:
        def call(self, profile_name, payload, response_type="json"):
            calls.append(
                {
                    "profile_name": profile_name,
                    "payload": payload,
                    "response_type": response_type,
                }
            )
            if profile_name == "emotion_feedback_classifier":
                output = {
                    "feedback_state": "GROUP_EXCELLENT",
                    "confidence": 0.91,
                    "comparison_summary": "第一完整时间槽只根据当前参与判断。",
                    "current_window_summary": "当前时间槽存在正常、积极的群体参与。",
                    "previous_window_summary": "第一槽不使用上一窗口作比较判断。",
                    "evidence_message_ids": [],
                    "excluded_alternatives": [
                        {
                            "state": "GROUP_LOW_PARTICIPATION",
                            "reason": "当前已有正常的任务相关交流。",
                        }
                    ],
                }
            else:
                output = {
                    "feedback_state": "GROUP_EXCELLENT",
                    "message_situation": "POSITIVE_ENGAGEMENT",
                    "situation_summary": "学生正在认真交流彼此观点。",
                    "final_text": final_text,
                }
            return LlmResult(
                success=True,
                output=output,
                raw_text=(
                    json.dumps(output, ensure_ascii=False)
                    if isinstance(output, dict)
                    else output
                ),
                profile_name=profile_name,
                model_name="two-stage-emotion-mock",
            )

    monkeypatch.setattr(
        service_module,
        "get_gateway",
        lambda: TwoStageGateway(),
    )

    result = _execute(db, context, discussion_id, now)

    assert result["status"] == "published"
    assert result["feedback_type_code"] == "GROUP_EXCELLENT"
    assert result["feedback_type_label"] == "群体优秀"
    assert result["message_situation"] == "POSITIVE_ENGAGEMENT"
    assert result["situation_summary"] == "学生正在认真交流彼此观点。"
    assert result["message"] == final_text
    assert [call["profile_name"] for call in calls] == [
        "emotion_feedback_classifier",
        "emotion_reflection_generator",
    ]
    assert calls[0]["response_type"] == "json"
    assert calls[0]["payload"]["response_format"] == {"type": "json_object"}
    assert calls[1]["response_type"] == "json"
    assert calls[1]["payload"]["response_format"] == {"type": "json_object"}
    generation_prompt = json.dumps(calls[1]["payload"], ensure_ascii=False)
    assert "GROUP_EXCELLENT" in generation_prompt
    assert "只包含 feedback_state、message_situation" in generation_prompt
    assert "E2-EXCELLENT-" not in generation_prompt

    run = dict(
        db.query_one(
            "SELECT * FROM intervention_runs WHERE id=?",
            (result["run_id"],),
        )
    )
    assert run["generated_message"] == final_text
    assert run["final_visible_message"] == final_text
    assert run["emotion_feedback_schema_version"] == "emotion_feedback_v1"
    assert run["emotion_feedback_type_code"] == "GROUP_EXCELLENT"
    assert run["emotion_feedback_type_label"] == "群体优秀"
    emotion_audit = json.loads(run["emotion_audit_json"])
    assert emotion_audit["message_situation"] == "POSITIVE_ENGAGEMENT"
    assert emotion_audit["situation_summary"] == "学生正在认真交流彼此观点。"
    classification = json.loads(run["emotion_feedback_classification_json"])
    assert classification["decision_source"] == "llm"
    assert classification["confidence"] == 0.91
    reference_ids = json.loads(run["emotion_reference_template_ids_json"])
    assert len(reference_ids) == 2
    assert all(item.startswith("E2-EXCELLENT-") for item in reference_ids)
    structured_output = json.loads(run["emotion_feedback_output_json"])
    assert structured_output == {
        "schema_version": "emotion_feedback_v1",
        "feedback_type_code": "GROUP_EXCELLENT",
        "feedback_type_label": "群体优秀",
        "content": final_text,
    }

    message = db.query_one(
        "SELECT content, metadata_json FROM messages WHERE id=?",
        (result["message_id"],),
    )
    assert message["content"] == final_text
    metadata = json.loads(message["metadata_json"])
    assert metadata["feedback_type_code"] == "GROUP_EXCELLENT"
    assert metadata["feedback_type_label"] == "群体优秀"
    assert metadata["reference_template_ids"] == reference_ids
