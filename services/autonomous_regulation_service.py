# -*- coding: utf-8 -*-
"""Autonomous regulation event persistence for V2 monitoring pipeline.

Persists self-regulation events detected by DecisionFusion into the
autonomous_regulation_events table.

Designed to be called from MonitoringService.run_detection() after fusion.
All writes are wrapped so that failures never block the main detection flow.
"""
import json
import logging

from db import execute, now_str, query_one

logger = logging.getLogger(__name__)

_EVENT_TYPE_SELF_REGULATION = "self_regulation_detected"


def persist_autonomous_regulation_event(
    group_id: int,
    fusion: dict,
    *,
    session_id=None,
    task_id=None,
    session_no=None,
    monitor_run_id=None,
    context=None,
    fusion_summary: dict = None,
) -> dict:
    """Persist a self-regulation event when the fusion result indicates one.

    Args:
        group_id: The group identifier.
        fusion: The full DecisionFusion.fuse() result dict.
        session_id: Optional session identifier.
        task_id: Optional task identifier.
        session_no: Optional session number.
        monitor_run_id: Optional monitor_run identifier (used for dedup).
        context: Optional detection context dict.
        fusion_summary: Optional pre-built summary dict. If None, derived from
                        fusion.

    Returns:
        dict with keys:
          - event_id (int or None): the new row id, or None if nothing written
          - persisted (bool): whether a row was actually inserted
          - skipped_reason (str or None): why it was skipped, if applicable

    The function is idempotent per monitor_run_id: if an event already exists
    with the same source_monitor_run_id, no duplicate is inserted.
    """
    if not isinstance(fusion, dict):
        logger.warning(
            "persist_autonomous_regulation_event called with non-dict fusion "
            "for group %s: %s", group_id, type(fusion).__name__,
        )
        return {"event_id": None, "persisted": False, "skipped_reason": "invalid_fusion"}

    # Only persist when the fusion explicitly marks self-regulation
    self_reg = fusion.get("self_regulation_detected")
    if not self_reg:
        return {"event_id": None, "persisted": False, "skipped_reason": "not_detected"}

    # Build event_type ¨C prefer finer-grained mapping when available
    event_type = _resolve_event_type(fusion)

    # Build evidence / metadata JSON
    meta = fusion_summary or _build_fusion_summary(fusion)
    metadata_json = json.dumps(meta, ensure_ascii=False)

    # Idempotency check: source_monitor_run_id duplication guard
    if monitor_run_id is not None:
        existing = query_one(
            "SELECT id FROM autonomous_regulation_events "
            "WHERE source_monitor_run_id=? AND event_type=?",
            (monitor_run_id, event_type),
        )
        if existing:
            logger.info(
                "Autonomous regulation event already exists for "
                "monitor_run %s (event_type=%s, existing_id=%s), skipping",
                monitor_run_id, event_type, existing["id"],
            )
            return {
                "event_id": existing["id"],
                "persisted": False,
                "skipped_reason": "already_exists",
            }

    # Determine detected_by ¨C primarily decision_fusion,
    # but record llm/rule source when available
    detected_by = "decision_fusion"
    decision_source = fusion.get("decision_source", "")
    if decision_source:
        if "llm" in decision_source:
            detected_by = "decision_fusion_llm"
        elif "rule" in decision_source:
            detected_by = "decision_fusion_rule"

    created_at = now_str()

    try:
        event_id = execute(
            """INSERT INTO autonomous_regulation_events
               (group_id, session_id, task_id, event_type,
                confidence, detected_by, note, metadata_json,
                source_monitor_run_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                group_id,
                session_id,
                task_id,
                event_type,
                fusion.get("confidence", 0.0),
                detected_by,
                meta.get("summary_note") or "",
                metadata_json,
                monitor_run_id,
                created_at,
            ),
        )
    except Exception as exc:
        logger.error(
            "Failed to persist autonomous regulation event for "
            "group %s (monitor_run=%s): %s",
            group_id, monitor_run_id, exc,
        )
        return {"event_id": None, "persisted": False, "skipped_reason": str(exc)}

    logger.info(
        "Persisted autonomous regulation event id=%s for group %s "
        "(monitor_run=%s, event_type=%s)",
        event_id, group_id, monitor_run_id, event_type,
    )

    return {"event_id": event_id, "persisted": True, "skipped_reason": None}


def _resolve_event_type(fusion: dict) -> str:
    """Map fusion result to a stable event_type string.

    If finer-grained sub-types are available from fusion metadata,
    return them; otherwise fall back to the generic stable string.
    """
    # DecisionFusion.fuse() currently does not expose sub-types like
    # planning / monitoring / reflection / help_seeking.
    # If those become available in future versions, map them here.
    sub_type = fusion.get("self_regulation_sub_type")
    if sub_type and sub_type in {"planning", "monitoring", "reflection", "help_seeking"}:
        return sub_type

    return _EVENT_TYPE_SELF_REGULATION


def _build_fusion_summary(fusion: dict) -> dict:
    """Build a concise JSON-serialisable summary of the fusion result.

    This summary is stored in metadata_json and can be used by audit and
    export services to display interpretable information.
    """
    return {
        "fusion_version": fusion.get("fusion_version"),
        "fused_state_code": fusion.get("fused_state_code"),
        "fused_state_label": fusion.get("fused_state_label"),
        "decision_source": fusion.get("decision_source"),
        "confidence": fusion.get("confidence"),
        "assessment_status": fusion.get("assessment_status"),
        "should_intervene": fusion.get("should_intervene"),
        "self_regulation_detected": fusion.get("self_regulation_detected"),
        "summary_note": (
            "Self-regulation detected by decision fusion "
            f"(source={fusion.get('decision_source', 'unknown')})"
        ),
    }
