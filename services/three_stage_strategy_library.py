# -*- coding: utf-8 -*-
"""Markdown-backed strategy library for the three-stage pipeline.

Batch 4 makes the Markdown strategy document the authority for strategy IDs,
names, templates, version, and hashes.  The precise sub-state route table is
loaded from ``strategy_route_manifest.json`` so runtime and tests share the
same deterministic mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Optional

from db import db, now_str
from services.three_stage_schema import (
    CANONICAL_SUB_STATE_CODES,
    OI_STRATEGY_IDS,
    OVERLAY_COMPATIBLE_PRIMARY_STATES,
    STATE_OVERLAY_CODES,
    dumps_json,
    normalize_canonical_sub_state,
)
from services.three_stage_route_manifest import (
    OPTIONAL_SUPPORT,
    REQUIRED_INTERVENTION,
    SUPPRESS,
    all_route_codes,
    route_for_canonical_state,
)


STRATEGY_LIBRARY_FILENAME = "SSRL策略库v3.md"

_SUB_CATEGORY_ALIASES = (
    ("跑题脱离(有自调节)", "off_topic_self_regulated"),
    ("跑题脱离（有自调节）", "off_topic_self_regulated"),
    ("跑题脱离(无自调节)", "off_topic_unregulated"),
    ("跑题脱离（无自调节）", "off_topic_unregulated"),
    ("标准型(个体边缘化场景)", "individual_marginalization"),
    ("标准型（个体边缘化场景）", "individual_marginalization"),
    ("标准型(阶段完成)", "stage_achievement"),
    ("标准型（阶段完成）", "stage_achievement"),
    ("个体边缘化", "individual_marginalization"),
    ("心理安全", "psychological_safety_risk"),
    ("高强度", "high_intensity_overload"),
    ("信息过载", "high_intensity_overload"),
    ("建设性冲突", "constructive_conflict"),
    ("人际性冲突", "interpersonal_conflict"),
    ("深度思考", "deep_thinking"),
    ("执行推进", "execution_progress"),
    ("困惑型", "confusion"),
    ("挫败型", "frustration"),
    ("倦怠型", "burnout"),
    ("跑题脱离", "off_topic_unregulated"),
    ("敷衍脱离", "perfunctory_detachment"),
    ("标准型", "standard"),
)

_TYPE_PRIORITY = {
    "观察抑制": 10,
    "情绪调节": 30,
    "情绪调节(认知重评)": 30,
    "情绪调节(反应聚焦)": 35,
    "情绪觉察": 40,
    "情绪表达": 50,
    "社会支持": 60,
    "社会支持(情感支持)": 60,
    "社会支持(工具支持)": 65,
}

_ROUTE_PRIORITY = {
    "OI-001": 5,
    "OI-002": 5,
    "OI-003": 5,
    "OI-004": 5,
    "ER-008": 15,
    "ER-001": 20,
    "SS-004": 25,
    "EA-001": 30,
    "ER-002": 30,
    "ER-005": 35,
    "SS-001": 80,
}


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    strategy_name: str
    strategy_type: str
    applicable_sub_states: tuple[str, ...]
    trigger_features: tuple[str, ...]
    template_examples: tuple[str, ...]
    cognitive_load: str
    expected_effect: str
    inappropriate_conditions: tuple[str, ...]
    should_intervene: bool
    is_exclusive: bool
    priority: int
    version: str
    content_hash: str

    def to_public_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "strategy_type": self.strategy_type,
            "applicable_sub_states": list(self.applicable_sub_states),
            "trigger_features": list(self.trigger_features),
            "template_examples": list(self.template_examples),
            "cognitive_load": self.cognitive_load,
            "expected_effect": self.expected_effect,
            "inappropriate_conditions": list(self.inappropriate_conditions),
            "should_intervene": self.should_intervene,
            "is_exclusive": self.is_exclusive,
            "priority": self.priority,
            "version": self.version,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class StrategyLibrary:
    version: str
    library_hash: str
    source_path: str
    definitions: tuple[StrategyDefinition, ...]

    @property
    def by_id(self) -> dict[str, StrategyDefinition]:
        return {item.strategy_id: item for item in self.definitions}


def default_strategy_library_path() -> Path:
    return Path(__file__).resolve().parents[1] / "docs" / STRATEGY_LIBRARY_FILENAME


def load_strategy_library(path: Optional[str | Path] = None) -> StrategyLibrary:
    source = Path(path) if path is not None else default_strategy_library_path()
    source = source.resolve()
    stat = source.stat()
    return _load_strategy_library_cached(str(source), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=8)
def _load_strategy_library_cached(path: str, _mtime_ns: int, _size: int) -> StrategyLibrary:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    version = _extract_version(text)
    library_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    definitions = tuple(_parse_strategy_blocks(text, version))
    return StrategyLibrary(
        version=version,
        library_hash=library_hash,
        source_path=str(source),
        definitions=definitions,
    )


def get_strategy_library_metadata(path: Optional[str | Path] = None) -> dict:
    library = load_strategy_library(path)
    return {
        "version": library.version,
        "library_hash": library.library_hash,
        "source_path": library.source_path,
        "strategy_count": len(library.definitions),
    }


def get_strategy_definition(strategy_id: str, path: Optional[str | Path] = None) -> Optional[StrategyDefinition]:
    return load_strategy_library(path).by_id.get(str(strategy_id or "").strip())


def get_stage2_route_mapping(path: Optional[str | Path] = None) -> dict[str, dict]:
    return {code: route_for_sub_state(code, path=path) for code in CANONICAL_SUB_STATE_CODES}


def route_for_sub_state(value: Optional[str], path: Optional[str | Path] = None) -> dict[str, Any]:
    canonical = normalize_canonical_sub_state(value)
    route = route_for_canonical_state(canonical)
    primary = list(route["primary_strategy_ids"])
    backup = list(route["backup_strategy_ids"])
    candidate_ids = _dedupe(primary + backup)
    library = load_strategy_library(path)
    missing_ids = [strategy_id for strategy_id in candidate_ids if strategy_id not in library.by_id]
    if missing_ids:
        raise ValueError("strategy_route_missing_definition:" + ",".join(missing_ids))
    inhibition_id = route.get("inhibition_strategy_id")
    if inhibition_id and inhibition_id not in OI_STRATEGY_IDS:
        raise ValueError("invalid_inhibition_strategy_id:" + inhibition_id)
    route_mode = route.get("route_mode") or (
        REQUIRED_INTERVENTION if route.get("should_intervene") else SUPPRESS
    )
    return {
        "canonical_sub_state": canonical,
        "primary_strategy_ids": primary,
        "backup_strategy_ids": backup,
        "candidate_strategy_ids": candidate_ids,
        "route_mode": route_mode,
        "intervention_mode": route.get("intervention_mode"),
        "should_intervene": route_mode == REQUIRED_INTERVENTION,
        "is_optional_support": route_mode == OPTIONAL_SUPPORT,
        "inhibition_strategy_id": inhibition_id,
        "route_manifest_version": route.get("route_manifest_version"),
        "strategy_library_version": library.version,
        "strategy_library_hash": library.library_hash,
    }


def validate_candidate_strategy_ids(
    canonical_sub_state: str,
    strategy_ids: list[str],
    *,
    path: Optional[str | Path] = None,
) -> list[str]:
    route = route_for_sub_state(canonical_sub_state, path=path)
    allowed = set(route["candidate_strategy_ids"])
    normalized = _dedupe(str(item or "").strip() for item in strategy_ids or [])
    invalid = [item for item in normalized if item not in allowed]
    if invalid:
        raise ValueError("invalid_candidate_strategy_id")
    if route["inhibition_strategy_id"] and normalized != [route["inhibition_strategy_id"]]:
        raise ValueError("oi_candidate_strategy_mismatch")
    if route["should_intervene"] and not normalized:
        raise ValueError("missing_candidate_strategy_ids")
    return normalized


def is_strategy_applicable(
    strategy_id: str,
    canonical_sub_state: str,
    *,
    require_intervention: Optional[bool] = None,
    path: Optional[str | Path] = None,
) -> bool:
    strategy = get_strategy_definition(strategy_id, path=path)
    if not strategy:
        return False
    canonical = normalize_canonical_sub_state(canonical_sub_state)
    if canonical not in strategy.applicable_sub_states:
        return False
    if require_intervention is not None and strategy.should_intervene != bool(require_intervention):
        return False
    return True


def rank_candidate_strategies(
    canonical_sub_state: str,
    *,
    usage_counts: Optional[dict[str, int]] = None,
    last_strategy_ids: Optional[list[str]] = None,
    path: Optional[str | Path] = None,
) -> list[dict]:
    route = route_for_sub_state(canonical_sub_state, path=path)
    usage_counts = usage_counts or {}
    last_strategy_ids = list(last_strategy_ids or [])
    primary = set(route["primary_strategy_ids"])
    backup = set(route["backup_strategy_ids"])
    ranked = []
    for position, strategy_id in enumerate(route["candidate_strategy_ids"]):
        strategy = get_strategy_definition(strategy_id, path=path)
        base = 100.0
        if strategy_id in primary:
            base += 25.0
        elif strategy_id in backup:
            base += 10.0
        base -= position
        base -= min(40.0, float(usage_counts.get(strategy_id, 0) or 0) * 12.0)
        if strategy_id in last_strategy_ids[-2:]:
            base -= 30.0
        ranked.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": strategy.strategy_name if strategy else None,
                "route_role": "primary" if strategy_id in primary else "backup",
                "score": round(base, 2),
                "usage_count": int(usage_counts.get(strategy_id, 0) or 0),
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["strategy_id"]))
    return ranked


def validate_strategy_library(path: Optional[str | Path] = None) -> list[dict]:
    library = load_strategy_library(path)
    issues = []
    by_id = library.by_id
    if len(by_id) != len(library.definitions):
        issues.append({"type": "duplicate_strategy_id"})
    routed_codes = set(all_route_codes())
    for code in CANONICAL_SUB_STATE_CODES:
        if code not in routed_codes:
            issues.append({"type": "missing_sub_state_route", "canonical_sub_state": code})
            continue
        route = route_for_sub_state(code, path=path)
        for strategy_id in route["candidate_strategy_ids"]:
            strategy = by_id.get(strategy_id)
            if not strategy:
                issues.append(
                    {
                        "type": "route_strategy_missing_definition",
                        "canonical_sub_state": code,
                        "strategy_id": strategy_id,
                    }
                )
                continue
            if code not in strategy.applicable_sub_states:
                issues.append(
                    {
                        "type": "route_strategy_not_applicable",
                        "canonical_sub_state": code,
                        "strategy_id": strategy_id,
                    }
                )
        if route["inhibition_strategy_id"]:
            if route["should_intervene"]:
                issues.append({"type": "oi_route_should_not_intervene", "canonical_sub_state": code})
            if route["backup_strategy_ids"]:
                issues.append({"type": "oi_route_has_backup", "canonical_sub_state": code})
    for strategy in library.definitions:
        if strategy.strategy_id in OI_STRATEGY_IDS:
            if strategy.should_intervene or not strategy.is_exclusive:
                issues.append({"type": "invalid_oi_definition", "strategy_id": strategy.strategy_id})
        if not strategy.strategy_name:
            issues.append({"type": "missing_strategy_name", "strategy_id": strategy.strategy_id})
        if not strategy.applicable_sub_states:
            issues.append({"type": "missing_applicable_sub_states", "strategy_id": strategy.strategy_id})
    return issues


def sync_strategy_definitions(conn=None, *, path: Optional[str | Path] = None) -> dict:
    library = load_strategy_library(path)
    timestamp = now_str()
    own_conn = conn is None
    if own_conn:
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
    try:
        for item in library.definitions:
            conn.execute(
                """
                INSERT INTO strategy_definitions(
                    strategy_id, strategy_name, strategy_type,
                    applicable_sub_states_json, trigger_features_json,
                    template_examples_json, cognitive_load, expected_effect,
                    inappropriate_conditions_json, should_intervene, is_exclusive,
                    priority, version, content_hash, is_active, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_id, version) DO UPDATE SET
                    strategy_name=excluded.strategy_name,
                    strategy_type=excluded.strategy_type,
                    applicable_sub_states_json=excluded.applicable_sub_states_json,
                    trigger_features_json=excluded.trigger_features_json,
                    template_examples_json=excluded.template_examples_json,
                    cognitive_load=excluded.cognitive_load,
                    expected_effect=excluded.expected_effect,
                    inappropriate_conditions_json=excluded.inappropriate_conditions_json,
                    should_intervene=excluded.should_intervene,
                    is_exclusive=excluded.is_exclusive,
                    priority=excluded.priority,
                    content_hash=excluded.content_hash,
                    is_active=1,
                    updated_at=excluded.updated_at
                """,
                (
                    item.strategy_id,
                    item.strategy_name,
                    item.strategy_type,
                    dumps_json(list(item.applicable_sub_states)),
                    dumps_json(list(item.trigger_features)),
                    dumps_json(list(item.template_examples)),
                    item.cognitive_load,
                    item.expected_effect,
                    dumps_json(list(item.inappropriate_conditions)),
                    1 if item.should_intervene else 0,
                    1 if item.is_exclusive else 0,
                    item.priority,
                    item.version,
                    item.content_hash,
                    1,
                    timestamp,
                    timestamp,
                ),
            )
        if own_conn:
            conn.commit()
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()
    return {
        "version": library.version,
        "library_hash": library.library_hash,
        "strategy_count": len(library.definitions),
        "source_path": library.source_path,
    }


def _parse_strategy_blocks(text: str, version: str) -> list[StrategyDefinition]:
    blocks = re.split(r"(?=^###\s*策略ID\s*[:：])", text, flags=re.MULTILINE)
    definitions = []
    route_applicability = _route_applicability_by_strategy()
    for block in blocks:
        id_match = re.search(r"^###\s*策略ID\s*[:：]\s*([A-Z]{2}-\d{3})\s*$", block, re.MULTILINE)
        if not id_match:
            continue
        strategy_id = id_match.group(1).strip()
        name = _field_inline(block, "策略名称")
        strategy_type = _field_inline(block, "类型")
        applicable_text = _field_text(block, "适用子类别")
        trigger_features = tuple(_field_list(block, "触发特征"))
        template_examples = tuple(_field_list(block, "话术模板"))
        cognitive_load = _field_inline(block, "认知负荷")
        expected_effect = "\n".join(_field_list(block, "预期效果"))
        inappropriate = tuple(_field_list(block, "不宜使用条件"))
        applicable = set(route_applicability.get(strategy_id, set()))
        applicable.update(_canonical_from_chinese_text(applicable_text))
        applicable.update(_canonical_from_chinese_text("\n".join(trigger_features)))
        content_hash = hashlib.sha256(_normalize_block(block).encode("utf-8")).hexdigest()
        should_intervene = strategy_id not in OI_STRATEGY_IDS
        is_exclusive = strategy_id in OI_STRATEGY_IDS
        definitions.append(
            StrategyDefinition(
                strategy_id=strategy_id,
                strategy_name=name,
                strategy_type=strategy_type,
                applicable_sub_states=tuple(
                    code for code in CANONICAL_SUB_STATE_CODES if code in applicable
                ),
                trigger_features=trigger_features,
                template_examples=template_examples,
                cognitive_load=cognitive_load,
                expected_effect=expected_effect,
                inappropriate_conditions=inappropriate,
                should_intervene=should_intervene,
                is_exclusive=is_exclusive,
                priority=_priority_for_strategy(strategy_id, strategy_type),
                version=version,
                content_hash=content_hash,
            )
        )
    return definitions


def _route_applicability_by_strategy() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for canonical in all_route_codes():
        route = route_for_canonical_state(canonical)
        for strategy_id in route["primary_strategy_ids"] + route["backup_strategy_ids"]:
            result.setdefault(strategy_id, set()).add(canonical)
            if canonical in STATE_OVERLAY_CODES:
                result[strategy_id].update(
                    OVERLAY_COMPATIBLE_PRIMARY_STATES.get(canonical, ())
                )
    return result


def _canonical_from_chinese_text(text: str) -> set[str]:
    value = str(text or "")
    result = set()
    for marker, canonical in _SUB_CATEGORY_ALIASES:
        if marker in value:
            result.add(canonical)
    return result


def _extract_version(text: str) -> str:
    current = re.search(r"\*\*当前版本\*\*\s*[:：]\s*([^\s(（]+)", text)
    if current:
        return current.group(1).strip()
    title = re.search(r"策略库\s*(v\d+(?:\.\d+)*)", text, re.IGNORECASE)
    if title:
        return title.group(1).strip()
    raise ValueError("strategy_library_version_not_found")


def _field_inline(block: str, label: str) -> str:
    values = _field_list(block, label)
    return values[0] if values else ""


def _field_text(block: str, label: str) -> str:
    pattern = (
        r"\*\*" + re.escape(label) + r"\*\*\s*[:：]\s*"
        r"(.*?)(?=\n\*\*[^*\n]+?\*\*\s*[:：]|\n###\s*策略ID|\n===STRATEGY_BREAK===|\Z)"
    )
    match = re.search(pattern, block, flags=re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def _field_list(block: str, label: str) -> list[str]:
    text = _field_text(block, label)
    if not text:
        return []
    result = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        line = line.strip().strip('"').strip()
        if line and line not in result:
            result.append(line)
    return result


def _normalize_block(block: str) -> str:
    return "\n".join(line.rstrip() for line in block.strip().splitlines())


def _priority_for_strategy(strategy_id: str, strategy_type: str) -> int:
    if strategy_id in _ROUTE_PRIORITY:
        return _ROUTE_PRIORITY[strategy_id]
    for prefix, priority in _TYPE_PRIORITY.items():
        if str(strategy_type or "").startswith(prefix):
            return priority
    return 100


def _dedupe(values) -> list[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "STRATEGY_LIBRARY_FILENAME",
    "StrategyDefinition",
    "StrategyLibrary",
    "default_strategy_library_path",
    "get_stage2_route_mapping",
    "get_strategy_definition",
    "get_strategy_library_metadata",
    "is_strategy_applicable",
    "load_strategy_library",
    "rank_candidate_strategies",
    "route_for_sub_state",
    "sync_strategy_definitions",
    "validate_candidate_strategy_ids",
    "validate_strategy_library",
]
