# -*- coding: utf-8 -*-
"""Regression coverage for Stage 2 state-detector JSON validation retries."""

import json


def test_state_detector_profile_allows_a_complete_structured_response():
    from services.llm_gateway import BUILTIN_PROFILES

    profile = BUILTIN_PROFILES["state_detector"]
    assert profile["read_timeout"] == 45
    assert profile["read_timeout"] < 60
    assert profile["retries"] == 0


class _FakeLlmResult:
    def __init__(self, output, *, success=True):
        self.success = success
        self.output = output
        self.raw_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        self.model_name = "fake-state-model"
        self.latency_ms = 1
        self.attempt_count = 1
        self.token_usage = None
        self.failure_type = None
        self.failure_message = None
        self.fallback_required = False


def _segment(canonical, start, end, evidence, *, active=True):
    return {
        "raw_sub_state": canonical,
        "canonical_sub_state": canonical,
        "secondary_tags": [],
        "start_sequence": start,
        "end_sequence": end,
        "confidence": 0.82,
        "evidence_message_ids": evidence,
        "reason": f"{canonical} evidence",
        "is_active_at_window_end": active,
        "detected_self_regulation": False,
    }


def _payload(canonical, *, evidence=None, candidates=None):
    evidence = list(evidence or [1, 2])
    if candidates is None:
        candidates = ["ER-003"] if canonical == "off_topic_unregulated" else ["ER-002"]
    return {
        "schema_version": "stage2.v1",
        "analysis_scope": {
            "candidate_start_sequence": 1,
            "candidate_end_sequence": 2,
            "input_cutoff_student_sequence": 2,
        },
        "segments": [_segment(canonical, 1, 2, evidence)],
        "active_sub_state": {
            "raw_sub_state": canonical,
            "canonical_sub_state": canonical,
            "secondary_tags": [],
            "confidence": 0.82,
            "start_sequence": 1,
            "end_sequence": 2,
            "evidence_message_ids": evidence,
            "detected_self_regulation": False,
        },
        "should_intervene": True,
        "inhibition": {"is_inhibited": False, "strategy_id": None, "reason": None},
        "candidate_strategy_ids": candidates,
        "decision_reason": "validated precise sub-state",
    }


def _context(messages=None):
    return {
        "group_id": 1,
        "recent_student_messages": messages
        or [
            {
                "id": 1,
                "role": "student",
                "user_id": 1,
                "content": "food later?",
                "created_at": "2026-07-17 10:00:00",
            },
            {
                "id": 2,
                "role": "student",
                "user_id": 2,
                "content": "play a game",
                "created_at": "2026-07-17 10:00:10",
            },
        ],
        "participants": [
            {"user_id": 1, "message_count_10m": 1, "recent_message_count": 1},
            {"user_id": 2, "message_count_10m": 1, "recent_message_count": 1},
        ],
    }


def test_llm_state_detector_retries_invalid_json_once_until_valid(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []
    outputs = ["{bad", _payload("off_topic_unregulated")]

    class FakeGateway:
        def call(self, profile, payload, response_type):
            calls.append((profile, payload, response_type))
            return _FakeLlmResult(outputs[len(calls) - 1])

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 2
    assert envelope["result"]["schema_version"] == "stage2.v1"
    assert envelope["result"]["primary_state"] == "task_detached"
    assert envelope["result"]["state_code"] == "task_detached"
    assert envelope["result"]["evidence_message_ids"] == [1, 2]
    assert envelope["result"]["segments"][0]["canonical_sub_state"] == "off_topic_unregulated"
    assert envelope["result"]["segments"][0]["evidence_sequences"] == [1, 2]
    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["meta"]["retry_count"] == 1
    assert calls[0][1]["max_tokens"] == 1800
    assert calls[1][1]["max_tokens"] == 3200


def test_llm_state_detector_sends_canonical_semantics_and_message_timestamps(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []

    class FakeGateway:
        def call(self, profile, request_payload, response_type):
            calls.append((profile, request_payload, response_type))
            return _FakeLlmResult(_payload("off_topic_unregulated"))

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert envelope["meta"]["validation_status"] == "passed"
    user_context = json.loads(calls[0][1]["messages"][1]["content"])
    semantics = user_context["canonical_sub_state_semantics"]
    assert set(semantics) == set(user_context["allowed_canonical_sub_states"])
    assert "30到120秒" in semantics["deep_thinking"]["definition"]
    assert "决策前" in semantics["execution_progress"]["boundary"]
    assert "价值质疑" in semantics["burnout"]["definition"]
    assert "倦怠" in semantics["perfunctory_detachment"]["boundary"]
    assert "个体边缘化" in semantics["execution_progress"]["boundary"]
    assert "防御性退出" in semantics["individual_marginalization"]["boundary"]
    assert "最低限度" in semantics["perfunctory_detachment"]["boundary"]
    assert "敷衍脱离" in semantics["individual_marginalization"]["boundary"]
    assert user_context["candidate_messages"][0]["timestamp"] == "2026-07-17 10:00:00"
    system_prompt = calls[0][1]["messages"][0]["content"]
    assert system_prompt.find("canonical_sub_state_semantics") >= 0
    assert "优先判为 individual_marginalization 而非 execution_progress" in system_prompt
    assert "优先判为 interpersonal_conflict" in system_prompt
    assert "应判为 perfunctory_detachment" in system_prompt


def test_llm_state_detector_inherits_omitted_active_evidence_from_matching_segment(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("off_topic_unregulated")
    payload["active_sub_state"].pop("evidence_message_ids")
    calls = []

    class FakeGateway:
        def call(self, profile, request_payload, response_type):
            calls.append((profile, request_payload, response_type))
            return _FakeLlmResult(payload)

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 1
    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["meta"]["retry_count"] == 0
    assert envelope["result"]["active_sub_state"]["evidence_message_ids"] == [1, 2]
    assert envelope["result"]["evidence_message_ids"] == [1, 2]


def test_llm_state_detector_recovers_null_active_duplicate_from_single_flagged_segment(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("standard", candidates=["SS-001"])
    payload["should_intervene"] = False
    payload["active_sub_state"] = None
    calls = []

    class FakeGateway:
        def call(self, profile, request_payload, response_type):
            calls.append((profile, request_payload, response_type))
            return _FakeLlmResult(payload)

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 1
    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["result"]["active_sub_state"]["canonical_sub_state"] == "standard"
    assert envelope["result"]["active_sub_state"]["evidence_message_ids"] == [1, 2]


def test_llm_state_detector_rejects_null_active_duplicate_without_flagged_segment():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("standard", candidates=["SS-001"])
    payload["should_intervene"] = False
    payload["active_sub_state"] = None
    payload["segments"][0]["is_active_at_window_end"] = False

    parsed = detector.parse_llm_json_content(payload, candidate_sequences=[1, 2])

    assert parsed["valid"] is False
    assert parsed["error_message"] == "invalid_active_sub_state"


def test_llm_state_detector_normalizes_null_inhibition_for_non_oi_route(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("standard", candidates=["SS-001"])
    payload["should_intervene"] = False
    payload["inhibition"] = None
    calls = []

    class FakeGateway:
        def call(self, profile, request_payload, response_type):
            calls.append((profile, request_payload, response_type))
            return _FakeLlmResult(payload)

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 1
    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["result"]["active_sub_state"]["canonical_sub_state"] == "standard"
    assert envelope["result"]["inhibition"] == {
        "is_inhibited": False,
        "strategy_id": None,
        "reason": None,
    }
    assert envelope["result"]["candidate_strategy_ids"] == []


def test_llm_state_detector_normalizes_null_inhibition_for_oi_route(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("deep_thinking", candidates=[])
    payload["should_intervene"] = False
    payload["inhibition"] = None
    calls = []

    class FakeGateway:
        def call(self, profile, request_payload, response_type):
            calls.append((profile, request_payload, response_type))
            return _FakeLlmResult(payload)

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 1
    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["result"]["active_sub_state"]["canonical_sub_state"] == "deep_thinking"
    assert envelope["result"]["inhibition"] == {
        "is_inhibited": True,
        "strategy_id": "OI-002",
        "reason": None,
    }
    assert envelope["result"]["candidate_strategy_ids"] == []


def test_llm_state_detector_fails_closed_after_one_repair_retry(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []

    class FakeGateway:
        def call(self, profile, payload, response_type):
            calls.append((profile, payload, response_type))
            return _FakeLlmResult("{bad")

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 2
    assert envelope["result"]["detector_error"] is True
    assert envelope["meta"]["validation_status"] == "failed"
    assert envelope["meta"]["schema_valid"] is False
    assert envelope["meta"]["retry_count"] == 1


def test_llm_state_detector_rejects_evidence_sequences_outside_candidates(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []

    class FakeGateway:
        def call(self, profile, payload, response_type):
            calls.append((profile, payload, response_type))
            return _FakeLlmResult(_payload("frustration", evidence=[999]))

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(calls) == 2
    assert envelope["result"]["detector_error"] is True
    assert envelope["result"]["error_type"] == "schema_validation_error"
    assert envelope["meta"]["schema_error"] == "evidence_outside_candidate"


def test_llm_state_detector_compacts_overcomplete_but_valid_evidence():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("frustration", evidence=[1, 2, 3, 4])
    payload["analysis_scope"]["candidate_end_sequence"] = 4
    payload["analysis_scope"]["input_cutoff_student_sequence"] = 4
    payload["segments"][0]["end_sequence"] = 4
    payload["active_sub_state"]["end_sequence"] = 4

    parsed = detector.parse_llm_json_content(
        payload,
        candidate_sequences=[1, 2, 3, 4],
    )

    assert parsed["valid"] is True
    assert parsed["data"]["segments"][0]["evidence_message_ids"] == [1, 2, 4]
    assert parsed["data"]["active_sub_state"]["evidence_message_ids"] == [1, 2, 4]


def test_llm_state_detector_enriches_explicit_compatible_overlays(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    cases = [
        (
            "interpersonal_conflict",
            ["ER-001"],
            ["我被嘲笑后不敢表达。", "人身攻击仍在继续，心理安全风险没有解除。"],
            "psychological_safety_risk",
        ),
        (
            "frustration",
            ["ER-002"],
            ["信息太多，我同时盯着多项约束。", "现在反应不过来，注意力跟不上。"],
            "high_intensity_overload",
        ),
        (
            "execution_progress",
            [],
            ["预算已经通过检查。", "这一阶段目标完成，接下来只做格式核对。"],
            "stage_achievement",
        ),
    ]
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    for canonical, candidates, texts, expected_overlay in cases:
        payload = _payload(canonical, candidates=candidates)
        if canonical == "execution_progress":
            payload["should_intervene"] = False
            payload["inhibition"] = {
                "is_inhibited": True,
                "strategy_id": "OI-004",
                "reason": "execution progress",
            }
        calls = []

        class FakeGateway:
            def call(self, profile, request_payload, response_type):
                calls.append(request_payload)
                return _FakeLlmResult(payload)

        monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
        messages = [
            {
                "id": index,
                "sequence": index,
                "role": "student",
                "user_id": index,
                "content": text,
                "created_at": f"2026-07-17 10:00:0{index}",
            }
            for index, text in enumerate(texts, start=1)
        ]

        envelope = detector.LLMStateDetector.detect(_context(messages))

        assert envelope["meta"]["validation_status"] == "passed"
        assert envelope["result"]["segments"][0]["secondary_tags"] == [expected_overlay]
        assert envelope["result"]["active_sub_state"]["secondary_tags"] == [expected_overlay]
        user_context = json.loads(calls[0]["messages"][1]["content"])
        assert set(user_context["allowed_secondary_tags"]) == {
            "psychological_safety_risk",
            "high_intensity_overload",
            "stage_achievement",
        }
        assert user_context["secondary_tag_semantics"][expected_overlay][
            "compatible_primary_state"
        ] == canonical


def test_llm_state_detector_rejects_overlay_on_incompatible_primary():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    payload = _payload("frustration", candidates=["ER-002"])
    payload["segments"][0]["secondary_tags"] = ["stage_achievement"]

    parsed = detector.parse_llm_json_content(payload, candidate_sequences=[1, 2])

    assert parsed["valid"] is False
    assert parsed["error_message"] == "secondary_tag_primary_mismatch"
