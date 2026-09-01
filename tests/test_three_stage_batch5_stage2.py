# -*- coding: utf-8 -*-
"""Batch 5 coverage for bounded Stage 2 call diagnostics."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class _FakeResult:
    def __init__(
        self,
        output=None,
        *,
        success=True,
        failure_type=None,
        failure_message=None,
        finish_reason="stop",
        attempt_count=1,
    ):
        self.success = success
        self.output = output
        self.raw_text = (
            output
            if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False)
            if output is not None
            else ""
        )
        self.model_name = "batch5-stage2-model"
        self.profile_name = "state_detector"
        self.latency_ms = 3
        self.attempt_count = attempt_count
        self.token_usage = {"completion_tokens": 12}
        self.failure_type = failure_type
        self.failure_message = failure_message or failure_type
        self.finish_reason = finish_reason


class _FakeGateway:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.profiles = {
            "state_detector": SimpleNamespace(
                model="batch5-profile-model",
                read_timeout=45,
                retries=0,
            )
        }

    def call(self, profile, payload, response_type):
        self.calls.append((profile, payload, response_type))
        return self.results.pop(0)


def _context():
    return {
        "group_id": 11,
        "session_id": 12,
        "discussion_id": 13,
        "pipeline_run_id": 14,
        "assessment_batch_id": 15,
        "recent_student_messages": [
            {
                "id": 101,
                "sequence": 1,
                "role": "student",
                "user_id": 21,
                "content": "student-secret-content-that-must-not-be-persisted",
                "created_at": "2026-08-01 10:00:00",
            }
        ],
    }


def _valid_output():
    return {
        "sub_category": "confusion",
        "canonical_state": "confusion",
        "confidence": 0.82,
        "evidence_message_ids": [1],
    }


def test_stage2_records_bounded_attempt_diagnostics_and_repair_budget(monkeypatch):
    detector = __import__(
        "services.discussion_pipeline_v2.llm_state_detector",
        fromlist=["LLMStateDetector"],
    )
    gateway = _FakeGateway(
        [
            _FakeResult(
                '{"sub_category":"confusion"',
                success=False,
                failure_type="truncated_response",
                failure_message="provider stopped at the output limit",
                finish_reason="length",
            ),
            _FakeResult(_valid_output()),
        ]
    )
    events = []
    audits = []
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "STATE_LLM_SCHEMA_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(detector, "get_gateway", lambda: gateway)
    monkeypatch.setattr(
        detector,
        "record_latency_event",
        lambda **kwargs: events.append(kwargs) or {"recorded": True},
    )
    monkeypatch.setattr(
        detector,
        "safe_write_audit_log",
        lambda **kwargs: audits.append(kwargs) or True,
    )

    envelope = detector.LLMStateDetector.detect(_context())

    assert envelope["meta"]["success"] is True
    assert envelope["meta"]["external_call_count"] == 2
    assert envelope["meta"]["repair_attempt_count"] == 1
    assert envelope["meta"]["gateway_retry_count"] == 0
    attempts = envelope["meta"]["validation_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["attempt_type"] == "initial"
    assert attempts[0]["failure_category"] == "response_truncated"
    assert attempts[1]["attempt_type"] == "repair"
    assert attempts[1]["failure_category"] is None
    required = {
        "model",
        "profile",
        "max_tokens",
        "timeout",
        "gateway_retry_count",
        "prompt_character_count",
        "prompt_estimated_tokens",
        "response_character_count",
        "finish_reason",
        "parser_result",
        "json_extractable",
        "open_brace_present",
        "close_brace_present",
        "response_incomplete",
    }
    assert required <= set(attempts[0])
    assert all(event["pipeline_run_id"] == 14 for event in events)
    assert all(event["assessment_batch_id"] == 15 for event in events)
    finished = [event for event in events if event["event"].endswith("_finished")]
    assert len(finished) == 2
    assert all("candidate_messages" not in event["details"] for event in finished)
    assert all("student-secret-content" not in json.dumps(event) for event in finished)
    assert "student-secret-content" not in envelope["meta"]["raw_response"]
    assert all("student-secret-content" not in json.dumps(item) for item in audits)


def test_stage2_diagnostics_do_not_call_complete_json_truncated(
    monkeypatch,
):
    detector = __import__(
        "services.discussion_pipeline_v2.llm_state_detector",
        fromlist=["LLMStateDetector"],
    )
    gateway = _FakeGateway([_FakeResult(_valid_output(), finish_reason="length")])
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: gateway)
    monkeypatch.setattr(detector, "record_latency_event", lambda **_kwargs: None)
    monkeypatch.setattr(detector, "safe_write_audit_log", lambda **_kwargs: True)

    envelope = detector.LLMStateDetector.detect(_context())

    attempt = envelope["meta"]["validation_attempts"][0]
    assert attempt["json_extractable"] is True
    assert attempt["response_incomplete"] is True
    assert attempt["failure_category"] is None


@pytest.mark.parametrize(
    ("reason", "message", "attempt_type", "expected"),
    [
        ("read_timeout", "", "initial", "transport_timeout"),
        ("network_error", "", "initial", "transport_error"),
        ("json_parse_error", "", "initial", "json_unparseable"),
        (
            "missing_top_level_field",
            "missing_top_level_field:sub_category",
            "initial",
            "missing_primary_state",
        ),
        (
            "missing_top_level_field",
            "missing_top_level_field:should_intervene",
            "initial",
            "missing_should_intervene",
        ),
        ("evidence_outside_candidate", "", "initial", "invalid_evidence"),
        ("json_parse_error", "", "repair", "repair_failed"),
    ],
)
def test_stage2_failure_categories_are_distinct(
    reason,
    message,
    attempt_type,
    expected,
):
    from services.three_stage_latency import normalize_stage2_failure

    assert (
        normalize_stage2_failure(
            reason,
            attempt_type=attempt_type,
            error_message=message,
        )
        == expected
    )
