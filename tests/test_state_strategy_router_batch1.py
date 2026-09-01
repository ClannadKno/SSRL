# -*- coding: utf-8 -*-
"""Batch 1 coverage for deterministic state strategy routing."""

from __future__ import annotations

from services.state_strategy_router import StateStrategyRouter, normalize_sub_category
from services.three_stage_route_manifest import REQUIRED_INTERVENTION, SUPPRESS
from services.three_stage_strategy_library import load_strategy_library


def test_router_routes_burnout_to_er008_stage3_pool():
    route = StateStrategyRouter().route("burnout")

    assert route.sub_category == "burnout"
    assert route.should_intervene is True
    assert route.terminal_decision == "STAGE3"
    assert route.route_mode == REQUIRED_INTERVENTION
    assert route.strategy_pool == ("ER-008",)
    assert route.to_dict()["strategy_pool"] == ["ER-008"]


def test_router_routes_constructive_conflict_to_oi_terminal():
    route = StateStrategyRouter().route("constructive_conflict")

    assert route.sub_category == "constructive_conflict"
    assert route.should_intervene is False
    assert route.terminal_decision == "OI"
    assert route.route_mode == SUPPRESS
    assert route.strategy_pool == ("OI-001",)
    assert route.inhibition_strategy_id == "OI-001"


def test_router_supports_plan_chinese_sub_category_names():
    router = StateStrategyRouter()

    for value in ("困惑型", "挫败型", "倦怠型", "人际性冲突", "跑题脱离(无自调节)", "敷衍脱离"):
        route = router.route(value)
        assert route.should_intervene is True
        assert route.terminal_decision == "STAGE3"
        assert route.strategy_pool

    for value in ("建设性冲突", "深度思考", "执行推进", "跑题脱离(有自调节)"):
        route = router.route(value)
        assert route.should_intervene is False
        assert route.terminal_decision == "OI"
        assert route.inhibition_strategy_id


def test_router_never_exposes_full_strategy_library_as_pool():
    router = StateStrategyRouter()
    library_count = len(load_strategy_library().definitions)

    for value in ("frustration", "burnout", "constructive_conflict"):
        route = router.route(value)
        assert 0 < len(route.strategy_pool) < library_count


def test_normalize_sub_category_accepts_width_variants():
    assert normalize_sub_category("跑题脱离（有自调节）") == "off_topic_self_regulated"
    assert normalize_sub_category("跑题脱离(无自调节)") == "off_topic_unregulated"
