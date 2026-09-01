# -*- coding: utf-8 -*-
"""Deterministic state-to-strategy routing for the three-stage pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from services.three_stage_route_manifest import (
    OPTIONAL_SUPPORT,
    REQUIRED_INTERVENTION,
    SUPPRESS,
)
from services.three_stage_schema import (
    CANONICAL_SUB_STATE_CODES,
    OI_STRATEGY_IDS,
    normalize_canonical_sub_state,
    route_for_state_with_overlays,
)


_SUB_CATEGORY_ALIASES = {
    "标准型": "standard",
    "常规协作": "standard",
    "深度思考": "deep_thinking",
    "执行推进": "execution_progress",
    "建设性冲突": "constructive_conflict",
    "人际性冲突": "interpersonal_conflict",
    "人际冲突": "interpersonal_conflict",
    "困惑型": "confusion",
    "困惑": "confusion",
    "挫败型": "frustration",
    "挫败": "frustration",
    "倦怠型": "burnout",
    "倦怠": "burnout",
    "跑题脱离(有自调节)": "off_topic_self_regulated",
    "跑题脱离（有自调节）": "off_topic_self_regulated",
    "跑题脱离(无自调节)": "off_topic_unregulated",
    "跑题脱离（无自调节）": "off_topic_unregulated",
    "敷衍脱离": "perfunctory_detachment",
    "个体边缘化": "individual_marginalization",
    "心理安全风险": "psychological_safety_risk",
    "心理安全": "psychological_safety_risk",
    "高强度过载": "high_intensity_overload",
    "信息过载": "high_intensity_overload",
    "阶段达成": "stage_achievement",
    "阶段完成": "stage_achievement",
}


@dataclass(frozen=True)
class StateStrategyRoute:
    sub_category: str
    canonical_state: str
    should_intervene: bool
    strategy_pool: tuple[str, ...]
    route_mode: str
    intervention_mode: Optional[str]
    primary_strategy_ids: tuple[str, ...]
    backup_strategy_ids: tuple[str, ...]
    inhibition_strategy_id: Optional[str]
    route_overlay_tag: Optional[str]
    route_manifest_version: Optional[str]
    strategy_library_version: Optional[str]
    strategy_library_hash: Optional[str]

    @property
    def terminal_decision(self) -> str:
        if self.should_intervene:
            return "STAGE3"
        if (
            self.route_mode == SUPPRESS
            and self.inhibition_strategy_id in OI_STRATEGY_IDS
        ):
            return "OI"
        if self.route_mode == OPTIONAL_SUPPORT:
            return "OPTIONAL_SUPPORT"
        return "SUPPRESS"

    @property
    def strategy_source(self) -> str:
        parts = [
            self.route_manifest_version or "route_manifest",
            self.strategy_library_version or "strategy_library",
        ]
        if self.strategy_library_hash:
            parts.append(self.strategy_library_hash[:12])
        return "|".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_category": self.sub_category,
            "canonical_state": self.canonical_state,
            "should_intervene": self.should_intervene,
            "strategy_pool": list(self.strategy_pool),
            "route_mode": self.route_mode,
            "terminal_decision": self.terminal_decision,
            "intervention_mode": self.intervention_mode,
            "primary_strategy_ids": list(self.primary_strategy_ids),
            "backup_strategy_ids": list(self.backup_strategy_ids),
            "inhibition_strategy_id": self.inhibition_strategy_id,
            "route_overlay_tag": self.route_overlay_tag,
            "route_manifest_version": self.route_manifest_version,
            "strategy_library_version": self.strategy_library_version,
            "strategy_library_hash": self.strategy_library_hash,
            "strategy_source": self.strategy_source,
        }

    def to_legacy_route_payload(self) -> dict[str, Any]:
        return {
            "canonical_sub_state": self.canonical_state,
            "candidate_strategy_ids": list(self.strategy_pool),
            "primary_strategy_ids": list(self.primary_strategy_ids),
            "backup_strategy_ids": list(self.backup_strategy_ids),
            "route_mode": self.route_mode,
            "intervention_mode": self.intervention_mode,
            "should_intervene": self.should_intervene,
            "is_optional_support": self.route_mode == OPTIONAL_SUPPORT,
            "inhibition_strategy_id": self.inhibition_strategy_id,
            "route_overlay_tag": self.route_overlay_tag,
            "route_manifest_version": self.route_manifest_version,
            "strategy_library_version": self.strategy_library_version,
            "strategy_library_hash": self.strategy_library_hash,
        }


class StateStrategyRouter:
    """Resolve a Stage 2 sub-category into the allowed strategy pool."""

    def route(
        self,
        sub_category: Optional[str],
        *,
        secondary_tags: Any = None,
    ) -> StateStrategyRoute:
        canonical = normalize_sub_category(sub_category)
        route = route_for_state_with_overlays(canonical, secondary_tags)
        route_mode = str(route.get("route_mode") or "").strip()
        strategy_pool = tuple(route.get("candidate_strategy_ids") or ())
        return StateStrategyRoute(
            sub_category=canonical,
            canonical_state=str(route.get("canonical_sub_state") or canonical),
            should_intervene=route_mode == REQUIRED_INTERVENTION,
            strategy_pool=strategy_pool,
            route_mode=route_mode,
            intervention_mode=route.get("intervention_mode"),
            primary_strategy_ids=tuple(route.get("primary_strategy_ids") or ()),
            backup_strategy_ids=tuple(route.get("backup_strategy_ids") or ()),
            inhibition_strategy_id=route.get("inhibition_strategy_id"),
            route_overlay_tag=route.get("route_overlay_tag"),
            route_manifest_version=route.get("route_manifest_version"),
            strategy_library_version=route.get("strategy_library_version"),
            strategy_library_hash=route.get("strategy_library_hash"),
        )


def normalize_sub_category(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if raw in CANONICAL_SUB_STATE_CODES:
        return raw
    normalized_width = raw.replace("（", "(").replace("）", ")")
    alias = (
        _SUB_CATEGORY_ALIASES.get(raw)
        or _SUB_CATEGORY_ALIASES.get(normalized_width)
        or _SUB_CATEGORY_ALIASES.get(normalized_width.replace(" ", ""))
    )
    return normalize_canonical_sub_state(alias or raw)


__all__ = [
    "StateStrategyRoute",
    "StateStrategyRouter",
    "normalize_sub_category",
]
