# -*- coding: utf-8 -*-
"""Authoritative state-to-strategy route manifest loader."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Optional


REQUIRED_INTERVENTION = "REQUIRED_INTERVENTION"
OPTIONAL_SUPPORT = "OPTIONAL_SUPPORT"
SUPPRESS = "SUPPRESS"
ROUTE_MODES = (REQUIRED_INTERVENTION, OPTIONAL_SUPPORT, SUPPRESS)
ROUTE_MANIFEST_FILENAME = "strategy_route_manifest.json"


def default_route_manifest_path() -> Path:
    return Path(__file__).resolve().parent / ROUTE_MANIFEST_FILENAME


def load_route_manifest(path: Optional[str | Path] = None) -> dict[str, Any]:
    source = Path(path) if path is not None else default_route_manifest_path()
    source = source.resolve()
    stat = source.stat()
    return _load_route_manifest_cached(str(source), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=8)
def _load_route_manifest_cached(path: str, _mtime_ns: int, _size: int) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    routes = payload.get("routes")
    if not isinstance(routes, dict):
        raise ValueError("invalid_strategy_route_manifest")
    normalized_routes = {}
    for canonical, route in routes.items():
        code = str(canonical or "").strip()
        if not code or not isinstance(route, dict):
            raise ValueError("invalid_strategy_route_manifest_route")
        route_mode = str(route.get("route_mode") or "").strip()
        if route_mode not in ROUTE_MODES:
            raise ValueError(f"invalid_route_mode:{code}")
        primary = _strategy_id_list(route.get("primary_strategy_ids"))
        backup = _strategy_id_list(route.get("backup_strategy_ids"))
        inhibition_id = route.get("inhibition_strategy_id")
        inhibition_id = str(inhibition_id).strip() if inhibition_id else None
        if inhibition_id and inhibition_id not in primary + backup:
            raise ValueError(f"inhibition_strategy_not_routed:{code}")
        normalized_routes[code] = {
            "route_mode": route_mode,
            "intervention_mode": str(route.get("intervention_mode") or "").strip(),
            "primary_strategy_ids": tuple(primary),
            "backup_strategy_ids": tuple(backup),
            "inhibition_strategy_id": inhibition_id,
        }
    return {
        "version": str(payload.get("version") or "strategy_route_manifest.v1"),
        "source_path": str(source),
        "routes": normalized_routes,
    }


def route_manifest_version(path: Optional[str | Path] = None) -> str:
    return load_route_manifest(path)["version"]


def route_for_canonical_state(
    canonical_sub_state: str,
    *,
    path: Optional[str | Path] = None,
) -> dict[str, Any]:
    manifest = load_route_manifest(path)
    code = str(canonical_sub_state or "").strip()
    route = manifest["routes"].get(code)
    if route is None:
        raise KeyError(f"missing_sub_state_route:{code}")
    return {
        "canonical_sub_state": code,
        "route_manifest_version": manifest["version"],
        "route_manifest_path": manifest["source_path"],
        **route,
    }


def all_route_codes(path: Optional[str | Path] = None) -> tuple[str, ...]:
    return tuple(load_route_manifest(path)["routes"].keys())


def _strategy_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("invalid_strategy_id_list")
    result = []
    for item in value:
        strategy_id = str(item or "").strip()
        if strategy_id and strategy_id not in result:
            result.append(strategy_id)
    return result
