# -*- coding: utf-8 -*-
"""Batch 8 route tests for the privacy-minimised DB-equivalent audit."""

from __future__ import annotations

import json
import uuid

from tests.test_teacher_canonical_state_batch4 import _seed_teacher_scope


def _seed_batch8_audit_rows(db, scope):
    now = db.now_str()
    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, session_no, task_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            context_start_sequence, context_end_sequence,
            trigger_type, trigger_sequence, window_key,
            status, terminal_status, attempt_count, max_attempts,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,1,2,1,2,'message_batch',2,?,
                 'succeeded','succeeded',1,2,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            "batch8-window-%s" % scope["discussion_id"],
            now,
            now,
        ),
    )
    message_ids = [
        row["id"]
        for row in db.query_all(
            """
            SELECT id
            FROM messages
            WHERE group_id=? AND session_id=? AND discussion_id=?
            ORDER BY sequence
            """,
            (
                scope["group_id"],
                scope["session_id"],
                scope["discussion_id"],
            ),
        )
    ]
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            assessment_batch_id, trigger_source,
            trigger_message_id,
            input_start_sequence, input_end_sequence,
            coarse_state_code, canonical_sub_state_code,
            secondary_sub_state_tags_json,
            sub_state_start_sequence, sub_state_end_sequence,
            sub_state_evidence_message_ids_json,
            should_intervene, inhibition_strategy_id,
            strategy_candidate_ids_json, supporting_strategy_ids_json,
            stage1_status, stage2_status, stage3_status,
            stage1_started_at, stage1_completed_at,
            stage2_started_at, stage2_completed_at,
            room_lock_acquired_at, room_lock_released_at,
            publish_status, final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,'authoritative_batch',?,1,2,'positive_collaboration','execution_progress',
                 '[]',1,2,?,0,'OI-004','["OI-004"]','[]',
                 'SUCCEEDED','SUCCEEDED','SUPPRESSED',
                 ?,?,?,?,?,?,
                 'NOT_PUBLISHED','SUPPRESSED',?,?)
        """,
        (
            str(uuid.uuid4()),
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            batch_id,
            message_ids[0],
            json.dumps(message_ids),
            now,
            now,
            now,
            now,
            now,
            now,
            now,
            now,
        ),
    )
    intervention_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, discussion_id, session_no, task_id,
            assessment_batch_id, target_segment_id,
            context_from_sequence, context_to_sequence,
            strategy_pipeline_run_id, canonical_sub_state_code,
            evidence_message_ids_json, evidence_sequences_json,
            selected_strategy_id, strategy_candidate_ids_json,
            agent_type, status, publish_status, final_disposition,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'["OI-004"]',
                 'strategy','suppressed','NOT_PUBLISHED','SUPPRESSED',?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            scope["session_no"],
            scope["task_id"],
            batch_id,
            None,
            1,
            2,
            pipeline_id,
            "execution_progress",
            json.dumps(message_ids),
            json.dumps([1, 2]),
            now,
            now,
        ),
    )
    slot_id = db.execute(
        """
        INSERT INTO emotion_reflection_slots(
            group_id, session_id, discussion_id, slot_index, scheduled_at,
            status, next_retry_at, defer_count, defer_deadline_at,
            coordination_strategy_run_id, created_at, updated_at
        ) VALUES(?,?,?,?,?,'deferred',?,1,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            1,
            now,
            now,
            now,
            pipeline_id,
            now,
            now,
        ),
    )
    latency_event_id = db.execute(
        """
        INSERT INTO strategy_pipeline_latency_events(
            group_id, session_id, discussion_id, task_id,
            pipeline_run_id, assessment_batch_id, cutoff_sequence,
            lock_owner, lock_token_hash, stage, event, occurred_at,
            elapsed_ms, details_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'room_lock','room_lock_acquired',?,0,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            scope["task_id"],
            pipeline_id,
            batch_id,
            2,
            -pipeline_id,
            "sha256:test-only",
            now,
            json.dumps({
                "publish_gate_allowed": False,
                "publish_gate_result": "blocked",
                "prompt_chars": 12,
                "raw_output": "must not be exposed",
                "content": "must not be exposed",
            }),
            now,
        ),
    )
    return {
        "batch_id": batch_id,
        "pipeline_id": pipeline_id,
        "intervention_id": intervention_id,
        "message_ids": message_ids,
        "slot_id": slot_id,
        "latency_event_id": latency_event_id,
    }


def test_state_suite_audit_is_disabled_by_default(
    db_and_app,
    teacher_login,
):
    db, app_module, _client = db_and_app
    client, headers = teacher_login
    scope = _seed_teacher_scope(db)
    app_module.app.config["TESTING"] = False

    response = client.get(
        "/api/teacher/group/%s/state-suite-audit" % scope["group_id"],
        query_string={"session_id": scope["session_id"]},
        headers=headers,
    )
    assert response.status_code == 404


def test_state_suite_audit_returns_batch6_scoped_tables_without_sensitive_data(
    db_and_app,
    teacher_login,
):
    db, app_module, _client = db_and_app
    client, headers = teacher_login
    scope = _seed_teacher_scope(db)
    seeded = _seed_batch8_audit_rows(db, scope)
    app_module.app.config["TESTING"] = True

    response = client.get(
        "/api/teacher/group/%s/state-suite-audit" % scope["group_id"],
        query_string={
            "session_id": scope["session_id"],
            "discussion_id": scope["discussion_id"],
        },
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["schema_version"] == "state-suite-audit/2"
    assert payload["audit_available"] is True
    assert payload["scope"]["group_id"] == scope["group_id"]
    assert payload["scope"]["session_id"] == scope["session_id"]
    assert payload["scope"]["discussion_id"] == scope["discussion_id"]
    assert set(payload["tables"]) == {
        "messages",
        "collaboration_state_segments",
        "state_assessment_batches",
        "strategy_pipeline_runs",
        "intervention_runs",
        "emotion_reflection_slots",
        "strategy_pipeline_latency_events",
    }
    assert payload["counts"]["messages"] == 2
    assert payload["tables"]["strategy_pipeline_runs"][0]["id"] == seeded[
        "pipeline_id"
    ]
    assert payload["tables"]["strategy_pipeline_runs"][0]["trigger_message_id"] == seeded[
        "message_ids"
    ][0]
    assert payload["tables"]["strategy_pipeline_runs"][0][
        "evidence_message_ids"
    ] == seeded["message_ids"]
    assert payload["tables"]["strategy_pipeline_runs"][0][
        "evidence_sequences"
    ] == [1, 2]
    assert payload["tables"]["intervention_runs"][0]["id"] == seeded[
        "intervention_id"
    ]
    assert payload["tables"]["emotion_reflection_slots"][0]["id"] == seeded[
        "slot_id"
    ]
    assert payload["tables"]["strategy_pipeline_latency_events"][0]["id"] == seeded[
        "latency_event_id"
    ]
    latency_details = json.loads(
        payload["tables"]["strategy_pipeline_latency_events"][0]["details_json"]
    )
    assert latency_details == {
        "prompt_chars": 12,
        "publish_gate_allowed": False,
        "publish_gate_result": "blocked",
    }
    assert payload["room_lock"]["complete_lock_token_included"] is False
    assert "lock_token" not in payload["room_lock"]
    assert payload["privacy"] == {
        "message_content_included": False,
        "participant_identity_included": False,
        "model_payload_included": False,
        "generated_agent_text_included": False,
        "complete_lock_token_included": False,
    }

    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        '"content"',
        '"user_id"',
        '"participant_code"',
        '"display_name"',
        '"raw_response"',
        '"generated_text"',
        '"validated_text"',
        '"room_lock_token"',
    ):
        assert forbidden not in serialized

    wrong_scope = client.get(
        "/api/teacher/group/%s/state-suite-audit" % scope["group_id"],
        query_string={
            "session_id": scope["session_id"],
            "discussion_id": scope["discussion_id"] + 999,
        },
        headers=headers,
    )
    assert wrong_scope.status_code == 400
