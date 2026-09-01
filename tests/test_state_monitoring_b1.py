# -*- coding: utf-8 -*-
import pytest


def test_state_meta_only_contains_final_primary_states():
    from knowledge_base import FINAL_STATE_CODES, LEGACY_PRIMARY_STATE_CODES, STATE_META

    assert set(FINAL_STATE_CODES) == {
        "positive_collaboration",
        "conflict_tension",
        "negative_silence",
        "blocked_frustration",
        "task_detached",
        "unknown",
    }
    assert set(STATE_META) == set(FINAL_STATE_CODES)
    assert not (set(STATE_META) & set(LEGACY_PRIMARY_STATE_CODES))


def test_legacy_state_codes_normalize_to_final_states():
    from knowledge_base import normalize_state_payload

    participation = normalize_state_payload("participation_imbalance")
    assert participation["state_code"] == "unknown"
    assert participation["legacy_state_code"] == "participation_imbalance"
    assert "participation_imbalance" in participation["evidence_tags"]

    coordination = normalize_state_payload("coordination_disorder")
    assert coordination["state_code"] == "blocked_frustration"

    repaired = normalize_state_payload("conflict_repair", evidence_tags=["return_to_task"])
    assert repaired["state_code"] == "positive_collaboration"

    insufficient = normalize_state_payload("insufficient_evidence")
    assert insufficient["state_code"] == "unknown"


def test_rule_candidates_never_use_legacy_primary_states():
    from services.rule_state_service import detect_group_state_rule

    rows = [
        {"role": "student", "user_id": 1, "content": "我先继续说一下"},
        {"role": "student", "user_id": 1, "content": "我再补充一个点"},
        {"role": "student", "user_id": 1, "content": "我继续整理"},
        {"role": "student", "user_id": 1, "content": "我最后总结"},
    ]
    context = {
        "window_messages": rows,
        "window_student_messages": rows,
        "recent_student_messages": rows,
        "recent_checkins": [],
        "checkin_summary": {},
        "page_activity": {"active_students": 4, "active_duration_seconds": 60},
        "participant_count": 4,
    }
    features = {
        "behavior": {
            "participation_imbalance": 1.0,
            "low_window_student_message_count": 4,
            "low_window_active_members": 1,
        },
        "text": {},
    }

    result = detect_group_state_rule(context, features)
    candidate_codes = {item["state_code"] for item in result["candidates"]}
    assert "participation_imbalance" not in candidate_codes
    assert "coordination_disorder" not in candidate_codes
    assert result["winning_state_code"] in candidate_codes


def test_fusion_normalizes_legacy_rule_state_before_persistence_payload():
    from services.state_assessment_service import fuse_state_evidence

    fusion = fuse_state_evidence(
        {
            "state_code": "coordination_disorder",
            "state_score": 0.81,
            "rule_assessment": {"assessment_status": "state_detected"},
        }
    )

    assert fusion["fused_state_code"] == "blocked_frustration"
    assert fusion["legacy_state_code"] == "coordination_disorder"
    assert fusion["normalization_reason"] == "legacy_coordination_disorder_normalized"


def test_fusion_blocks_intervention_when_required_llm_validation_fails():
    from services.state_assessment_service import fuse_state_evidence

    fusion = fuse_state_evidence(
        {
            "state_code": "conflict_tension",
            "state_score": 0.86,
            "rule_assessment": {"assessment_status": "state_detected"},
        },
        llm_result=None,
        llm_meta={
            "llm_required": True,
            "analysis_failed": True,
            "validation_status": "failed",
            "schema_valid": False,
            "failure_reason": "json_parse_failed",
        },
    )

    assert fusion["fused_state_code"] == "conflict_tension"
    assert fusion["assessment_status"] == "uncertain"
    assert fusion["should_intervene"] is False
    assert fusion["decision_source"] == "rule_high_confidence_fallback"


def test_llm_primary_state_parser_allows_unknown_and_rejects_legacy_states():
    from services.llm_analyzer import ALLOWED_PRIMARY_STATES, _normalize_result

    assert "unknown" in ALLOWED_PRIMARY_STATES
    assert "insufficient_evidence" not in ALLOWED_PRIMARY_STATES
    assert "participation_imbalance" not in ALLOWED_PRIMARY_STATES

    meta = {"model_name": "test-model", "prompt_version": "test", "latency_ms": 1}
    normalized = _normalize_result(
        {
            "primary_state": "positive_collaboration",
            "secondary_flags": [],
            "confidence": 0.2,
            "assessment_status": "insufficient_evidence",
            "self_regulation_detected": False,
            "should_intervene_recommendation": False,
            "evidence_sentences": [],
            "reason": "",
        },
        meta,
    )
    assert normalized["state_code"] == "unknown"

    with pytest.raises(ValueError, match="invalid_primary_state"):
        _normalize_result(
            {
                "primary_state": "participation_imbalance",
                "secondary_flags": [],
                "confidence": 0.8,
                "assessment_status": "confirmed",
                "self_regulation_detected": False,
                "should_intervene_recommendation": False,
                "evidence_sentences": [],
                "reason": "",
            },
            meta,
        )


def test_teacher_trend_maps_legacy_states_to_segment_display_system():
    from services.teacher_emotion_trend_service import _DISTRIBUTION_KEYS, _map_state_code, _map_state_label
    from services.three_stage_schema import FINAL_SUB_STATE_CODES

    assert _DISTRIBUTION_KEYS == [
        *FINAL_SUB_STATE_CODES,
        "observing",
        "unclassified",
    ]
    assert _map_state_code("coordination_disorder") == "observing"
    assert _map_state_code("task_detached") == "observing"
    assert _map_state_code("insufficient_evidence") == "unclassified"
    assert _map_state_label("insufficient_evidence") == "未分类"
