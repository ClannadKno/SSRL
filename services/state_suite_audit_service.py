# -*- coding: utf-8 -*-
"""Privacy-minimised database read model for canonical state-suite tests.

This module intentionally returns identifiers, scope fields, state decisions,
strategy routing, room-lease lifecycle, emotion-slot lifecycle, and structured
latency events only. It never returns message content, participant identity,
model prompts/responses, generated intervention text, or complete lock tokens.
"""

from __future__ import annotations

import json

from db import query_all, query_one


REQUIRED_TABLES = (
    "messages",
    "collaboration_state_segments",
    "state_assessment_batches",
    "strategy_pipeline_runs",
    "intervention_runs",
    "emotion_reflection_slots",
    "strategy_pipeline_latency_events",
)


# Keep the latency audit useful for attribution without exposing prompts,
# responses, generated intervention text, or complete lock tokens.
_SAFE_LATENCY_DETAIL_KEYS = {
    "model",
    "profile",
    "max_tokens",
    "timeout_seconds",
    "gateway_retries",
    "prompt_chars",
    "prompt_estimated_tokens",
    "response_chars",
    "finish_reason",
    "error",
    "failure_type",
    "failure_category",
    "stage3_failure_category",
    "parser_result",
    "local_parse_success",
    "response_starts_with_brace",
    "response_ends_with_brace",
    "json_extractable",
    "core_json_extractable",
    "incomplete_response",
    "attempt_type",
    "entered_repair",
    "stage3_attempt_count",
    "publish_gate_allowed",
    "publish_gate_reason",
    "publish_gate_result",
    "published_message_id",
    "lease_released",
    "release_reason",
    "success",
    "duplicate",
    "message_id",
    "selected_strategy_id",
}


def _safe_latency_details(value):
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _SAFE_LATENCY_DETAIL_KEYS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            item = item[:500]
        result[key] = item
    return result


def _json_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _int_list(value):
    result = []
    for item in _json_list(value):
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _table_exists(table_name):
    row = query_one(
        """
        SELECT 1 AS present
        FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table_name,),
    )
    return bool(row)


def _resolve_discussion(group_id, session_id, discussion_id=None):
    if discussion_id is not None:
        row = query_one(
            """
            SELECT id, group_id, session_id, status
            FROM group_session_discussions
            WHERE id=? AND group_id=? AND session_id=?
            """,
            (discussion_id, group_id, session_id),
        )
        if not row:
            raise ValueError(
                "discussion_id does not belong to the requested group/session"
            )
        return dict(row)

    row = query_one(
        """
        SELECT id, group_id, session_id, status
        FROM group_session_discussions
        WHERE group_id=? AND session_id=?
        ORDER BY
            CASE WHEN status='running' THEN 0 ELSE 1 END,
            id DESC
        LIMIT 1
        """,
        (group_id, session_id),
    )
    if not row:
        raise ValueError("no discussion exists for the requested group/session")
    return dict(row)


def _scoped_rows(sql, scope):
    return [
        dict(row)
        for row in query_all(
            sql,
            (
                scope["group_id"],
                scope["session_id"],
                scope["discussion_id"],
            ),
        )
    ]


def get_state_suite_audit(
    *,
    group_id,
    session_id,
    discussion_id=None,
):
    """Return a discussion-scoped, content-free audit snapshot."""
    group = query_one(
        """
        SELECT id, group_code, state, active_intervention_run_id,
               lock_expires_at
        FROM groups
        WHERE id=?
        """,
        (group_id,),
    )
    if not group:
        raise ValueError("group not found")
    session = query_one(
        "SELECT id, session_no, task_id, status FROM experiment_sessions WHERE id=?",
        (session_id,),
    )
    if not session:
        raise ValueError("session not found")

    discussion = _resolve_discussion(
        group_id,
        session_id,
        discussion_id=discussion_id,
    )
    scope = {
        "group_id": int(group_id),
        "group_code": group["group_code"],
        "session_id": int(session_id),
        "session_no": session["session_no"],
        "task_id": session["task_id"],
        "discussion_id": int(discussion["id"]),
        "discussion_status": discussion["status"],
    }
    available_tables = {
        table_name: _table_exists(table_name)
        for table_name in REQUIRED_TABLES
    }

    messages = _scoped_rows(
        """
        SELECT id, group_id, session_id, discussion_id, task_id, session_no,
               sequence, COALESCE(role, sender_type, '') AS role,
               sender_type, strategy_id, intervention_run_id, agent_type,
               trigger_source, created_at
        FROM messages
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY sequence, id
        """,
        scope,
    )
    sequence_by_message_id = {
        int(row["id"]): row.get("sequence")
        for row in messages
        if row.get("id") is not None
    }

    segments = _scoped_rows(
        """
        SELECT id, group_id, session_id, discussion_id, task_id, session_no,
               segment_kind, start_message_id, end_message_id,
               start_sequence, end_sequence, assessment_batch_id,
               strategy_pipeline_run_id, state_code, coarse_state_code,
               raw_sub_state_code, canonical_sub_state_code,
               secondary_tags_json, assessment_status, source, source_stage,
               should_intervene, selected_strategy_id,
               evidence_message_ids_json, evidence_sequences,
               fallback_reason, dedupe_key, is_finalized, created_at, updated_at
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY COALESCE(start_sequence, trigger_sequence, 0), id
        """,
        scope,
    )
    for row in segments:
        row["secondary_tags"] = _json_list(row.pop("secondary_tags_json", None))
        row["evidence_message_ids"] = _int_list(
            row.pop("evidence_message_ids_json", None)
        )
        row["evidence_sequences"] = _int_list(row.get("evidence_sequences"))

    batches = _scoped_rows(
        """
        SELECT id, group_id, session_id, discussion_id, task_id, session_no,
               candidate_start_sequence, candidate_end_sequence,
               context_start_sequence, context_end_sequence,
               trigger_type, trigger_sequence, status, terminal_status,
               error_code, fallback_action, fallback_segment_count,
               replacement_of_pipeline_run_id, replacement_reason,
               replacement_trigger_message_id, replacement_cutoff_sequence,
               attempt_count, max_attempts, created_at, completed_at, terminal_at
        FROM state_assessment_batches
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY id
        """,
        scope,
    )

    pipeline_runs = _scoped_rows(
        """
        SELECT id, run_uuid, group_id, session_id, discussion_id, task_id,
               assessment_batch_id, trigger_source,
               trigger_message_id,
               input_start_sequence, input_end_sequence,
               input_cutoff_student_sequence,
               trigger_level_state, latest_state, latest_should_intervene,
               latest_state_pipeline_run_id,
               coarse_state_code, canonical_sub_state_code,
               secondary_sub_state_tags_json,
               sub_state_start_sequence, sub_state_end_sequence,
               sub_state_evidence_message_ids_json,
               detected_self_regulation, fresh_detected_self_regulation,
               should_intervene, inhibition_strategy_id,
               suppression_type, suppression_strategy_id,
               suppression_evidence_message_ids_json,
               suppression_source_batch_id, suppression_source_segment_id,
               suppression_decision_reason, suppression_decision_at,
               strategy_candidate_ids_json, selected_strategy_id,
               supporting_strategy_ids_json, strategy_library_version,
               stage1_status, stage2_status, stage3_status,
               stage1_started_at, stage1_completed_at,
               stage2_started_at, stage2_completed_at,
               stage3_started_at, stage3_completed_at,
               room_lock_acquired_at, room_lock_released_at,
               publish_status, published_message_id, final_status,
               parent_run_id, superseded_by_run_id,
               replaced_by_pipeline_run_id, replacement_reason,
               replacement_trigger_message_id, replacement_cutoff_sequence,
               failure_code, skip_reason, created_at, updated_at
        FROM strategy_pipeline_runs
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY id
        """,
        scope,
    )
    for row in pipeline_runs:
        row["state_overlays"] = _json_list(
            row.pop("secondary_sub_state_tags_json", None)
        )
        evidence_ids = _int_list(
            row.pop("sub_state_evidence_message_ids_json", None)
        )
        row["evidence_message_ids"] = evidence_ids
        row["evidence_sequences"] = [
            sequence_by_message_id[item]
            for item in evidence_ids
            if sequence_by_message_id.get(item) is not None
        ]
        row["strategy_candidate_ids"] = _json_list(
            row.pop("strategy_candidate_ids_json", None)
        )
        row["supporting_strategy_ids"] = _json_list(
            row.pop("supporting_strategy_ids_json", None)
        )
        suppression_evidence_ids = _int_list(
            row.pop("suppression_evidence_message_ids_json", None)
        )
        row["suppression_evidence_message_ids"] = suppression_evidence_ids
        row["suppression_evidence_sequences"] = [
            sequence_by_message_id[item]
            for item in suppression_evidence_ids
            if sequence_by_message_id.get(item) is not None
        ]

    intervention_runs = _scoped_rows(
        """
        SELECT id, group_id, session_id, discussion_id, task_id, session_no,
               strategy_pipeline_run_id, assessment_batch_id, target_segment_id,
               cutoff_sequence, context_from_sequence, context_to_sequence,
               canonical_sub_state_code, detected_state,
               evidence_message_ids_json, evidence_sequences_json,
               decision, selected_strategy_id, strategy_candidate_ids_json,
               agent_type, status, publish_status, final_disposition,
               message_id, failure_reason, skip_reason,
               created_at, completed_at, published_at
        FROM intervention_runs
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY id
        """,
        scope,
    )
    for row in intervention_runs:
        evidence_ids = _int_list(row.pop("evidence_message_ids_json", None))
        row["evidence_message_ids"] = evidence_ids
        explicit_sequences = _int_list(
            row.pop("evidence_sequences_json", None)
        )
        row["evidence_sequences"] = explicit_sequences or [
            sequence_by_message_id[item]
            for item in evidence_ids
            if sequence_by_message_id.get(item) is not None
        ]
        row["strategy_candidate_ids"] = _json_list(
            row.pop("strategy_candidate_ids_json", None)
        )

    emotion_slots = _scoped_rows(
        """
        SELECT id, group_id, session_id, discussion_id, slot_index,
               scheduled_at, status, started_at, completed_at,
               message_id, intervention_run_id, skip_reason,
               retry_count, max_attempts, enqueued_at, next_retry_at,
               defer_count, defer_deadline_at, generation_student_sequence,
               superseded_by_slot_id, coordination_strategy_run_id,
               created_at, updated_at
        FROM emotion_reflection_slots
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY slot_index, id
        """,
        scope,
    )

    latency_events = _scoped_rows(
        """
        SELECT id, group_id, session_id, discussion_id, task_id,
               pipeline_run_id, assessment_batch_id, cutoff_sequence,
               lock_owner, lock_token_hash, call_id, attempt, stage, event,
               occurred_at, elapsed_ms, details_json, created_at
        FROM strategy_pipeline_latency_events
        WHERE group_id=? AND session_id=? AND discussion_id=?
        ORDER BY occurred_at, id
        """,
        scope,
    )
    for row in latency_events:
        row["details_json"] = json.dumps(
            _safe_latency_details(row.pop("details_json", None)),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    room_lock = {
        "group_id": int(group_id),
        "state": group["state"],
        "locked": str(group["state"] or "").upper() == "AI_INTERVENING",
        "active_intervention_run_id": group["active_intervention_run_id"],
        "lock_expires_at": group["lock_expires_at"],
        "complete_lock_token_included": False,
    }

    tables = {
        "messages": messages,
        "collaboration_state_segments": segments,
        "state_assessment_batches": batches,
        "strategy_pipeline_runs": pipeline_runs,
        "intervention_runs": intervention_runs,
        "emotion_reflection_slots": emotion_slots,
        "strategy_pipeline_latency_events": latency_events,
    }
    return {
        "schema_version": "state-suite-audit/2",
        "scope": scope,
        "room_lock": room_lock,
        "available_tables": available_tables,
        "audit_available": all(available_tables.values()),
        "privacy": {
            "message_content_included": False,
            "participant_identity_included": False,
            "model_payload_included": False,
            "generated_agent_text_included": False,
            "complete_lock_token_included": False,
        },
        "counts": {
            table_name: len(rows)
            for table_name, rows in tables.items()
        },
        "tables": tables,
    }
