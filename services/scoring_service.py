# -*- coding: utf-8 -*-
"""Scoring service for questionnaire responses.

Provides pure-function scoring logic decoupled from Flask/DB contexts,
supporting Likert (forward/reverse), scenario-judgment mapping, and
text/info skip types.  All functions are unit-testable with plain dicts.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

_logger = logging.getLogger(__name__)

# -- Question type constants ----------------------------------------------
QTYPE_LIKERT = "likert"
QTYPE_LIKERT_5 = "likert_5"
QTYPE_LIKERT_7 = "likert_7"
QTYPE_TEXT = "text"
QTYPE_INFO = "info"
QTYPE_SCENARIO_JUDGMENT = "scenario_judgment"

SKIPPED_QUESTION_TYPES = {QTYPE_TEXT, QTYPE_INFO}

# -- Internal helpers -----------------------------------------------------


def _parse_json_field(value: Any) -> Optional[Union[Dict, List]]:
    """Safely deserialise a JSON database field."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _score_likert(item: Dict[str, Any], raw_value: int) -> int:
    """Score a single Likert item (forward or reverse)."""
    min_v = int(item.get("min_value", 1))
    max_v = int(item.get("max_value", 7))

    if not (min_v <= raw_value <= max_v):
        raise ValueError(
            f"Raw value {raw_value} outside valid range [{min_v}, {max_v}] "
            f"for item {item.get('item_code', '?')}"
        )

    if item.get("reverse_scored", False):
        return max_v + min_v - raw_value
    return raw_value


def _score_scenario_judgment(
    item: Dict[str, Any],
    response: Dict[str, Any],
    raw_value: int,
) -> int:
    """Score a scenario-judgment item via score_map_json or options_json."""
    score_map = _parse_json_field(item.get("score_map_json"))
    if score_map is not None:
        option_key = response.get("response_option_key")
        if option_key is not None and option_key in score_map:
            return int(score_map[option_key])
        key = str(raw_value)
        if key in score_map:
            return int(score_map[key])
        raise ValueError(
            f"Option key {option_key!r} / value {raw_value!r} not found "
            f"in score_map for item {item.get('item_code', '?')}"
        )

    options = _parse_json_field(item.get("options_json"))
    if options is not None:
        if isinstance(options, list):
            if 0 <= raw_value < len(options):
                opt = options[raw_value]
                if isinstance(opt, dict) and "score" in opt:
                    return int(opt["score"])
                return int(opt.get("value", raw_value))
            raise ValueError(
                f"Index {raw_value} out of range for {len(options)} options "
                f"in item {item.get('item_code', '?')}"
            )
        if isinstance(options, dict):
            option_key = response.get("response_option_key", str(raw_value))
            entry = options.get(option_key)
            if entry is not None:
                if isinstance(entry, dict) and "score" in entry:
                    return int(entry["score"])
                return int(entry)
            raise ValueError(
                f"Option key {option_key!r} not found in options for "
                f"item {item.get('item_code', '?')}"
            )

    return raw_value


# -- Public API -----------------------------------------------------------


def score_single_response_item(
    item: Dict[str, Any],
    response: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Score one questionnaire-item response.

    Parameters
    ----------
    item:
        Dict from questionnaire_items.
    response:
        Dict from questionnaire_responses, or None when the item has
        no recorded answer.

    Returns
    -------
    A dictionary with scoring info.
    """
    result: Dict[str, Any] = {
        "item_id": item.get("id"),
        "item_code": item.get("item_code", ""),
        "dimension_key": item.get("dimension_key") or "",
        "dimension_label": item.get("dimension_label", ""),
        "question_type": item.get("question_type", QTYPE_LIKERT),
        "reverse_scored": bool(item.get("reverse_scored", False)),
        "include_in_score": bool(item.get("include_in_score", True)),
        "raw_value": None,
        "scored_value": None,
        "missing": False,
        "skipped": False,
        "skip_reason": None,
        "error": None,
        "response_text": None,
    }

    qtype = item.get("question_type", QTYPE_LIKERT)

    # -- Text / info items are never scored --
    if qtype in SKIPPED_QUESTION_TYPES:
        result["skipped"] = True
        result["skip_reason"] = f"Question type '{qtype}' is not scored"
        result["include_in_score"] = False
        if response is not None:
            result["raw_value"] = response.get("response_value")
            result["response_text"] = response.get("response_text")
        return result

    # -- Missing response --
    if response is None:
        result["missing"] = True
        result["error"] = "No response found"
        return result

    raw_value = response.get("response_value")
    result["raw_value"] = raw_value

    if raw_value is None:
        result["missing"] = True
        result["error"] = "Response has no value (raw_value is None)"
        return result

    # -- Score according to question type --
    try:
        if qtype in (QTYPE_SCENARIO_JUDGMENT, "scenario"):
            scored = _score_scenario_judgment(item, response, raw_value)
        elif qtype in (QTYPE_LIKERT, QTYPE_LIKERT_5, QTYPE_LIKERT_7):
            scored = _score_likert(item, raw_value)
        else:
            scored = raw_value  # fallback: passthrough
        result["scored_value"] = scored
    except (ValueError, TypeError) as exc:
        result["error"] = str(exc)
        result["scored_value"] = None

    return result


def score_questionnaire_responses(
    questionnaire: Dict[str, Any],
    items: List[Dict[str, Any]],
    responses: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Score a full set of questionnaire responses."""
    response_by_item: Dict[int, Dict[str, Any]] = {}
    for r in (responses or []):
        iid = r.get("item_id")
        if iid is not None:
            response_by_item[iid] = r

    item_scores = []
    missing_items = []
    for item in (items or []):
        iid = item.get("id")
        resp = response_by_item.get(iid)
        scored = score_single_response_item(item, resp)
        item_scores.append(scored)
        if scored.get("missing"):
            missing_items.append({
                "item_id": iid,
                "item_code": item.get("item_code", ""),
            })

    dimension_scores = compute_dimension_scores(item_scores)
    total_score = compute_total_score(item_scores)

    return {
        "questionnaire_id": questionnaire.get("id"),
        "questionnaire_code": questionnaire.get("code", ""),
        "questionnaire_title": questionnaire.get("title", ""),
        "stage": responses[0].get("response_stage") if responses else None,
        "user_id": responses[0].get("user_id") if responses else None,
        "session_id": responses[0].get("session_id") if responses else None,
        "item_scores": item_scores,
        "dimension_scores": dimension_scores,
        "total_score": total_score,
        "missing_items": missing_items,
        "item_count": len(items) if items else 0,
        "scored_item_count": sum(
            1 for s in item_scores if s.get("scored_value") is not None
        ),
    }


def compute_dimension_scores(
    item_scores: List[Dict[str, Any]],
) -> Dict[str, Dict[str, float]]:
    """Compute per-dimension mean / sum / count from scored items."""
    groups: Dict[str, List[float]] = {}
    for s in (item_scores or []):
        if not s.get("include_in_score", True):
            continue
        sv = s.get("scored_value")
        if sv is None:
            continue
        dim = s.get("dimension_key") or s.get("dimension_label") or "_ungrouped"
        groups.setdefault(dim, []).append(sv)

    result: Dict[str, Dict[str, float]] = {}
    for dim_key in sorted(groups):
        vals = groups[dim_key]
        if not vals:
            continue
        cnt = len(vals)
        total = sum(vals)
        result[dim_key] = {
            "mean": round(total / cnt, 4),
            "sum": total,
            "count": cnt,
        }
    return result


def compute_total_score(
    item_scores: List[Dict[str, Any]],
) -> Dict[str, Union[float, int]]:
    """Compute overall mean / sum / count from scored items."""
    values = [
        s["scored_value"]
        for s in (item_scores or [])
        if s.get("include_in_score", True) and s.get("scored_value") is not None
    ]
    cnt = len(values)
    total = sum(values) if values else 0
    return {
        "mean": round(total / cnt, 4) if cnt > 0 else 0.0,
        "sum": total,
        "count": cnt,
    }


def load_and_score_user_questionnaire(
    questionnaire_id: int,
    user_id: int,
    stage: str,
    session_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Load questionnaire data from the database and return scored results."""
    from db import query_all, query_one

    q_data = query_one(
        "SELECT * FROM questionnaires WHERE id=?", (questionnaire_id,)
    )
    if not q_data:
        return {"error": f"Questionnaire {questionnaire_id} not found"}
    q_data = dict(q_data)

    items = [
        dict(r)
        for r in query_all(
            "SELECT * FROM questionnaire_items WHERE questionnaire_id=? "
            "ORDER BY sort_order ASC, id ASC",
            (questionnaire_id,),
        )
    ]
    if not items:
        return {"error": f"No items for questionnaire {questionnaire_id}"}

    if session_id:
        responses = [
            dict(r)
            for r in query_all(
                "SELECT * FROM questionnaire_responses "
                "WHERE questionnaire_id=? AND user_id=? AND response_stage=? "
                "AND session_id=? ORDER BY item_id ASC",
                (questionnaire_id, user_id, stage, session_id),
            )
        ]
    else:
        responses = [
            dict(r)
            for r in query_all(
                "SELECT * FROM questionnaire_responses "
                "WHERE questionnaire_id=? AND user_id=? AND response_stage=? "
                "ORDER BY item_id ASC",
                (questionnaire_id, user_id, stage),
            )
        ]

    user = query_one(
        "SELECT id, participant_code, real_name FROM users WHERE id=?", (user_id,),
    )

    result = score_questionnaire_responses(q_data, items, responses)
    if user:
        result["user_id"] = user["id"]
        u = dict(user)
        result["participant_code"] = u.get("participant_code", "") or ""
        result["user_name"] = u.get("real_name", "") or ""
    return result
