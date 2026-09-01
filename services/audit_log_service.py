# -*- coding: utf-8 -*-
"""Lightweight safe wrapper around db.write_audit_log.

All V2 pipeline audit logging goes through this module so that:
- Exceptions are caught and logged, never blocking the main flow.
- Metadata is consistently JSON-serialized and placed in after_value.
- Sensitive content (full prompts, student original text) is excluded.
"""
import json
import logging

logger = logging.getLogger(__name__)


def safe_write_audit_log(
    action_type: str,
    actor_type: str = "system",
    actor_id: str = "pipeline_v2",
    target_type: str = None,
    target_id=None,
    metadata: dict = None,
) -> bool:
    """Write an audit log entry with safe defaults. Never raises.

    Args:
        action_type: Stable event name, e.g. "detection.complete".
        actor_type:  Kind of actor ("system", "teacher", "student").
        actor_id:    Actor identifier ("pipeline_v2", "intervention_v2", user id).
        target_type: e.g. "monitor_run", "intervention_run", "group".
        target_id:   Primary key of the target.
        metadata:    Dict of structured metadata serialized into after_value.

    Returns:
        True if written successfully, False on any error.
    """
    try:
        from db import write_audit_log

        metadata_json = json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None

        # Build a short human-readable reason for convenience
        reason_parts = [f"actor={actor_type}/{actor_id}"]
        if metadata:
            # Include key summary fields only (no prompt, no student text)
            for key in ("final_state", "strategy_id", "state_code", "success", "skip_reason"):
                val = metadata.get(key)
                if val is not None:
                    reason_parts.append(f"{key}={val}")
        reason = " | ".join(reason_parts)

        operator_id = f"{actor_type}/{actor_id}"

        write_audit_log(
            operator_id=operator_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            after_value=metadata_json,
            reason=reason,
        )
        return True

    except Exception as exc:
        logger.warning(
            "safe_write_audit_log failed for action_type=%s target_type=%s target_id=%s: %s",
            action_type, target_type, target_id, exc,
        )
        return False
