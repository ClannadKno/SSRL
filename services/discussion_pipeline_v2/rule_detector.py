# -*- coding: utf-8 -*-
"""RuleDetector：规则状态判断。"""
from typing import Optional

from services.rule_state_service import detect_group_state_rule as _detect_rule


class RuleDetector:
    """基于规则的协作状态检测。"""

    @staticmethod
    def detect(context: dict, features: dict) -> dict:
        """运行规则打分，返回规则评估结果。"""
        return _detect_rule(context, features)
