# -*- coding: utf-8 -*-
"""Batch 1 tests for the public final-state ontology."""

from __future__ import annotations

from services.three_stage_schema import (
    CANONICAL_SUB_STATE_SEMANTICS,
    FINAL_SUB_STATE_CODES,
    INTERVENTION_SUB_STATE_CODES,
    LEGACY_STATE_CODES,
    NON_INTERVENTION_SUB_STATE_CODES,
    PRIMARY_SUB_STATE_CODES,
    PROCESS_STATE_CODES,
    STAGE2_MODEL_OUTPUT_STATE_CODES,
    STATE_OVERLAY_CODES,
    FinalMessageStateResolver,
    is_legacy_state,
    is_primary_sub_state,
    is_process_state,
    is_state_overlay,
    legacy_state_for_sub_state,
)
from services.three_stage_strategy_library import (
    load_strategy_library,
    route_for_sub_state,
    validate_strategy_library,
)


def test_state_ontology_layers_are_explicit_and_disjoint():
    assert FINAL_SUB_STATE_CODES == PRIMARY_SUB_STATE_CODES
    assert len(FINAL_SUB_STATE_CODES) == 12
    assert len(STATE_OVERLAY_CODES) == 3
    assert "individual_marginalization" in FINAL_SUB_STATE_CODES
    assert "unknown_sub_state" in PROCESS_STATE_CODES

    assert not (set(FINAL_SUB_STATE_CODES) & set(STATE_OVERLAY_CODES))
    assert not (set(FINAL_SUB_STATE_CODES) & set(PROCESS_STATE_CODES))
    assert not (set(FINAL_SUB_STATE_CODES) & set(LEGACY_STATE_CODES))

    for code in FINAL_SUB_STATE_CODES:
        assert is_primary_sub_state(code)
    for code in STATE_OVERLAY_CODES:
        assert is_state_overlay(code)
        assert not is_primary_sub_state(code)
    for code in PROCESS_STATE_CODES:
        assert is_process_state(code)
        assert not is_primary_sub_state(code)
    for code in LEGACY_STATE_CODES:
        assert is_legacy_state(code)
        assert not is_primary_sub_state(code)


def test_model_primary_output_excludes_overlays_but_keeps_unknown_process_state():
    assert set(FINAL_SUB_STATE_CODES).issubset(set(STAGE2_MODEL_OUTPUT_STATE_CODES))
    assert "unknown_sub_state" in STAGE2_MODEL_OUTPUT_STATE_CODES
    for overlay in STATE_OVERLAY_CODES:
        assert overlay not in STAGE2_MODEL_OUTPUT_STATE_CODES


def test_model_semantics_cover_every_output_and_separate_thinking_from_execution():
    assert set(CANONICAL_SUB_STATE_SEMANTICS) == set(STAGE2_MODEL_OUTPUT_STATE_CODES)
    for semantics in CANONICAL_SUB_STATE_SEMANTICS.values():
        assert semantics["definition"]
        assert semantics["boundary"]

    thinking = CANONICAL_SUB_STATE_SEMANTICS["deep_thinking"]
    execution = CANONICAL_SUB_STATE_SEMANTICS["execution_progress"]
    assert "30到120秒" in thinking["definition"]
    assert "固定分工" in thinking["boundary"]
    assert "决策前" in execution["boundary"]

    burnout = CANONICAL_SUB_STATE_SEMANTICS["burnout"]
    perfunctory = CANONICAL_SUB_STATE_SEMANTICS["perfunctory_detachment"]
    assert "价值质疑" in burnout["definition"]
    assert "敷衍脱离" in burnout["boundary"]
    assert "倦怠" in perfunctory["boundary"]
    assert "最低限度" in perfunctory["boundary"]
    assert "稳定针对同一成员" in perfunctory["boundary"]

    execution = CANONICAL_SUB_STATE_SEMANTICS["execution_progress"]
    marginalization = CANONICAL_SUB_STATE_SEMANTICS["individual_marginalization"]
    assert "优先判为个体边缘化" in execution["boundary"]
    assert "重复提出任务建议" in marginalization["boundary"]
    assert "执行推进" in marginalization["boundary"]
    assert "防御性退出" in marginalization["boundary"]
    assert "人际冲突" in marginalization["boundary"]
    assert "敷衍脱离" in marginalization["boundary"]

    interpersonal = CANONICAL_SUB_STATE_SEMANTICS["interpersonal_conflict"]
    assert "相互攻击" in interpersonal["boundary"]
    assert "算了、按你们的来" in interpersonal["boundary"]


def test_final_message_state_resolver_projects_overlay_compatibly():
    resolved = FinalMessageStateResolver.resolve(
        canonical_sub_state_code="execution_progress",
        secondary_tags_json='["stage_achievement"]',
        coarse_state_code="positive_collaboration",
        confidence=0.86,
        segment_id=12,
        assessment_batch_id=10,
        strategy_pipeline_run_id=24,
        inhibition_strategy_id="OI-004",
    )

    assert resolved["final_sub_state_code"] == "execution_progress"
    assert resolved["final_sub_state_label"] == "执行推进"
    assert resolved["coarse_state_code"] == "positive_collaboration"
    assert resolved["legacy_state_code"] == "positive_collaboration"
    assert resolved["state_overlays"] == ["stage_achievement"]
    assert resolved["assessment_status"] == "confirmed"

    overlay_only = FinalMessageStateResolver.resolve(
        canonical_sub_state_code="psychological_safety_risk",
        state_code="conflict_tension",
    )
    assert overlay_only["final_sub_state_code"] == "interpersonal_conflict"
    assert overlay_only["state_overlays"] == ["psychological_safety_risk"]
    assert overlay_only["assignment_source"] == "legacy_overlay_projection"
    assert overlay_only["inferred"] is True

    process = FinalMessageStateResolver.resolve(
        canonical_sub_state_code="unknown_sub_state",
    )
    assert process["final_sub_state_code"] is None
    assert process["assignment_source"] == "unknown_sub_state"

    legacy_only = FinalMessageStateResolver.resolve(state_code="task_detached")
    assert legacy_only["final_sub_state_code"] is None
    assert legacy_only["legacy_state_code"] == "task_detached"
    assert legacy_only["assignment_source"] == "legacy_coarse_only"


def test_legacy_projection_is_coarse_compatibility_only():
    assert legacy_state_for_sub_state("execution_progress") == "positive_collaboration"
    assert legacy_state_for_sub_state("interpersonal_conflict") == "conflict_tension"
    assert legacy_state_for_sub_state("unknown_sub_state") == "unknown"
    for legacy in LEGACY_STATE_CODES:
        assert legacy not in FINAL_SUB_STATE_CODES


def test_strategy_routes_and_oi_exclusivity_remain_valid():
    library = load_strategy_library()
    by_id = library.by_id

    assert len(by_id) == 28
    assert validate_strategy_library() == []
    for canonical in FINAL_SUB_STATE_CODES:
        route = route_for_sub_state(canonical)
        assert route["canonical_sub_state"] == canonical
        assert set(route["candidate_strategy_ids"]).issubset(by_id)

    for overlay in STATE_OVERLAY_CODES:
        route = route_for_sub_state(overlay)
        assert route["canonical_sub_state"] == overlay
        assert set(route["candidate_strategy_ids"]).issubset(by_id)
        assert overlay not in INTERVENTION_SUB_STATE_CODES
        assert overlay not in NON_INTERVENTION_SUB_STATE_CODES

    for canonical, oi_strategy in {
        "constructive_conflict": "OI-001",
        "deep_thinking": "OI-002",
        "off_topic_self_regulated": "OI-003",
        "execution_progress": "OI-004",
    }.items():
        route = route_for_sub_state(canonical)
        assert route["candidate_strategy_ids"] == [oi_strategy]
        assert route["should_intervene"] is False
        assert route["inhibition_strategy_id"] == oi_strategy


def test_teacher_detailed_state_system_uses_only_primary_final_states():
    from services.teacher_emotion_trend_service import _detailed_state_system

    system = _detailed_state_system()
    detailed_codes = {
        item["code"] for item in system if item.get("is_detailed")
    }
    assert detailed_codes == set(FINAL_SUB_STATE_CODES)
    assert not (detailed_codes & set(STATE_OVERLAY_CODES))
