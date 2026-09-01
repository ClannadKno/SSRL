# -*- coding: utf-8 -*-
"""Batch 17 coverage for Stage 2 semantics and message display labels."""

from __future__ import annotations

import json

import pytest


class _FakeLlmResult:
    def __init__(self, output):
        self.success = True
        self.output = output
        self.raw_text = json.dumps(output, ensure_ascii=False)
        self.model_name = "batch17-state-model"
        self.latency_ms = 1
        self.attempt_count = 1
        self.token_usage = None
        self.failure_type = None
        self.failure_message = None
        self.fallback_required = False
        self.finish_reason = "stop"


def _state_only(sub_category, canonical_state):
    return {
        "sub_category": sub_category,
        "canonical_state": canonical_state,
        "confidence": 0.86,
        "evidence_message_ids": [1, 2],
    }


def _detector_context():
    return {
        "group_id": 1,
        "recent_student_messages": [
            {
                "id": 11,
                "sequence": 1,
                "role": "student",
                "user_id": 1,
                "content": "做了也白做，我不想继续了。",
                "created_at": "2026-08-02 10:00:00",
            },
            {
                "id": 12,
                "sequence": 2,
                "role": "student",
                "user_id": 2,
                "content": "这个任务没有意义。",
                "created_at": "2026-08-02 10:00:10",
            },
        ],
    }


def test_semantic_mismatch_repair_receives_bounded_compatibility_map(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []
    outputs = [
        _state_only("burnout", "frustration"),
        _state_only("burnout", "burnout"),
    ]

    class FakeGateway:
        def call(self, profile, payload, response_type):
            calls.append(payload)
            return _FakeLlmResult(outputs[len(calls) - 1])

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_detector_context())

    assert len(calls) == 2
    assert envelope["meta"]["validation_status"] == "passed"
    assert envelope["result"]["active_sub_state"]["canonical_sub_state"] == "burnout"
    assert envelope["meta"]["validation_attempts"][0]["failure_category"] == (
        "sub_category_canonical_mismatch"
    )
    repair_context = json.loads(calls[1]["messages"][1]["content"])
    compatibility = repair_context[
        "sub_category_to_compatible_canonical_states"
    ]
    assert compatibility == detector.SUB_CATEGORY_CANONICAL_COMPATIBILITY
    assert compatibility["burnout"] == ["burnout"]
    assert compatibility["stage_achievement"] == ["execution_progress"]
    assert set(compatibility["high_intensity_overload"]) == {
        "interpersonal_conflict",
        "standard",
        "frustration",
    }
    assert repair_context["validation_error"] == "sub_category_canonical_mismatch"
    assert "sub_category_to_compatible_canonical_states" in calls[1]["messages"][0]["content"]


def test_repeated_semantic_mismatch_fails_closed_with_specific_category(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = []

    class FakeGateway:
        def call(self, profile, payload, response_type):
            calls.append(payload)
            return _FakeLlmResult(_state_only("burnout", "frustration"))

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    envelope = detector.LLMStateDetector.detect(_detector_context())

    assert len(calls) == 2
    assert envelope["result"]["detector_error"] is True
    assert envelope["result"]["primary_state"] == "unknown"
    assert envelope["meta"]["schema_error"] == "sub_category_canonical_mismatch"
    assert envelope["meta"]["failure_category"] == (
        "sub_category_canonical_mismatch"
    )
    assert {
        item["failure_category"]
        for item in envelope["meta"]["validation_attempts"]
    } == {"sub_category_canonical_mismatch"}


@pytest.mark.parametrize(
    "sub_category,canonical_state",
    [
        ("psychological_safety_risk", "interpersonal_conflict"),
        ("high_intensity_overload", "standard"),
        ("high_intensity_overload", "frustration"),
        ("stage_achievement", "execution_progress"),
    ],
)
def test_overlay_primary_compatible_pairs_are_accepted(
    sub_category,
    canonical_state,
):
    from services.discussion_pipeline_v2.llm_state_detector import (
        parse_llm_json_content,
    )

    parsed = parse_llm_json_content(
        _state_only(sub_category, canonical_state),
        candidate_sequences=[1, 2],
    )

    assert parsed["valid"] is True
    assert parsed["data"]["active_sub_state"]["canonical_sub_state"] == canonical_state
    assert parsed["data"]["active_sub_state"]["secondary_tags"] == [sub_category]


@pytest.mark.parametrize(
    "sub_category,canonical_state",
    [
        ("psychological_safety_risk", "standard"),
        ("high_intensity_overload", "execution_progress"),
        ("stage_achievement", "standard"),
    ],
)
def test_overlay_primary_incompatible_pairs_are_rejected(
    sub_category,
    canonical_state,
):
    from services.discussion_pipeline_v2.llm_state_detector import (
        parse_llm_json_content,
    )

    parsed = parse_llm_json_content(
        _state_only(sub_category, canonical_state),
        candidate_sequences=[1, 2],
    )

    assert parsed["valid"] is False
    assert parsed["error_type"] == "sub_category_canonical_mismatch"


def _message(sequence):
    return {
        "id": sequence,
        "sequence": sequence,
        "group_id": 1,
        "session_id": 1,
        "discussion_id": 1,
        "role": "student",
        "sender_type": "student",
        "created_at": "2026-08-02 10:00:%02d" % sequence,
    }


def _batch17_assignment_fixture():
    messages = [_message(sequence) for sequence in range(1, 6)]
    segments = [
        {
            "id": 101,
            "group_id": 1,
            "session_id": 1,
            "discussion_id": 1,
            "start_sequence": 2,
            "end_sequence": 2,
            "state_code": "positive_collaboration",
            "canonical_sub_state_code": "execution_progress",
            "source": "llm",
            "assessment_status": "confirmed",
            "assessment_batch_id": 11,
            "batch_status": "succeeded",
            "confidence": 0.9,
        },
        {
            "id": 102,
            "group_id": 1,
            "session_id": 1,
            "discussion_id": 1,
            "start_sequence": 4,
            "end_sequence": 4,
            "state_code": "unknown",
            "canonical_sub_state_code": None,
            "process_sub_state_code": "unknown_sub_state",
            "source": "llm",
            "assessment_status": "confirmed",
            "assessment_batch_id": 12,
            "batch_status": "succeeded",
            "confidence": 0.42,
        },
    ]
    batch_windows = [
        {
            "batch_id": 11,
            "start_sequence": 1,
            "end_sequence": 2,
            "status": "succeeded",
        },
        {
            "batch_id": 12,
            "start_sequence": 3,
            "end_sequence": 4,
            "status": "succeeded",
        },
        {
            "batch_id": 13,
            "start_sequence": 5,
            "end_sequence": 5,
            "status": "failed",
            "terminal_status": "quarantined",
            "error_code": "sub_category_canonical_mismatch",
        },
    ]
    return messages, segments, batch_windows


def test_message_labels_distinguish_success_unknown_and_failure_without_backfill():
    from services.message_state_assignment_service import (
        FAILED_UNCLASSIFIED_STATE,
        SUCCESS_UNCOVERED_STATE,
        UNKNOWN_SUB_STATE,
        assign_message_states,
    )

    messages, segments, batch_windows = _batch17_assignment_fixture()
    payload = assign_message_states(
        messages=messages,
        state_segments=segments,
        state_assessment_batches=batch_windows,
        display_context={"discussion_id": 1, "has_assessment_pipeline": True},
        group_id=1,
        session_id=1,
        discussion_id=1,
    )
    assignments = payload["by_sequence"]

    assert all(assignments[sequence]["display_state_label"] for sequence in range(1, 6))
    assert assignments[1]["display_state_code"] == SUCCESS_UNCOVERED_STATE
    assert assignments[1]["final_sub_state_code"] is None
    assert assignments[2]["final_sub_state_code"] == "execution_progress"
    assert assignments[3]["display_state_code"] == SUCCESS_UNCOVERED_STATE
    assert assignments[3]["final_sub_state_code"] is None
    assert assignments[4]["display_state_code"] == UNKNOWN_SUB_STATE
    assert assignments[4]["assignment_source"] == "unknown_sub_state_segment"
    assert assignments[4]["final_sub_state_code"] is None
    assert assignments[5]["display_state_code"] == FAILED_UNCLASSIFIED_STATE
    assert assignments[5]["error_code"] == "sub_category_canonical_mismatch"
    assert assignments[5]["final_sub_state_code"] is None

    summary = payload["summary"]
    assert summary["success_uncovered_student_message_count"] == 2
    assert summary["unknown_sub_state_student_message_count"] == 1
    assert summary["failed_unclassified_student_message_count"] == 1
    assert summary["other_unclassified_student_message_count"] == 0
    assert summary["unclassified_category_count_invariant"] is True
    assert summary["student_state_count_invariant"] is True


def test_teacher_trend_preserves_unknown_as_process_state_not_formal_primary(
    monkeypatch,
):
    import services.teacher_emotion_trend_service as trend
    from services.message_state_assignment_service import assign_message_states

    message = _message(4)
    monkeypatch.setattr(
        trend,
        "_student_messages_by_sequence",
        lambda *_args, **_kwargs: {4: message},
    )
    monkeypatch.setattr(
        trend,
        "_assessment_display_context",
        lambda *_args, **_kwargs: {
            "discussion_id": 1,
            "has_assessment_pipeline": True,
            "batch_windows": [
                {
                    "batch_id": 12,
                    "start_sequence": 4,
                    "end_sequence": 4,
                    "status": "succeeded",
                }
            ],
            "observation_status": "inactive",
        },
    )
    monkeypatch.setattr(
        trend,
        "_read_segment_rows",
        lambda *_args, **_kwargs: [
            {
                "id": 102,
                "group_id": 1,
                "session_id": 1,
                "discussion_id": 1,
                "segment_kind": "message_range",
                "start_sequence": 4,
                "end_sequence": 4,
                "state_code": "unknown",
                "canonical_sub_state_code": "unknown_sub_state",
                "raw_sub_state_code": "unknown_sub_state",
                "source": "llm",
                "assessment_status": "confirmed",
                "assessment_batch_id": 12,
                "batch_status": "succeeded",
                "confidence": 0.42,
                "is_finalized": 1,
                "created_at": message["created_at"],
                "updated_at": message["created_at"],
            }
        ],
    )

    state_segments, _silence, _warnings, context, _student_index = (
        trend._build_segments(
            1,
            1,
            "2026-08-02 09:59:00",
            "2026-08-02 10:01:00",
            {},
        )
    )

    assert len(state_segments) == 1
    assert state_segments[0]["final_sub_state_code"] is None
    assert state_segments[0]["canonical_sub_state_code"] is None
    assert state_segments[0]["process_sub_state_code"] == "unknown_sub_state"
    assigned = assign_message_states(
        messages=[message],
        state_segments=state_segments,
        display_context=context,
        group_id=1,
        session_id=1,
        discussion_id=1,
    )["by_sequence"][4]
    assert assigned["final_sub_state_code"] is None
    assert assigned["display_state_code"] == "unknown_sub_state"
    assert assigned["display_state_label"] == "子状态不确定"


def test_teacher_api_projection_and_export_share_display_contract():
    from services.message_state_assignment_service import assign_message_states
    from services.teacher_emotion_review_service import _assign_state_to_messages
    from services.teacher_export_service import _decorate_message_assignment

    messages, segments, batch_windows = _batch17_assignment_fixture()
    context = {
        "discussion_id": 1,
        "has_assessment_pipeline": True,
        "batch_windows": batch_windows,
    }
    assignment_payload = assign_message_states(
        messages=messages,
        state_segments=segments,
        display_context=context,
        group_id=1,
        session_id=1,
        discussion_id=1,
    )
    api_messages = [dict(message) for message in messages]
    _assign_state_to_messages(
        api_messages,
        segments,
        context,
        group_id=1,
        session_id=1,
        discussion_id=1,
    )

    for message in api_messages:
        assignment = assignment_payload["by_sequence"][message["sequence"]]
        export_row = _decorate_message_assignment(message, assignment, None)
        assert message["display_state_code"] == assignment["display_state_code"]
        assert message["display_state_label"] == assignment["display_state_label"]
        assert message["state_label"] == assignment["display_state_label"]
        assert export_row["display_state_code"] == assignment["display_state_code"]
        assert export_row["display_state_label"] == assignment["display_state_label"]
        assert export_row["final_sub_state_code"] == (
            assignment["final_sub_state_code"] or ""
        )
