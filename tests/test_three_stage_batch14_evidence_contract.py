# -*- coding: utf-8 -*-
"""Batch 14 coverage for the dynamic Stage 2 evidence contract."""

from __future__ import annotations

import json


class _FakeLlmResult:
    def __init__(self, output):
        self.success = True
        self.output = output
        self.raw_text = json.dumps(output, ensure_ascii=False)
        self.model_name = "batch14-model"
        self.profile_name = "state_detector"
        self.latency_ms = 1
        self.attempt_count = 1
        self.gateway_retry_count = 0
        self.compatibility_fallback_count = 0
        self.token_usage = {"completion_tokens": 20}
        self.finish_reason = "stop"
        self.failure_type = None
        self.failure_message = None
        self.final_content_only = True


def _state_only(evidence):
    return {
        "sub_category": "confusion",
        "canonical_state": "confusion",
        "confidence": 0.86,
        "evidence_message_ids": list(evidence),
    }


def _context():
    return {
        "group_id": 7,
        "assessment_batch_id": 14,
        "state_detector_candidate_sequences": [14],
        "state_detector_messages": [
            {
                "id": 113,
                "sequence": 13,
                "role": "student",
                "user_id": 1,
                "content": "PRIVATE_CONTEXT_ONLY_TEXT",
            },
            {
                "id": 114,
                "sequence": 14,
                "role": "student",
                "user_id": 2,
                "content": "PRIVATE_CANDIDATE_TEXT",
            },
        ],
    }


def _install_detector_fakes(monkeypatch, outputs, *, audit_rows=None, latency_rows=None):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    class FakeGateway:
        profiles = {}

        def __init__(self):
            self.calls = []

        def call(self, profile, payload, response_type):
            self.calls.append((profile, payload, response_type))
            return _FakeLlmResult(outputs[len(self.calls) - 1])

    gateway = FakeGateway()
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "STATE_LLM_SCHEMA_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(detector, "get_gateway", lambda: gateway)
    monkeypatch.setattr(
        detector,
        "safe_write_audit_log",
        lambda **kwargs: (audit_rows.append(kwargs) if audit_rows is not None else True),
    )
    monkeypatch.setattr(
        detector,
        "record_latency_event",
        lambda **kwargs: (
            latency_rows.append(kwargs) if latency_rows is not None else None
        ),
    )
    return detector, gateway


def test_single_candidate_sequence_is_enumerated_and_accepted(monkeypatch):
    audit_rows = []
    detector, gateway = _install_detector_fakes(
        monkeypatch,
        [_state_only([14])],
        audit_rows=audit_rows,
    )

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(gateway.calls) == 1
    user_payload = json.loads(gateway.calls[0][1]["messages"][1]["content"])
    assert user_payload["evidence_contract"] == {
        "allowed_candidate_sequences": [14],
        "context_only_sequences": [13],
        "context_only_may_be_evidence": False,
        "mixed_evidence_policy": (
            "retain_valid_candidate_sequences_and_audit_rejections"
        ),
        "minimum_valid_candidate_evidence": 1,
        "sequence_inference_allowed": False,
    }
    assert user_payload["output_contract"]["allowed_evidence_sequences"] == [14]
    assert user_payload["output_contract"]["forbidden_context_only_sequences"] == [13]
    assert envelope["result"]["active_sub_state"]["evidence_message_ids"] == [14]
    assert envelope["result"]["evidence_message_ids"] == [114]
    assert envelope["result"]["evidence_validation"]["sequence_inference_used"] is False
    assert audit_rows[-1]["metadata"]["evidence_valid_sequence_count"] == 1


def test_mixed_duplicate_and_unordered_evidence_keeps_only_real_candidates():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    parsed = detector.parse_llm_json_content(
        _state_only([16, 13, 99, 14, 16, 15]),
        candidate_sequences=[14, 15, 16],
        context_only_sequences=[13],
    )

    assert parsed["valid"] is True
    assert parsed["data"]["active_sub_state"]["evidence_message_ids"] == [14, 15, 16]
    validation = parsed["data"]["evidence_validation"]
    assert validation["valid_sequence_count"] == 3
    assert validation["rejected_sequence_count"] == 2
    assert validation["duplicate_sequence_count"] == 1
    assert validation["input_order_normalized"] is True
    assert validation["filtered"] is True
    assert validation["sequence_inference_used"] is False
    segment = validation["segments"][0]
    assert segment["accepted_sequences"] == [14, 15, 16]
    assert segment["rejected_reason_counts"] == {
        "context_only_not_evidence": 1,
        "outside_candidate": 1,
    }


def test_only_context_or_outside_evidence_fails_closed_with_audit_details():
    import services.discussion_pipeline_v2.llm_state_detector as detector

    parsed = detector.parse_llm_json_content(
        _state_only([13, 99]),
        candidate_sequences=[14],
        context_only_sequences=[13],
    )

    assert parsed["valid"] is False
    assert parsed["error_type"] == "evidence_outside_candidate"
    assert parsed["error_message"] == "evidence_outside_candidate"
    validation = parsed["evidence_validation"]
    assert validation["valid_sequence_count"] == 0
    assert validation["rejected_sequence_count"] == 2
    assert validation["sequence_inference_used"] is False


def test_repair_that_still_uses_invalid_evidence_is_terminal_and_bounded(
    monkeypatch,
):
    audit_rows = []
    latency_rows = []
    detector, gateway = _install_detector_fakes(
        monkeypatch,
        [_state_only([13]), _state_only([99])],
        audit_rows=audit_rows,
        latency_rows=latency_rows,
    )

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(gateway.calls) == 2
    assert envelope["result"]["detector_error"] is True
    assert envelope["result"]["error_type"] == "schema_validation_error"
    assert envelope["meta"]["schema_error"] == "evidence_outside_candidate"
    assert envelope["meta"]["external_call_count"] == 2
    assert envelope["meta"]["repair_attempt_count"] == 1
    repair_payload = json.loads(gateway.calls[1][1]["messages"][1]["content"])
    assert repair_payload["evidence_contract"]["allowed_candidate_sequences"] == [14]
    assert repair_payload["validation_error"] == "evidence_outside_candidate"
    assert audit_rows[-1]["metadata"]["evidence_rejected_sequence_count"] == 1
    persisted_diagnostics = json.dumps(
        {"audit": audit_rows, "latency": latency_rows},
        ensure_ascii=False,
    )
    assert "PRIVATE_CONTEXT_ONLY_TEXT" not in persisted_diagnostics
    assert "PRIVATE_CANDIDATE_TEXT" not in persisted_diagnostics

