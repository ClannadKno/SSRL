# -*- coding: utf-8 -*-
"""Batch 2 coverage for the Stage 2 state-only detector contract."""

from __future__ import annotations

import json


class _FakeLlmResult:
    def __init__(self, output):
        self.success = True
        self.output = output
        self.raw_text = json.dumps(output, ensure_ascii=False)
        self.model_name = "batch2-state-only"
        self.latency_ms = 1
        self.attempt_count = 1
        self.token_usage = None
        self.failure_type = None
        self.failure_message = None
        self.fallback_required = False


def test_state_only_contract_keeps_burnout_distinct_from_frustration():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    parsed = detector.parse_llm_json_content(
        {
            "sub_category": "burnout",
            "canonical_state": "burnout",
            "confidence": 0.88,
            "evidence_message_ids": [1, 3],
        },
        candidate_sequences=[1, 2, 3],
    )

    assert parsed["valid"] is True
    data = parsed["data"]
    assert data["state_recognition"] == {
        "sub_category": "burnout",
        "canonical_state": "burnout",
        "confidence": 0.88,
        "evidence_message_ids": [1, 3],
    }
    assert data["active_sub_state"]["canonical_sub_state"] == "burnout"
    assert data["segments"][0]["state_code"] == "blocked_frustration"
    assert data["candidate_strategy_ids"] == ["ER-008"]


def test_state_only_contract_supports_stage_achievement_as_overlay():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    parsed = detector.parse_llm_json_content(
        {
            "sub_category": "stage_achievement",
            "canonical_state": "execution_progress",
            "confidence": 0.91,
            "evidence_message_ids": [2, 3],
        },
        candidate_sequences=[1, 2, 3],
    )

    assert parsed["valid"] is True
    data = parsed["data"]
    assert data["state_recognition"]["sub_category"] == "stage_achievement"
    assert data["state_recognition"]["canonical_state"] == "execution_progress"
    assert data["active_sub_state"]["canonical_sub_state"] == "execution_progress"
    assert data["active_sub_state"]["secondary_tags"] == ["stage_achievement"]


def test_state_only_contract_rejects_strategy_fields_and_burnout_collapse():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    with_strategy = detector.parse_llm_json_content(
        {
            "sub_category": "burnout",
            "canonical_state": "burnout",
            "confidence": 0.88,
            "evidence_message_ids": [1],
            "candidate_strategy_ids": ["ER-008"],
        },
        candidate_sequences=[1],
    )
    collapsed = detector.parse_llm_json_content(
        {
            "sub_category": "burnout",
            "canonical_state": "frustration",
            "confidence": 0.88,
            "evidence_message_ids": [1],
        },
        candidate_sequences=[1],
    )

    assert with_strategy["valid"] is False
    assert with_strategy["error_type"] == "stage2_state_only_forbidden_field"
    assert collapsed["valid"] is False
    assert collapsed["error_type"] == "sub_category_canonical_mismatch"


def test_detector_request_omits_strategy_mapping_and_accepts_compact_output(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []

    class FakeGateway:
        def call(self, profile, request_payload, response_type):
            calls.append((profile, request_payload, response_type))
            return _FakeLlmResult(
                {
                    "sub_category": "倦怠型",
                    "canonical_state": "burnout",
                    "confidence": 0.87,
                    "evidence_message_ids": [1, 2],
                }
            )

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(
        {
            "group_id": 1,
            "recent_student_messages": [
                {
                    "id": 11,
                    "sequence": 1,
                    "role": "student",
                    "user_id": 1,
                    "content": "反正做了也白做，没什么意思。",
                    "created_at": "2026-07-31 10:00:00",
                },
                {
                    "id": 12,
                    "sequence": 2,
                    "role": "student",
                    "user_id": 2,
                    "content": "我也不太想继续了。",
                    "created_at": "2026-07-31 10:00:10",
                },
            ],
        }
    )

    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["result"]["active_sub_state"]["canonical_sub_state"] == "burnout"
    assert envelope["result"]["candidate_strategy_ids"] == ["ER-008"]
    user_payload = json.loads(calls[0][1]["messages"][1]["content"])
    assert user_payload["output_contract"]["required_top_level_fields"] == [
        "sub_category",
        "canonical_state",
        "confidence",
        "evidence_message_ids",
    ]
    assert "sub_state_strategy_mapping" not in user_payload
    assert "strategy_library_version" not in user_payload
    assert "burnout" in user_payload["allowed_sub_categories"]
    assert "stage_achievement" in user_payload["allowed_sub_categories"]
