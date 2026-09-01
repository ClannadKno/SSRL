# -*- coding: utf-8 -*-
"""FeatureService：提取增量特征。"""
from typing import Optional

from services.feature_service import extract_group_features as _extract_features


class FeatureService:
    """提取行为与文本增量特征。"""

    @staticmethod
    def extract(context: dict) -> dict:
        """基于已收集的上下文提取特征。"""
        return _extract_features(context)
