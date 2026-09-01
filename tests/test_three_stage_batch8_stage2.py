# -*- coding: utf-8 -*-
"""Batch 8 coverage for Stage 2 state-boundary decisions."""

from __future__ import annotations

import json


REQUIRED_BOUNDARY_PAIRS = {
    "confusion_vs_frustration",
    "constructive_conflict_vs_interpersonal_conflict",
    "frustration_vs_perfunctory_detachment",
    "perfunctory_detachment_vs_individual_marginalization",
    "standard_vs_execution_progress",
    "primary_state_vs_high_intensity_overload",
}
BOUNDARY_FIELDS = {"positive", "counterexample", "boundary", "recovered", "overlay"}


def test_stage2_boundary_guidance_covers_each_required_example_dimension():
    from services.three_stage_schema import STAGE2_STATE_BOUNDARY_GUIDANCE

    assert set(STAGE2_STATE_BOUNDARY_GUIDANCE) == REQUIRED_BOUNDARY_PAIRS
    for guidance in STAGE2_STATE_BOUNDARY_GUIDANCE.values():
        assert set(guidance) == BOUNDARY_FIELDS
        assert all(isinstance(value, str) and value.strip() for value in guidance.values())


def test_state_only_high_intensity_overlay_requires_an_explicit_primary_state():
    from services.discussion_pipeline_v2.llm_state_detector import parse_llm_json_content

    ambiguous = parse_llm_json_content(
        {
            "sub_category": "high_intensity_overload",
            "canonical_state": "high_intensity_overload",
            "confidence": 0.9,
            "evidence_message_ids": [1, 2],
        },
        candidate_sequences=[1, 2],
    )
    assert ambiguous["valid"] is False
    assert ambiguous["error_type"] == "invalid_canonical_state"

    explicit = parse_llm_json_content(
        {
            "sub_category": "high_intensity_overload",
            "canonical_state": "frustration",
            "confidence": 0.9,
            "evidence_message_ids": [1, 2],
        },
        candidate_sequences=[1, 2],
    )
    assert explicit["valid"] is True
    assert explicit["data"]["active_sub_state"]["canonical_sub_state"] == "frustration"
    assert explicit["data"]["active_sub_state"]["secondary_tags"] == [
        "high_intensity_overload"
    ]


def test_high_intensity_overlay_does_not_default_to_interpersonal_conflict():
    from services.discussion_pipeline_v2.llm_state_detector import (
        _explicit_overlay_tags,
        parse_llm_json_content,
    )

    parsed = parse_llm_json_content(
        {
            "sub_category": "high_intensity_overload",
            "canonical_state": "standard",
            "confidence": 0.8,
            "evidence_message_ids": [3],
        },
        candidate_sequences=[3],
    )
    assert parsed["valid"] is True
    assert parsed["data"]["active_sub_state"]["canonical_sub_state"] == "standard"
    assert parsed["data"]["active_sub_state"]["secondary_tags"] == [
        "high_intensity_overload"
    ]

    assert _explicit_overlay_tags(
        "interpersonal_conflict",
        "信息太多，大家同时盯着多个限制；有人身攻击后成员不敢发言。",
    ) == ["psychological_safety_risk", "high_intensity_overload"]


def test_detector_payload_exposes_boundary_guidance_and_overlay_contract(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []

    class Result:
        success = True
        output = {
            "sub_category": "high_intensity_overload",
            "canonical_state": "frustration",
            "confidence": 0.86,
            "evidence_message_ids": [1, 2],
        }
        raw_text = json.dumps(output, ensure_ascii=False)
        model_name = "batch8-state-model"
        profile_name = "state_detector"
        latency_ms = 1
        attempt_count = 1
        token_usage = None
        failure_type = None
        failure_message = None
        finish_reason = "stop"

    class Gateway:
        def call(self, profile, payload, response_type):
            calls.append((profile, payload, response_type))
            return Result()

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: Gateway())
    monkeypatch.setattr(detector, "safe_write_audit_log", lambda **_kwargs: True)
    monkeypatch.setattr(detector, "record_latency_event", lambda **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(
        {
            "group_id": 8,
            "recent_student_messages": [
                {
                    "id": 81,
                    "sequence": 1,
                    "role": "student",
                    "user_id": 1,
                    "content": "信息太多，我同时盯着几个限制，反应不过来。",
                    "created_at": "2026-08-01 10:00:00",
                },
                {
                    "id": 82,
                    "sequence": 2,
                    "role": "student",
                    "user_id": 2,
                    "content": "我还在逐项核对预算和空间，想把问题拆开。",
                    "created_at": "2026-08-01 10:00:10",
                },
            ],
        }
    )

    assert envelope["meta"]["validation_status"] == "passed"
    assert len(calls) == 1
    user_payload = json.loads(calls[0][1]["messages"][1]["content"])
    assert set(user_payload["state_boundary_guidance"]) == REQUIRED_BOUNDARY_PAIRS
    assert user_payload["output_contract"][
        "overlay_requires_explicit_primary_canonical_state"
    ] is True
    system_prompt = calls[0][1]["messages"][0]["content"]
    assert "持续尝试并提供具体任务内容属于 frustration" in system_prompt
    assert "high_intensity_overload 只能作为 secondary overlay" in system_prompt
    assert "不能填写 overlay 自身" in system_prompt

