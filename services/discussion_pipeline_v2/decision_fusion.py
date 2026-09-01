# -*- coding: utf-8 -*-
"""DecisionFusion：融合规则和 LLM 的检测结果。"""
import logging

from config import PIPELINE_V2_ANALYZER_VERSION
from services.state_assessment_service import fuse_state_evidence
from knowledge_base import STATE_META

logger = logging.getLogger(__name__)


class DecisionFusion:
    """融合规则检测和 LLM 检测结果，生成最终的监测结论。"""

    FUSION_VERSION = f"{PIPELINE_V2_ANALYZER_VERSION}_fusion_v1"

    @staticmethod
    def fuse(
        rule_assessment: dict,
        llm_result: dict = None,
        llm_meta: dict = None,
    ) -> dict:
        """融合规则和 LLM 结果。

        参数：
            rule_assessment: RuleDetector.detect() 的返回值
            llm_result: LLMStateDetector.detect() 返回的 result 字段中的内容

        返回包含最终状态、置信度、评估状态等字段的字典。
        """
        if not rule_assessment:
            return {
                "fusion_version": DecisionFusion.FUSION_VERSION,
                "fused_state_code": "unknown",
                "fused_state_label": "未知",
                "risk_level": 0,
                "confidence": 0.0,
                "assessment_status": "insufficient_evidence",
                "decision_source": "no_rule_data",
            }

        state_code = rule_assessment.get("winning_state_code", "unknown")
        state_score = float(rule_assessment.get("winning_score", 0.0))
        label, risk_level, risk_label = STATE_META.get(state_code, STATE_META["unknown"])

        llm_payload = _normalize_llm_payload(llm_result)

        # 使用现成的融合逻辑
        rule_state_wrapper = {
            "state_code": state_code,
            "state_score": state_score,
            "rule_assessment": rule_assessment,
        }
        fusion = fuse_state_evidence(rule_state_wrapper, llm_payload, llm_meta)

        return {
            "fusion_version": DecisionFusion.FUSION_VERSION,
            "fused_state_code": fusion["fused_state_code"],
            "fused_state_label": fusion["fused_state_label"],
            "risk_level": fusion["risk_level"],
            "risk_label": fusion["risk_label"],
            "confidence": fusion["confidence"],
            "assessment_status": fusion["assessment_status"],
            "decision_source": fusion["decision_source"],
            "should_intervene": fusion["should_intervene"],
            "self_regulation_detected": fusion["self_regulation_detected"],
            "self_regulation_sub_type": fusion.get("self_regulation_sub_type"),
            "autonomous_regulation_reason": fusion.get(
                "autonomous_regulation_reason"
            ),
            "llm_failure_blocks_intervention": fusion.get("llm_failure_blocks_intervention", False),
            "llm_validation_status": fusion.get("llm_validation_status"),
            "llm_failure_reason": fusion.get("llm_failure_reason"),
        }


def _normalize_llm_payload(llm_result: dict = None) -> dict:
    if not isinstance(llm_result, dict):
        return {}
    # If detector error/fallback, don't use LLM result, fall back to rule
    if llm_result.get("detector_error"):
        return {}
    state_code = llm_result.get("state_code") or llm_result.get("primary_state")
    if state_code:
        payload = dict(llm_result)
        payload["state_code"] = state_code
        return payload
    return {}
