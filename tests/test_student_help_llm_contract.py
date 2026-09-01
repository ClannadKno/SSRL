# -*- coding: utf-8 -*-

from services import llm_analyzer
from services.llm_gateway import BUILTIN_PROFILES, LlmResult


class _CapturingGateway:
    def __init__(self):
        self.calls = []

    def call(self, profile_name, payload, response_type="text"):
        self.calls.append((profile_name, payload, response_type))
        return LlmResult(
            success=True,
            output="先说出当前卡点，再一起确定下一步。",
            profile_name=profile_name,
        )


def test_student_help_profile_uses_1000_output_tokens():
    assert BUILTIN_PROFILES["student_help"]["max_tokens"] == 1000


def test_student_help_prompt_targets_30_and_caps_at_40_chars(monkeypatch):
    gateway = _CapturingGateway()
    monkeypatch.setattr(llm_analyzer, "_llm_enabled", lambda: True)
    monkeypatch.setattr(llm_analyzer, "get_gateway", lambda: gateway)

    result = llm_analyzer.generate_student_help_response(
        1,
        {
            "current_task": {
                "title": "讨论任务",
                "output_requirement": "形成小组方案",
            }
        },
        {"template_guidance": "先识别卡点，再给一个最小行动。"},
        "experiment",
        [{"real_name": "同学A", "content": "我们不知道下一步怎么办。"}],
        "请帮我们梳理下一步。",
    )

    assert result
    assert len(gateway.calls) == 1
    profile_name, payload, response_type = gateway.calls[0]
    system_prompt = payload["messages"][0]["content"]
    assert profile_name == "student_help"
    assert response_type == "text"
    assert "以 30 个汉字以内为目标" in system_prompt
    assert "最多不得超过 40 个汉字（标点计入字数）" in system_prompt
    assert "超出时必须主动压缩措辞" in system_prompt
    assert "120 个中文字符" not in system_prompt
    assert "80 个中文字符" not in system_prompt
