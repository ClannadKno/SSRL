# -*- coding: utf-8 -*-
"""Batch 12 coverage for Stage 2 final-content and reasoning controls."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx


def _gateway(handler, *, profile_name="state_detector"):
    from services.llm_gateway import LLMGateway, LlmProfile

    profile = LlmProfile(
        name=profile_name,
        temperature=0.0,
        connect_timeout=1,
        read_timeout=2,
        max_tokens=1800,
        retries=0,
        model="batch12-model",
        base_url="https://provider.test/chat/completions",
        api_key="test-key",
    )
    gateway = LLMGateway.__new__(LLMGateway)
    gateway.profiles = {profile_name: profile}
    gateway.clients = {
        profile_name: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://provider.test",
        )
    }
    gateway._closed = False
    return gateway


def _chat_response(*, content, reasoning_content="", finish_reason="stop", usage=None):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage or {"completion_tokens": 20},
    }


def _stage2_payload():
    return {
        "messages": [
            {"role": "system", "content": "Return compact JSON."},
            {"role": "user", "content": "{}"},
        ],
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "_sera_final_content_only": True,
        "_sera_compatibility_fallback_fields": ["thinking"],
        "_sera_external_call_budget": 2,
    }


def test_gateway_classifies_reasoning_budget_exhausted_without_parsing_reasoning():
    requests = []
    reasoning_json = '{"sub_category":"confusion"}'

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_chat_response(
                content="",
                reasoning_content=reasoning_json,
                finish_reason="length",
                usage={
                    "completion_tokens": 1800,
                    "completion_tokens_details": {"reasoning_tokens": 1800},
                },
            ),
        )

    gateway = _gateway(handler)
    try:
        result = gateway.call("state_detector", _stage2_payload(), "json")
    finally:
        gateway.close()

    assert result.success is False
    assert result.output is None
    assert result.failure_type == "reasoning_budget_exhausted"
    assert result.final_content_only is True
    assert result.attempt_count == 1
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert reasoning_json not in json.dumps(result.output)


def test_gateway_uses_only_valid_final_content_when_reasoning_is_present():
    def handler(_request):
        return httpx.Response(
            200,
            json=_chat_response(
                content='{"source":"final"}',
                reasoning_content='{"source":"reasoning"}',
            ),
        )

    gateway = _gateway(handler)
    try:
        result = gateway.call("state_detector", _stage2_payload(), "json")
    finally:
        gateway.close()

    assert result.success is True
    assert result.output == {"source": "final"}


def test_gateway_never_promotes_reasoning_json_when_final_content_is_empty():
    def handler(_request):
        return httpx.Response(
            200,
            json=_chat_response(
                content="",
                reasoning_content='{"source":"reasoning"}',
                usage={
                    "completion_tokens": 20,
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
            ),
        )

    gateway = _gateway(handler)
    try:
        result = gateway.call("state_detector", _stage2_payload(), "json")
    finally:
        gateway.close()

    assert result.success is False
    assert result.output is None
    assert result.failure_type == "invalid_response"


def test_gateway_drops_unsupported_thinking_parameter_once_within_budget():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if "thinking" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unknown parameter: thinking",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx.Response(
            200,
            json=_chat_response(content='{"source":"compatibility"}'),
        )

    gateway = _gateway(handler)
    try:
        result = gateway.call("state_detector", _stage2_payload(), "json")
    finally:
        gateway.close()

    assert result.success is True
    assert result.output == {"source": "compatibility"}
    assert result.attempt_count == 2
    assert result.gateway_retry_count == 0
    assert result.compatibility_fallback_count == 1
    assert len(requests) == 2
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "thinking" not in requests[1]


def test_gateway_default_json_and_text_behavior_remains_reasoning_compatible():
    responses = [
        _chat_response(content="", reasoning_content='{"legacy":true}'),
        _chat_response(content="", reasoning_content="legacy text"),
    ]

    def handler(_request):
        return httpx.Response(200, json=responses.pop(0))

    gateway = _gateway(handler, profile_name="ordinary")
    try:
        json_result = gateway.call("ordinary", {"messages": []}, "json")
        text_result = gateway.call("ordinary", {"messages": []}, "text")
    finally:
        gateway.close()

    assert json_result.output == {"legacy": True}
    assert text_result.output == "legacy text"
    assert json_result.final_content_only is False
    assert text_result.final_content_only is False


def test_detector_counts_compatibility_attempts_and_does_not_exceed_batch_budget(
    monkeypatch,
):
    import services.discussion_pipeline_v2.llm_state_detector as detector
    from services.llm_gateway import LlmResult

    raw_text = json.dumps(
        _chat_response(
            content="",
            reasoning_content='{"sub_category":"confusion"}',
            finish_reason="length",
            usage={
                "completion_tokens": 1800,
                "completion_tokens_details": {"reasoning_tokens": 1800},
            },
        )
    )
    result = LlmResult(
        success=False,
        output=None,
        raw_text=raw_text,
        model_name="batch12-model",
        profile_name="state_detector",
        attempt_count=2,
        token_usage={
            "completion_tokens": 1800,
            "completion_tokens_details": {"reasoning_tokens": 1800},
        },
        finish_reason="length",
        failure_type="reasoning_budget_exhausted",
        failure_message="final content empty",
        compatibility_fallback_count=1,
        final_content_only=True,
    )

    class FakeGateway:
        def __init__(self):
            self.calls = []
            self.profiles = {
                "state_detector": SimpleNamespace(
                    model="batch12-model",
                    read_timeout=45,
                    retries=0,
                )
            }

        def call(self, profile, payload, response_type):
            self.calls.append((profile, payload, response_type))
            return result

    gateway = FakeGateway()
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "STATE_LLM_SCHEMA_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(detector, "get_gateway", lambda: gateway)
    monkeypatch.setattr(detector, "record_latency_event", lambda **_kwargs: None)
    monkeypatch.setattr(detector, "safe_write_audit_log", lambda **_kwargs: True)

    envelope = detector.LLMStateDetector.detect(
        {
            "group_id": 1,
            "batch_scheduler_attempt_count": 1,
            "batch_scheduler_max_attempts": 2,
            "recent_student_messages": [
                {
                    "id": 101,
                    "sequence": 1,
                    "role": "student",
                    "user_id": 1,
                    "content": "I do not know the first step.",
                }
            ],
        }
    )

    assert len(gateway.calls) == 1
    assert gateway.calls[0][1]["thinking"] == {"type": "disabled"}
    assert gateway.calls[0][1]["_sera_final_content_only"] is True
    assert envelope["meta"]["failure_type"] == "reasoning_budget_exhausted"
    assert envelope["meta"]["failure_category"] == "reasoning_budget_exhausted"
    assert envelope["meta"]["detector_attempt_count"] == 1
    assert envelope["meta"]["external_call_count"] == 2
    assert envelope["meta"]["repair_attempt_count"] == 0
    assert envelope["meta"]["gateway_retry_count"] == 0
    assert envelope["meta"]["compatibility_fallback_count"] == 1
    assert envelope["meta"]["batch_scheduler_attempt_count"] == 1


def test_scheduler_does_not_requeue_reasoning_budget_exhaustion(monkeypatch):
    import services.state_assessment_scheduler as scheduler

    terminal = {
        "batch": {
            "id": 9,
            "status": "failed",
            "terminal_status": "quarantined",
        }
    }
    monkeypatch.setattr(
        scheduler.StateAssessmentBatchService,
        "terminalize_exhausted_batch",
        staticmethod(lambda *_args, **_kwargs: terminal),
    )

    result = scheduler._schedule_limited_retry(
        {
            "id": 9,
            "group_id": 1,
            "session_id": 2,
            "discussion_id": 3,
            "candidate_start_sequence": 1,
            "candidate_end_sequence": 2,
            "attempt_count": 1,
            "max_attempts": 2,
        },
        error_code="reasoning_budget_exhausted",
        error_detail="final content empty",
    )

    assert result["retried"] is False
    assert result["reason"] == "structured_output_retry_exhausted"
    assert result["terminal"] == terminal
