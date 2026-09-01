# -*- coding: utf-8 -*-
"""Batch 2 tests for per-message final state assignment."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.message_state_assignment_service import assign_message_states


def _stamp(offset_seconds: int) -> str:
    base = datetime(2026, 7, 27, 12, 0, 0)
    return (base + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _message(sequence, *, role="student", discussion_id=1):
    return {
        "id": sequence,
        "group_id": 1,
        "session_id": 1,
        "discussion_id": discussion_id,
        "sequence": sequence,
        "role": role,
        "sender_type": role,
        "created_at": _stamp(sequence * 10),
    }


def _primary_segment(
    segment_id,
    start,
    end,
    final_code,
    *,
    source="llm",
    discussion_id=1,
    batch_id=None,
):
    return {
        "id": segment_id,
        "group_id": 1,
        "session_id": 1,
        "discussion_id": discussion_id,
        "start_sequence": start,
        "end_sequence": end,
        "final_sub_state_code": final_code,
        "canonical_sub_state_code": final_code,
        "coarse_state_code": "positive_collaboration",
        "source": source,
        "assessment_status": "confirmed",
        "assessment_batch_id": batch_id,
        "batch_status": "succeeded" if batch_id else None,
        "confidence": 0.91,
        "is_finalized": True,
    }


def _legacy_segment(segment_id, start, end, coarse_code):
    return {
        "id": segment_id,
        "group_id": 1,
        "session_id": 1,
        "discussion_id": 1,
        "start_sequence": start,
        "end_sequence": end,
        "state_code": coarse_code,
        "coarse_state_code": coarse_code,
        "source": "state_monitor",
        "assessment_status": "confirmed",
        "confidence": 0.7,
        "is_finalized": True,
    }


def test_audit_32_message_shape_gets_complete_student_display_assignment():
    messages = [
        _message(sequence, role=("agent" if sequence == 23 else "student"))
        for sequence in range(1, 33)
    ]
    state_segments = [
        _primary_segment(1, 4, 9, "execution_progress", batch_id=11),
        _primary_segment(2, 12, 19, "constructive_conflict", batch_id=12),
        _primary_segment(3, 24, 28, "frustration", batch_id=13),
        _legacy_segment(4, 4, 11, "positive_collaboration"),
    ]
    batch_windows = [
        {
            "batch_id": 101,
            "start_sequence": 1,
            "end_sequence": 3,
            "status": "failed",
            "terminal_status": "quarantined",
            "error_code": "truncated_response",
        },
        {
            "batch_id": 102,
            "start_sequence": 20,
            "end_sequence": 22,
            "status": "succeeded",
        },
        {
            "batch_id": 103,
            "start_sequence": 27,
            "end_sequence": 30,
            "status": "failed",
            "terminal_status": "quarantined",
            "error_code": "schema_validation_error",
        },
        {
            "batch_id": 104,
            "start_sequence": 31,
            "end_sequence": 32,
            "status": "running",
        },
    ]
    silence_segments = [
        {
            "id": 90,
            "segment_kind": "time_range",
            "source": "silence_rule",
            "start_at": _stamp(225),
            "end_at": _stamp(235),
            "previous_student_message_id": 22,
            "next_student_message_id": 24,
        }
    ]

    payload = assign_message_states(
        messages=messages,
        state_segments=state_segments,
        state_assessment_batches=batch_windows,
        display_context={
            "discussion_id": 1,
            "has_assessment_pipeline": True,
            "observation_status": "inactive",
        },
        silence_segments=silence_segments,
        group_id=1,
        session_id=1,
        discussion_id=1,
    )

    assignments = payload["by_sequence"]
    summary = payload["summary"]

    assert summary["student_message_count"] == 31
    assert summary["display_assigned_student_message_count"] == 31
    assert summary["student_display_assignment_rate"] == 1.0
    assert summary["precise_sub_state_message_count"] == 19
    assert summary["legacy_monitor_only_message_count"] == 2
    assert summary["student_state_count_invariant"] is True

    assert assignments[4]["final_sub_state_code"] == "execution_progress"
    assert assignments[9]["final_sub_state_code"] == "execution_progress"
    assert assignments[10]["final_sub_state_code"] is None
    assert assignments[10]["coarse_state_code"] == "positive_collaboration"
    assert assignments[10]["assignment_source"] == "legacy_monitor_only"
    assert assignments[10]["assessment_status"] == "unclassified"
    assert assignments[23]["role"] == "agent"
    assert assignments[23]["assessment_status"] is None
    assert assignments[23]["context_state_code"] == "constructive_conflict"
    assert assignments[24]["final_sub_state_code"] == "frustration"
    assert assignments[27]["final_sub_state_code"] == "frustration"
    assert assignments[29]["assignment_source"] == "batch_unclassified"
    assert assignments[29]["error_code"] == "schema_validation_error"
    assert assignments[31]["assignment_source"] == "awaiting_active_batch"
    assert assignments[31]["assessment_status"] == "observing"
    assert not any(
        item.get("assignment_source") == "silence_rule"
        for item in payload["assignments"]
    )


def test_short_gap_carry_forward_is_explicit_inferred_and_finite():
    messages = [_message(sequence) for sequence in range(1, 6)]
    payload = assign_message_states(
        messages=messages,
        state_segments=[
            _primary_segment(1, 1, 2, "standard", source="session_finalizer")
        ],
        display_context={"discussion_id": 1},
        group_id=1,
        session_id=1,
        discussion_id=1,
    )

    assignments = payload["by_sequence"]

    assert assignments[1]["assignment_source"] == "session_finalizer_segment"
    assert assignments[3]["assignment_source"] == "carry_forward"
    assert assignments[3]["final_sub_state_code"] == "standard"
    assert assignments[3]["inferred"] is True
    assert assignments[4]["assignment_source"] == "carry_forward"
    assert assignments[4]["inferred"] is True
    assert assignments[5]["assignment_source"] == "awaiting_detection_conditions"
    assert assignments[5]["assessment_status"] == "observing"


@pytest.mark.parametrize(
    "messages,display_context,batches,silence_segments,expected_source,expected_status",
    [
        (
            [_message(1), _message(2), _message(3, discussion_id=2)],
            {"discussion_id": 1},
            [],
            [],
            "awaiting_detection_conditions",
            "observing",
        ),
        (
            [_message(1), _message(2), _message(3)],
            {"discussion_id": 1, "last_intervention_sequence": 2},
            [],
            [],
            "awaiting_detection_conditions",
            "observing",
        ),
        (
            [_message(1), _message(2), _message(3)],
            {"discussion_id": 1},
            [
                {
                    "batch_id": 7,
                    "start_sequence": 3,
                    "end_sequence": 3,
                    "status": "failed",
                    "terminal_status": "quarantined",
                    "error_code": "truncated_response",
                }
            ],
            [],
            "batch_unclassified",
            "unclassified",
        ),
        (
            [_message(1), _message(2), _message(3)],
            {"discussion_id": 1},
            [],
            [
                {
                    "id": 8,
                    "source": "silence_rule",
                    "previous_student_message_id": 2,
                    "next_student_message_id": 3,
                    "start_at": _stamp(20),
                    "end_at": _stamp(30),
                }
            ],
            "awaiting_detection_conditions",
            "observing",
        ),
    ],
)
def test_carry_forward_does_not_cross_scope_or_process_boundaries(
    messages,
    display_context,
    batches,
    silence_segments,
    expected_source,
    expected_status,
):
    payload = assign_message_states(
        messages=messages,
        state_segments=[_primary_segment(1, 1, 2, "deep_thinking")],
        state_assessment_batches=batches,
        display_context=display_context,
        silence_segments=silence_segments,
        group_id=1,
        session_id=1,
        discussion_id=display_context.get("discussion_id"),
    )

    assignment = payload["by_sequence"][3]
    assert assignment["assignment_source"] == expected_source
    assert assignment["assessment_status"] == expected_status
    assert assignment["final_sub_state_code"] is None
