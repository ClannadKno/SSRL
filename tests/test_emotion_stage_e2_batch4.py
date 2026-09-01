# -*- coding: utf-8 -*-

import json
import re
from datetime import datetime, timedelta

from tests.helpers import seed_running_session


INTERVAL_SECONDS = 300


def _time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _ready_discussion(db, *, now=None, session_no=2041):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    now = now or datetime.now().replace(microsecond=0)
    seeded = seed_running_session(
        db, session_no=session_no, member_count=4, limit_minutes=60
    )
    db.execute(
        """
        UPDATE experiment_sessions
        SET agent_mode='emotion', emotion_agent_enabled=1,
            strategy_agent_enabled=0
        WHERE id=?
        """,
        (seeded["session_id"],),
    )
    runtime = None
    for user_id, _key in seeded["students"]:
        runtime = enter_group_discussion_stage(
            seeded["session_id"], seeded["group_id"], user_id
        )
    started_at = now - timedelta(seconds=INTERVAL_SECONDS + 5)
    db.execute(
        """
        UPDATE group_session_discussions
        SET started_at=?, deadline=?, status='running', updated_at=?
        WHERE id=?
        """,
        (
            _time(started_at),
            _time(now + timedelta(minutes=30)),
            _time(now),
            runtime["id"],
        ),
    )
    runtime = dict(runtime)
    runtime["started_at"] = _time(started_at)
    return seeded, runtime, now


def _due_slot(db, seeded, runtime, now):
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.ensure_latest_due_slot(
        {
            "group_id": seeded["group_id"],
            "session_id": seeded["session_id"],
            "discussion_id": runtime["id"],
            "all_members_entered_at": runtime["started_at"],
        },
        now=now,
    )
    assert result.get("enqueue_slot_id")
    return int(result["enqueue_slot_id"])


def _classification(state="GROUP_LOW_PARTICIPATION"):
    return {
        "feedback_state": state,
        "feedback_type_code": state,
        "comparison_summary": "当前窗口相较前一窗口的群体参与变化已完成判断。",
        "current_window_summary": "当前窗口的小组交流概况已经汇总。",
        "canonical_sub_state_code": "conflict_tension",
        "strategy_id": "should-never-reach-e2",
        "student_messages": [
            {
                "id": 901,
                "sequence": 41,
                "created_at": "2026-08-04 10:09:30",
                "member_label": "成员1",
                "content": "这个部分有点难，但我们还在一起尝试。",
            }
        ],
    }


def _e1_output(state="GROUP_LOW_PARTICIPATION"):
    return {
        "feedback_state": state,
        "confidence": 0.88,
        "comparison_summary": "当前完整窗口的群体有效参与较低。",
        "current_window_summary": "当前窗口交流较少，适合低压力支持。",
        "previous_window_summary": "第一槽不使用上一窗口作比较判断。",
        "evidence_message_ids": [],
        "excluded_alternatives": [
            {
                "state": "GROUP_EXCELLENT",
                "reason": "当前有效参与尚不足以归入群体优秀。",
            }
        ],
    }


def _e2_output(
    state,
    final_text,
    *,
    message_situation="NEUTRAL_OR_MIXED",
    situation_summary="当前消息没有更突出的高风险情绪信号。",
):
    return {
        "feedback_state": state,
        "message_situation": message_situation,
        "situation_summary": situation_summary,
        "final_text": final_text,
    }


class _SequenceGateway:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def call(self, profile_name, payload, response_type="json"):
        from services.llm_gateway import LlmResult

        self.calls.append(
            {
                "profile_name": profile_name,
                "payload": payload,
                "response_type": response_type,
            }
        )
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return LlmResult(
            success=True,
            output=output,
            raw_text=json.dumps(output, ensure_ascii=False),
            profile_name=profile_name,
            model_name="emotion-e2-test-model",
        )


def test_all_five_template_pools_are_safe_and_group_facing():
    from services.emotion_agent.emotion_feedback_generator import (
        EMOTION_FEEDBACK_TYPES,
        EmotionFeedbackGenerator,
    )

    assert set(EMOTION_FEEDBACK_TYPES) == {
        "GROUP_EXCELLENT",
        "GROUP_IMPROVING",
        "GROUP_DECLINING",
        "GROUP_LOW_PARTICIPATION",
        "GROUP_SUSTAINED_EXCELLENT",
    }
    for state, config in EMOTION_FEEDBACK_TYPES.items():
        assert len(config["templates"]) == 8
        for template_id, text in config["templates"]:
            validated = EmotionFeedbackGenerator.validate_output(
                _e2_output(state, text),
                expected_state=state,
            )
            assert validated["final_text"] == text, template_id
            assert "大家" in text or "小组" in text
            assert not re.search(r"你(?!们)", text)
            assert "老师" not in text


def test_e2_prompt_contains_frozen_student_messages_and_only_allowed_inputs():
    from services.emotion_agent.emotion_feedback_generator import (
        EMOTION_FEEDBACK_E2_INPUT_KEYS,
        EmotionFeedbackGenerator,
    )

    e2_input = EmotionFeedbackGenerator.build_input(
        _classification("GROUP_IMPROVING"),
        student_messages=_classification("GROUP_IMPROVING")["student_messages"],
        reference_templates=[
            {"template_id": "ref-1", "text": "这一阶段大家比前面更主动了，小组的进步值得肯定！👍"}
        ],
        recent_emotion_messages=[
            {"feedback_state": "GROUP_EXCELLENT", "text": "大家前一阶段参与得很积极！😊"}
        ],
    )
    system_prompt, user_prompt = EmotionFeedbackGenerator.build_prompt(e2_input)
    user_payload = json.loads(user_prompt)

    assert set(e2_input) == EMOTION_FEEDBACK_E2_INPUT_KEYS
    assert set(user_payload) == EMOTION_FEEDBACK_E2_INPUT_KEYS
    assert user_payload["feedback_state"] == "GROUP_IMPROVING"
    assert user_payload["student_messages"] == [
        {
            "id": 901,
            "sequence": 41,
            "created_at": "2026-08-04 10:09:30",
            "member_label": "成员1",
            "content": "这个部分有点难，但我们还在一起尝试。",
        }
    ]
    assert "这个部分有点难，但我们还在一起尝试" in user_prompt
    assert "conflict_tension" not in user_prompt
    assert "should-never-reach-e2" not in user_prompt
    assert "不得修改 feedback_state" in system_prompt
    assert "最多40个字符" in system_prompt
    assert "feedback_state 不是情绪状态" in system_prompt
    assert "student_messages 的语义优先级高于" in system_prompt
    assert "INTERPERSONAL_TENSION" in system_prompt
    assert "参与活跃不等于情绪积极或合作良好" in system_prompt
    assert "只包含 feedback_state、message_situation" in system_prompt


def test_conflict_window_requires_auditable_situation_before_publishing():
    from services.emotion_agent.emotion_feedback_generator import (
        EmotionFeedbackGenerator,
    )

    state = "GROUP_EXCELLENT"
    conflict_messages = [
        {"id": 9, "sequence": 9, "member_label": "成员3", "content": "你们两个今天是和预算表杠上了。"},
        {"id": 10, "sequence": 10, "member_label": "成员4", "content": "规则确实绕，但现在有点开始互相较劲了。"},
        {"id": 13, "sequence": 13, "member_label": "成员1", "content": "你们一直否定我的讨论区方案，根本没有认真听我的理由。"},
        {"id": 14, "sequence": 14, "member_label": "成员2", "content": "你每次都说不是针对我，却又直接绕过我的顾虑，我觉得你根本不愿意听。"},
    ]
    grounded_text = "大家都很在意讨论，有分歧也正常，缓一缓听听彼此。🌿"
    gateway = _SequenceGateway(
        [
            {
                "feedback_state": state,
                "final_text": "大家表现得很主动，小组认真交流的状态很不错！",
            },
            _e2_output(
                state,
                grounded_text,
                message_situation="INTERPERSONAL_TENSION",
                situation_summary="成员之间出现否定、针对感和拒绝倾听的对抗表达。",
            ),
        ]
    )
    e2_input = EmotionFeedbackGenerator.build_input(
        _classification(state),
        student_messages=conflict_messages,
        reference_templates=["大家表现得很主动，小组认真交流的状态很不错！"],
        recent_emotion_messages=[],
    )

    result = EmotionFeedbackGenerator.generate(e2_input, gateway=gateway)

    assert result["success"] is True
    assert result["message_situation"] == "INTERPERSONAL_TENSION"
    assert "否定" in result["situation_summary"]
    assert result["final_text"] == grounded_text
    assert result["validation_status"] == "VALID_AFTER_REPAIR"
    assert len(gateway.calls) == 2
    first_user_payload = json.loads(
        gateway.calls[0]["payload"]["messages"][1]["content"]
    )
    assert [item["content"] for item in first_user_payload["student_messages"]] == [
        item["content"] for item in conflict_messages
    ]
    repair_prompt = gateway.calls[1]["payload"]["messages"][0]["content"]
    assert "invalid_output_schema" in repair_prompt
    assert "student_messages 的语义优先级高于" in repair_prompt


def test_changed_state_is_repaired_once_without_changing_state():
    from services.emotion_agent.emotion_feedback_generator import (
        EMOTION_FEEDBACK_TYPES,
        EmotionFeedbackGenerator,
    )

    state = "GROUP_IMPROVING"
    valid_text = EMOTION_FEEDBACK_TYPES[state]["templates"][2][1]
    gateway = _SequenceGateway(
        [
            _e2_output(
                "GROUP_EXCELLENT",
                "老师看到你进步了，正确答案是四。😊😊",
            ),
            _e2_output(
                state,
                valid_text,
                message_situation="POSITIVE_ENGAGEMENT",
                situation_summary="学生仍在共同尝试解决困难。",
            ),
        ]
    )
    e2_input = EmotionFeedbackGenerator.build_input(
        _classification(state),
        student_messages=_classification(state)["student_messages"],
        reference_templates=[item[1] for item in EMOTION_FEEDBACK_TYPES[state]["templates"][:2]],
        recent_emotion_messages=[],
    )

    result = EmotionFeedbackGenerator.generate(e2_input, gateway=gateway)

    assert result["success"] is True
    assert result["feedback_state"] == state
    assert result["final_text"] == valid_text
    assert result["fallback_used"] is False
    assert result["validation_status"] == "VALID_AFTER_REPAIR"
    assert len(gateway.calls) == 2
    assert all(call["response_type"] == "json" for call in gateway.calls)
    assert all(
        call["payload"]["response_format"] == {"type": "json_object"}
        for call in gateway.calls
    )
    assert "feedback_state_changed" in gateway.calls[1]["payload"]["messages"][0]["content"]


def test_two_structurally_invalid_generations_use_distinct_same_category_fallback():
    from services.emotion_agent.emotion_feedback_generator import (
        EMOTION_FEEDBACK_TYPES,
        EmotionFeedbackGenerator,
        messages_are_too_similar,
    )

    state = "GROUP_DECLINING"
    recent = EMOTION_FEEDBACK_TYPES[state]["templates"][0][1]
    gateway = _SequenceGateway(
        [
            _e2_output("GROUP_EXCELLENT", recent),
            _e2_output(state, ""),
        ]
    )
    e2_input = EmotionFeedbackGenerator.build_input(
        _classification(state),
        student_messages=_classification(state)["student_messages"],
        reference_templates=[item[1] for item in EMOTION_FEEDBACK_TYPES[state]["templates"][:2]],
        recent_emotion_messages=[recent],
    )

    result = EmotionFeedbackGenerator.generate(e2_input, gateway=gateway)

    assert result["success"] is True
    assert result["feedback_state"] == state
    assert result["fallback_used"] is True
    assert result["validation_status"] == "FALLBACK_VALID"
    assert not messages_are_too_similar(result["final_text"], recent)
    validated = EmotionFeedbackGenerator.validate_output(
        _e2_output(state, result["final_text"]),
        expected_state=state,
        recent_emotion_messages=[recent],
    )
    assert validated["feedback_state"] == state


def test_model_failure_on_conflict_window_uses_tension_aware_fallback():
    from services.emotion_agent.emotion_feedback_generator import (
        EmotionFeedbackGenerator,
    )

    state = "GROUP_EXCELLENT"
    gateway = _SequenceGateway([RuntimeError("timeout"), RuntimeError("timeout")])
    e2_input = EmotionFeedbackGenerator.build_input(
        _classification(state),
        student_messages=[
            {"id": 13, "content": "你们一直否定我的方案，根本没有认真听我的理由。"},
            {"id": 14, "content": "你说不是针对我，却根本不愿意听我的顾虑。"},
        ],
        reference_templates=["大家表现得很主动，小组认真交流的状态很不错！"],
        recent_emotion_messages=[],
    )

    result = EmotionFeedbackGenerator.generate(e2_input, gateway=gateway)

    assert result["success"] is True
    assert result["fallback_used"] is True
    assert result["message_situation"] == "INTERPERSONAL_TENSION"
    assert result["fallback_template_id"].startswith("E2-TENSION-")
    assert "状态很不错" not in result["final_text"]
    assert "分歧" in result["final_text"] or "听" in result["final_text"]


def test_generated_text_is_published_without_local_length_or_content_gate():
    from services.emotion_agent.emotion_feedback_generator import (
        EmotionFeedbackGenerator,
    )

    state = "GROUP_EXCELLENT"
    generated_text = (
        "大家参与很积极，彼此回应也很充分，这份共同投入和认真交流的良好状态"
        "特别值得继续保持下去！😊😊"
    )
    gateway = _SequenceGateway(
        [_e2_output(state, generated_text, message_situation="POSITIVE_ENGAGEMENT")]
    )
    e2_input = EmotionFeedbackGenerator.build_input(
        _classification(state),
        student_messages=_classification(state)["student_messages"],
        reference_templates=[],
        recent_emotion_messages=[generated_text],
    )

    result = EmotionFeedbackGenerator.generate(e2_input, gateway=gateway)

    assert len(generated_text) > 40
    assert result["success"] is True
    assert result["final_text"] == generated_text
    assert result["fallback_used"] is False
    assert result["validation_status"] == "VALID"
    assert result["model_validation"]["checks"] == {
        "non_empty": True,
        "state_consistent": True,
        "message_situation_present": True,
        "situation_summary_present": True,
        "validation_scope": "structure_only",
        "content_checked": False,
        "length_checked": False,
    }
    assert len(gateway.calls) == 1


def test_execute_slot_persists_and_publishes_stage_e2_record(
    db_and_app, monkeypatch
):
    db, _app, _client = db_and_app
    seeded, runtime, now = _ready_discussion(db)
    source_message = db.create_message(
        seeded["group_id"],
        seeded["students"][0][0],
        "这个观点有点难，但我们正在一起尝试。",
        role="student",
        created_at=_time(now - timedelta(seconds=20)),
        session_id=seeded["session_id"],
        discussion_id=runtime["id"],
    )
    slot_id = _due_slot(db, seeded, runtime, now)
    from services.emotion_agent.emotion_feedback_generator import (
        EMOTION_FEEDBACK_TYPES,
    )

    final_text = EMOTION_FEEDBACK_TYPES["GROUP_LOW_PARTICIPATION"]["templates"][0][1]

    class Gateway:
        def call(self, profile_name, payload, response_type="json"):
            from services.llm_gateway import LlmResult

            output = (
                _e1_output()
                if profile_name == "emotion_feedback_classifier"
                else _e2_output(
                    "GROUP_LOW_PARTICIPATION",
                    final_text,
                    message_situation="CONFUSION_OR_HESITATION",
                    situation_summary="学生表示内容较难，但仍在共同尝试。",
                )
            )
            return LlmResult(
                success=True,
                output=output,
                raw_text=json.dumps(output, ensure_ascii=False),
                profile_name=profile_name,
                model_name="emotion-e2-integration-model",
            )

    import services.emotion_agent.emotion_reflection_service as service_module

    monkeypatch.setattr(service_module, "get_gateway", lambda: Gateway())
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    generation = dict(
        db.query_one(
            "SELECT * FROM emotion_feedback_generations WHERE slot_id=?",
            (slot_id,),
        )
    )
    slot = dict(
        db.query_one(
            "SELECT * FROM emotion_reflection_slots WHERE id=?", (slot_id,)
        )
    )
    message = dict(
        db.query_one("SELECT * FROM messages WHERE id=?", (slot["message_id"],))
    )
    input_snapshot = json.loads(generation["input_snapshot_json"])

    assert result["status"] == "sent"
    assert slot["status"] == "sent"
    assert generation["status"] == "PUBLISHED"
    assert generation["emotion_feedback_state"] == "GROUP_LOW_PARTICIPATION"
    assert generation["final_text"] == final_text == message["content"]
    assert generation["fallback_used"] == 0
    assert generation["validation_status"] == "VALID"
    assert generation["published_message_id"] == message["id"]
    assert generation["published_at"]
    assert set(input_snapshot) == {
        "feedback_state",
        "comparison_summary",
        "current_window_summary",
        "student_messages",
        "reference_templates",
        "recent_emotion_messages",
    }
    assert input_snapshot["student_messages"] == [
        {
            "id": source_message["id"],
            "sequence": source_message["sequence"],
            "created_at": source_message["created_at"],
            "member_label": "成员1",
            "content": "这个观点有点难，但我们正在一起尝试。",
        }
    ]
    serialized_input = json.dumps(input_snapshot, ensure_ascii=False)
    assert "canonical" not in serialized_input
    assert "strategy_id" not in serialized_input


def test_discussion_end_is_rechecked_before_publish(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded, runtime, now = _ready_discussion(db, session_no=2042)
    slot_id = _due_slot(db, seeded, runtime, now)
    from services.emotion_agent.emotion_feedback_generator import (
        EMOTION_FEEDBACK_TYPES,
    )

    final_text = EMOTION_FEEDBACK_TYPES["GROUP_LOW_PARTICIPATION"]["templates"][1][1]

    class ClosingGateway:
        closed = False

        def call(self, profile_name, payload, response_type="json"):
            from services.llm_gateway import LlmResult

            if profile_name == "emotion_reflection_generator" and not self.closed:
                self.closed = True
                db.execute(
                    "UPDATE group_session_discussions SET status='closed' WHERE id=?",
                    (runtime["id"],),
                )
            output = (
                _e1_output()
                if profile_name == "emotion_feedback_classifier"
                else _e2_output(
                    "GROUP_LOW_PARTICIPATION",
                    final_text,
                    message_situation="NEUTRAL_OR_MIXED",
                )
            )
            return LlmResult(
                success=True,
                output=output,
                raw_text=json.dumps(output, ensure_ascii=False),
                profile_name=profile_name,
                model_name="emotion-e2-close-race-model",
            )

    import services.emotion_agent.emotion_reflection_service as service_module

    monkeypatch.setattr(service_module, "get_gateway", lambda: ClosingGateway())
    from services.emotion_slot_service import EmotionSlotService

    result = EmotionSlotService.execute_slot(slot_id, now=now)
    generation = db.query_one(
        "SELECT status, published_message_id FROM emotion_feedback_generations WHERE slot_id=?",
        (slot_id,),
    )

    assert result["status"] == "expired"
    assert result["reason"] == "discussion_closed_after_generation"
    assert generation["status"] == "EXPIRED"
    assert generation["published_message_id"] is None
    assert db.query_one(
        "SELECT COUNT(*) AS count FROM messages WHERE agent_type='emotion'"
    )["count"] == 0
