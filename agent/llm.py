# -*- coding: utf-8 -*-
"""Compatibility wrappers around the Phase 7 LLM analyzer service."""
from knowledge_base import STATE_META
from services.llm_analyzer import (
    analyze_group_llm as _analyze_group_llm,
    run_group_llm_analysis as _run_group_llm_analysis,
)


def _coerce_context_and_features(context_rows, rule_state=None):
    if isinstance(context_rows, dict):
        context = dict(context_rows)
    else:
        context = {}
        if isinstance(rule_state, dict) and isinstance(rule_state.get("context_json"), dict):
            context = dict(rule_state["context_json"])
        if context_rows:
            context["recent_student_messages"] = list(context_rows)
        else:
            context.setdefault("recent_student_messages", [])

    features = {}
    if isinstance(rule_state, dict) and isinstance(rule_state.get("feature_json"), dict):
        features = rule_state["feature_json"]
    return context, features


def run_group_llm_analysis(group_id, context_rows, rule_state=None, request_impl=None):
    context, features = _coerce_context_and_features(context_rows, rule_state=rule_state)
    return _run_group_llm_analysis(
        group_id,
        context,
        features=features,
        rule_result=rule_state,
        request_impl=request_impl,
    )


def analyze_group_llm(group_id, context_rows, rule_state=None, request_impl=None):
    return run_group_llm_analysis(
        group_id,
        context_rows,
        rule_state=rule_state,
        request_impl=request_impl,
    )["result"]


def merge_state_with_llm(rule_state, llm_result):
    if not llm_result:
        return rule_state

    merged = dict(rule_state)
    state_code = llm_result.get("primary_state") or llm_result.get("state_code")
    if state_code in STATE_META and llm_result.get("assessment_status") != "insufficient_evidence":
        state_label, risk_level, risk_label = STATE_META[state_code]
        merged.update(
            {
                "state_code": state_code,
                "state_label": state_label,
                "risk_level": risk_level,
                "risk_label": risk_label,
            }
        )
    merged["llm_state_code"] = state_code
    merged["llm_confidence"] = llm_result.get("confidence")
    merged["llm_reason"] = llm_result.get("reason")
    merged["llm_assessment_status"] = llm_result.get("assessment_status")
    merged["llm_secondary_flags"] = llm_result.get("secondary_flags") or []
    merged["llm_should_intervene_recommendation"] = llm_result.get("should_intervene_recommendation")
    merged["llm_self_regulation_detected"] = llm_result.get("self_regulation_detected")
    merged["llm_evidence_sentences"] = llm_result.get("evidence_sentences") or []
    return merged
