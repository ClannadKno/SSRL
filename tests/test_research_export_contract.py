# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import io
import json
import zipfile

from tests.helpers import create_group, create_student, seed_running_session


def _zip(response):
    return zipfile.ZipFile(io.BytesIO(response.get_data()))


def _csv_rows(archive, path):
    text = archive.read(path).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _seed_research_export(db):
    scope = seed_running_session(db, session_no=91, member_count=2)
    now = db.now_str()
    db.execute(
        "UPDATE experiment_sessions SET title=? WHERE id=?",
        ("第一周任务", scope["session_id"]),
    )
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, created_at, updated_at
        ) VALUES(?,?,'running',?,?)
        """,
        (scope["session_id"], scope["group_id"], now, now),
    )
    student_id = scope["students"][0][0]
    first_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], student_id, "先列出约束。", 1, "student", "student",
            scope["session_no"], scope["task_id"], scope["session_id"], discussion_id, now,
        ),
    )
    second_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], student_id, "再比较两个方案。", 2, "student", "student",
            scope["session_no"], scope["task_id"], scope["session_id"], discussion_id, now,
        ),
    )
    fallback_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], student_id, "This message has no persisted state.", 3,
            "student", "student", scope["session_no"], scope["task_id"],
            scope["session_id"], discussion_id, now,
        ),
    )
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_message_id, input_start_sequence, input_end_sequence,
            coarse_should_escalate, coarse_state_code,
            canonical_sub_state_code, secondary_sub_state_tags_json,
            sub_state_evidence_message_ids_json,
            should_intervene, strategy_candidate_ids_json, selected_strategy_id,
            strategy_selection_reason, stage1_status, stage2_status, stage3_status,
            publish_status, final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "research-export-run", scope["group_id"], scope["session_id"],
            scope["session_no"], discussion_id, scope["task_id"], "student_message",
            first_message_id, 1, 2, 1, "conflict", "interpersonal_conflict", "[]", "[1]",
            1, '["ER-001"]', "ER-001", "需要把分歧拉回证据", "completed",
            "completed", "completed", "pending", "running", now, now,
        ),
    )
    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, session_no, task_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence, trigger_type,
            window_key, status, terminal_status, model, prompt_version,
            started_at, completed_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'succeeded','succeeded',?,?,?,?,?,?)
        """,
        (
            scope["group_id"], scope["session_id"], scope["session_no"],
            scope["task_id"], discussion_id, 1, 2, "student_message", "window-1-2",
            "state-model", "state-prompt-v1", now, now, now, now,
        ),
    )
    segment_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, segment_kind, assessment_batch_id,
            start_message_id, end_message_id, start_sequence, end_sequence,
            evidence_message_ids_json,
            evidence_sequences, confidence, source, assessment_status,
            trigger_type, prompt_version, is_finalized, dedupe_key,
            created_at, updated_at, canonical_sub_state_code,
            secondary_tags_json, sub_state_confidence,
            strategy_pipeline_run_id, should_intervene, source_stage
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], scope["session_id"], scope["session_no"],
            scope["task_id"], discussion_id, "interpersonal_conflict", "message_range",
            batch_id, 1, 2, 1, 2, "[1]", "[1]", 0.91, "llm", "confirmed",
            "student_message", "state-prompt-v1", 1, "research-segment", now, now,
            "interpersonal_conflict", "[]", 0.91, pipeline_id, 1, "stage2",
        ),
    )
    agent_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at,
            agent_type, strategy_id, trigger_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], db.get_sera_user_id(), "请用证据说明各自理由。", 4,
            "agent", "agent", scope["session_no"], scope["task_id"],
            scope["session_id"], discussion_id, now, "strategy", "ER-001", "student_message",
        ),
    )
    db.execute(
        """UPDATE strategy_pipeline_runs
              SET validated_intervention_text=?, published_message_id=?, published_at=?,
                  publish_status='published', final_status='published'
            WHERE id=?""",
        ("请用证据说明各自理由。", agent_message_id, now, pipeline_id),
    )
    intervention_run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, discussion_id, session_no, task_id,
            status, trigger_type, selected_strategy_id, message_id,
            strategy_pipeline_run_id, canonical_sub_state_code,
            publish_status, actual_published_at, created_at
        ) VALUES(?,?,?,?,?,'published',?,?,?,?,?,'published',?,?)
        """,
        (
            scope["group_id"], scope["session_id"], discussion_id,
            scope["session_no"], scope["task_id"], "student_message", "ER-001",
            agent_message_id, pipeline_id, "interpersonal_conflict", now, now,
        ),
    )
    intervention_log_id = db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, intervention_run_id, message_id,
            message, trigger_source, strategy_id, session_id,
            session_no, task_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], intervention_run_id, intervention_run_id,
            agent_message_id, "请用证据说明各自理由。", "student_message", "ER-001",
            scope["session_id"], scope["session_no"], scope["task_id"], discussion_id, now,
        ),
    )
    db.execute("UPDATE messages SET linked_log_id=? WHERE id=?", (intervention_log_id, agent_message_id))
    help_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at,
            agent_type, trigger_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], db.get_sera_user_id(), "先明确分工，再逐项推进。", 5,
            "agent", "agent", scope["session_no"], scope["task_id"],
            scope["session_id"], discussion_id, now, "strategy", "help_request",
        ),
    )
    help_id = db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            discussion_id, status, request_text, intent, source_message_id,
            response_message_id, intervention_run_id, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,'completed',?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], student_id, scope["task_id"], scope["session_no"],
            scope["session_id"], discussion_id, "请帮我们推进", "process_help",
            first_message_id, help_message_id, None, now, now,
        ),
    )
    assert help_id
    emotion_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at,
            agent_type, trigger_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], db.get_sera_user_id(), "小组正在稳定推进，请继续保持。", 6,
            "agent", "agent", scope["session_no"], scope["task_id"],
            scope["session_id"], discussion_id, now, "emotion", "emotion_slot",
        ),
    )
    emotion_slot_id = db.execute(
        """
        INSERT INTO emotion_reflection_slots(
            group_id, session_id, discussion_id, slot_index, scheduled_at,
            prompt_version, previous_metrics_json, current_metrics_json,
            status, completed_at, message_id, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], scope["session_id"], discussion_id, 1, now,
            "emotion-slot-v1", "{}", "{}", "sent", now,
            emotion_message_id, now, now,
        ),
    )
    emotion_assessment_id = db.execute(
        """
        INSERT INTO emotion_feedback_assessments(
            slot_id, group_id, session_id, discussion_id, slot_index,
            prompt_version, status, emotion_feedback_state, confidence,
            previous_metrics_json, current_metrics_json,
            evidence_message_ids_json, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            emotion_slot_id, scope["group_id"], scope["session_id"], discussion_id,
            1, "emotion-e1-v1", "SUCCEEDED", "GROUP_IMPROVING", 0.88,
            "{}", "{}", json.dumps([second_message_id]), now, now,
        ),
    )
    db.execute(
        """
        INSERT INTO emotion_feedback_generations(
            slot_id, assessment_id, attempt_no, prompt_version,
            emotion_feedback_state, status, final_text, validation_status,
            published_message_id, published_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            emotion_slot_id, emotion_assessment_id, 1, "emotion-e2-v1",
            "GROUP_IMPROVING", "PUBLISHED", "小组正在稳定推进，请继续保持。",
            "VALID", emotion_message_id, now, now, now,
        ),
    )
    db.execute(
        """
        INSERT INTO emotion_checkins(
            group_id, user_id, session_no, emotion_option, checkin_type,
            created_at, positivity, engagement, atmosphere,
            expression_willingness, note, session_id, task_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], student_id, scope["session_no"], "专注", "during",
            now, 4, 5, 4, 5, "正常", scope["session_id"], scope["task_id"],
        ),
    )
    db.execute(
        """
        INSERT INTO collaborative_documents(
            group_id, task_id, session_no, title, content_text, status,
            created_by, created_at, updated_at, submitted_at, session_id
        ) VALUES(?,?,?,?,?,'submitted',?,?,?,?,?)
        """,
        (
            scope["group_id"], scope["task_id"], scope["session_no"], "最终方案",
            "# 最终方案\n\n- 保留学生原文", student_id, now, now, now, scope["session_id"],
        ),
    )
    questionnaire_id = db.create_questionnaire(
        {"code": "reflection", "title": "反思问卷", "timing": "post", "active": True},
        items=[{
            "item_code": "R1", "prompt_text": "请写下反思", "dimension_label": "反思",
            "sort_order": 1, "question_type": "text",
        }],
    )
    item_id = db.query_one(
        "SELECT id FROM questionnaire_items WHERE questionnaire_id=?",
        (questionnaire_id,),
    )["id"]
    db.create_questionnaire_submission(
        questionnaire_id, student_id, scope["group_id"], scope["session_id"],
        scope["session_no"], "post", {str(item_id): {"text": "=SUM(1,2)"}},
    )
    scope.update(
        discussion_id=discussion_id,
        pipeline_id=pipeline_id,
        segment_id=segment_id,
        first_message_id=first_message_id,
        fallback_message_id=fallback_message_id,
        agent_message_id=agent_message_id,
        help_message_id=help_message_id,
        help_id=help_id,
        emotion_message_id=emotion_message_id,
        emotion_slot_id=emotion_slot_id,
    )
    return scope


def test_fixed_export_schemas_and_removed_registry_entries():
    from services.research_export_service import (
        EXPORT_SCHEMAS,
        MESSAGES_EXPORT_COLUMNS,
        STATE_ASSESSMENTS_EXPORT_COLUMNS,
    )
    from services.teacher_export_service import EXPORT_REGISTRY

    assert EXPORT_SCHEMAS["messages"][1] == MESSAGES_EXPORT_COLUMNS
    assert MESSAGES_EXPORT_COLUMNS == [
        "session_id", "group_id", "message_id", "sequence", "sender_role",
        "participant_code", "agent_type", "agent_event_id", "agent_reference_code",
        "state_code", "state_assignment_source", "selected_strategy_id", "content",
        "reply_to_message_id", "created_at",
    ]
    assert "state_code" in STATE_ASSESSMENTS_EXPORT_COLUMNS
    assert "rule_result_json" not in STATE_ASSESSMENTS_EXPORT_COLUMNS
    assert not {
        "strategy_reviews.csv", "intervention_uptake.csv", "unified-events.csv", "audit_logs.csv"
    } & set(EXPORT_REGISTRY)

    expected = {
        "state-assessments": [
            "session_id", "group_id", "assessment_id", "pipeline_run_id", "trigger_source",
            "window_start_sequence", "window_end_sequence", "state_code", "state_overlays",
            "confidence", "evidence_message_ids", "assessment_status",
            "failure_code", "latency_ms", "model_name", "prompt_version", "created_at",
        ],
        "strategy-pipeline": [
            "session_id", "group_id", "pipeline_run_id", "trigger_source",
            "trigger_message_id", "input_start_sequence", "input_end_sequence",
            "stage1_need_intervention", "stage1_candidate_states", "state_assessment_id",
            "state_code", "state_overlays", "routing_type", "candidate_strategy_ids",
            "selected_strategy_id", "inhibition_strategy_id", "strategy_selection_reason",
            "generation_status", "publish_status", "published_message_id", "skip_reason",
            "failure_code", "stage1_latency_ms", "stage2_latency_ms", "stage3_latency_ms",
            "total_latency_ms", "created_at", "completed_at",
        ],
        "interventions": [
            "session_id", "group_id", "intervention_id", "pipeline_run_id", "message_id",
            "trigger_source", "trigger_message_id", "state_code", "selected_strategy_id",
            "content", "published_at",
        ],
        "participation": [
            "session_id", "group_id", "participant_code", "message_count",
            "character_count", "active_minutes", "first_message_at", "last_message_at",
        ],
        "emotion-checkins": [
            "session_id", "group_id", "checkin_id", "participant_code", "checkin_type",
            "emotion_option", "positivity", "engagement", "atmosphere",
            "expression_willingness", "note", "created_at",
        ],
        "help-requests": [
            "session_id", "group_id", "help_request_id", "participant_code", "request_text",
            "intent", "source_message_id", "status", "response_message_id", "pipeline_run_id",
            "failure_code", "created_at", "completed_at",
        ],
        "questionnaires": [
            "session_id", "group_id", "participant_code",
        ],
    }
    for key, columns in expected.items():
        assert EXPORT_SCHEMAS[key][1] == columns


def test_unscoped_rows_are_reported_without_global_directories(db_and_app, teacher_login):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = seed_running_session(db, session_no=92, member_count=1)
    student_id = scope["students"][0][0]
    db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            scope["group_id"], student_id, "历史脏数据", 1, "student", "student",
            scope["session_no"], scope["task_id"], db.now_str(),
        ),
    )

    response = client.get("/export/messages", headers=headers)
    with _zip(response) as archive:
        assert set(archive.namelist()) == {"manifest.json", "README.md"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["excluded_rows"]["messages"] == 1
        assert manifest["warnings"]
        assert "_global" not in archive.read("README.md").decode("utf-8")


def test_all_export_has_only_structured_research_files(db_and_app, teacher_login):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = _seed_research_export(db)

    response = client.get("/export/all?session_id=999&blind=1", headers=headers)
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert response.headers["X-Export-Mode"] == "full_nonblinded"

    with _zip(response) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        assert "README.md" in names
        assert all("_global" not in name for name in names)
        assert all("_ungrouped" not in name for name in names)
        assert all("_empty" not in name for name in names)
        data_names = [name for name in names if name not in {"manifest.json", "README.md"}]
        assert data_names
        assert "questionnaire_items.csv" not in data_names
        assert any(name.endswith("/questionnaire_items.csv") for name in data_names)
        assert all(name.startswith("sessions/") for name in data_names)
        assert not any(name.endswith("intervention_uptake.csv") for name in names)
        assert not any(name.endswith("unified-events.csv") for name in names)
        assert not any(name.endswith("audit_logs.csv") for name in names)
        assert not any(name.endswith("strategy_reviews.csv") for name in names)

        messages_path = next(name for name in names if name.endswith("/messages.csv"))
        state_path = next(name for name in names if name.endswith("/state_assessments.csv"))
        pipeline_path = next(name for name in names if name.endswith("/strategy_pipeline.csv"))
        intervention_path = next(name for name in names if name.endswith("/interventions.csv"))
        assert "/G91/" in messages_path

        messages = _csv_rows(archive, messages_path)
        assert list(messages[0]) == [
            "session_id", "group_id", "message_id", "sequence", "sender_role",
            "participant_code", "agent_type", "agent_event_id", "agent_reference_code",
            "state_code", "state_assignment_source", "selected_strategy_id", "content",
            "reply_to_message_id", "created_at",
        ]
        student_message = next(
            row for row in messages if row["message_id"] == str(scope["first_message_id"])
        )
        agent_message = next(
            row for row in messages if row["message_id"] == str(scope["agent_message_id"])
        )
        help_message = next(
            row for row in messages if row["message_id"] == str(scope["help_message_id"])
        )
        emotion_message = next(
            row for row in messages if row["message_id"] == str(scope["emotion_message_id"])
        )
        fallback_message = next(
            row for row in messages if row["message_id"] == str(scope["fallback_message_id"])
        )
        assert student_message["state_code"] == "interpersonal_conflict"
        assert student_message["state_assignment_source"] == "detected"
        assert student_message["selected_strategy_id"] == ""
        assert agent_message["agent_type"] == "strategy"
        assert agent_message["agent_reference_code"] == "ER-001"
        assert agent_message["state_code"] == ""
        assert agent_message["state_assignment_source"] == ""
        assert agent_message["selected_strategy_id"] == "ER-001"
        assert help_message["agent_type"] == "help"
        assert help_message["agent_reference_code"] == "HELP_REQUEST_RESPONSE"
        assert help_message["agent_event_id"] == str(scope["help_id"])
        assert help_message["selected_strategy_id"] == ""
        assert emotion_message["agent_type"] == "emotion"
        assert emotion_message["agent_reference_code"] == "GROUP_IMPROVING"
        assert emotion_message["agent_event_id"].endswith("-1")
        assert emotion_message["selected_strategy_id"] == ""
        assert fallback_message["state_code"] == "standard"
        assert fallback_message["state_assignment_source"] == "export_fallback"
        states = _csv_rows(archive, state_path)
        assert len(states) == 1
        assert states[0]["state_code"] == "interpersonal_conflict"
        assert json.loads(states[0]["evidence_message_ids"]) == [scope["first_message_id"]]
        assert "should_intervene" not in states[0]
        pipelines = _csv_rows(archive, pipeline_path)
        assert pipelines[0]["routing_type"] == "required"
        assert "generated_intervention_text" not in pipelines[0]
        interventions = _csv_rows(archive, intervention_path)
        assert interventions[0]["message_id"] == str(scope["agent_message_id"])

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["export_scope"] == "all"
        assert manifest["export_mode"] == "full_nonblinded"
        assert manifest["path_structure"].startswith("sessions/")
        assert "filters" not in manifest
        assert manifest["excluded_rows"]["messages"] == 0
        assert manifest["schema_versions"]["messages.csv"] == "2.0"
        assert manifest["schema_versions"]["state_assessments.csv"] == "2.0"
        assert manifest["state_export_fallback"]["total_rows"] == 1
        assert manifest["agent_message_statistics"] == {
            "strategy": 1, "emotion": 1, "help": 1, "unclassified": 0,
        }
        assert manifest["deduplication_statistics"]["state_assessments_removed"] == 0
        assert all(value == 0 for value in manifest["validation_results"].values())
        inventory = manifest["dataset_inventory"]
        item = next(
            entry
            for entry in inventory
            if entry["session_id"] == scope["session_id"]
            and entry["group_id"] == scope["group_id"]
        )
        assert set(item["files"]) == {
            "messages.csv", "state_assessments.csv", "strategy_pipeline.csv",
            "interventions.csv", "participation_summary.csv", "emotion_checkins.csv",
            "emotion_feedback.csv", "help_requests.csv",
        }
        for filename, file_info in item["files"].items():
            path = next(name for name in names if name.endswith("/" + filename))
            assert file_info["generated"] is True
            assert file_info["rows"] == len(_csv_rows(archive, path))


def test_state_assessments_are_pipeline_scoped_deduplicated_and_keep_failures(
    db_and_app, teacher_login
):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = _seed_research_export(db)
    now = db.now_str()

    db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, segment_kind, assessment_batch_id, strategy_pipeline_run_id,
            start_message_id, end_message_id, start_sequence, end_sequence,
            evidence_message_ids_json, evidence_sequences, confidence,
            source, assessment_status, trigger_type, prompt_version,
            is_finalized, dedupe_key, created_at, updated_at,
            canonical_sub_state_code, secondary_tags_json, sub_state_confidence,
            should_intervene, source_stage
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], scope["session_id"], scope["session_no"],
            scope["task_id"], scope["discussion_id"], "standard", "message_range",
            None, scope["pipeline_id"],
            scope["first_message_id"], scope["fallback_message_id"], 1, 3,
            "[]", "[]", 0.7, "llm", "failed", "student_message",
            "old-prompt", 1, "duplicate-export-candidate", now, now,
            "standard", "[]", 0.7, 0, "stage2",
        ),
    )
    unreferenced_segment_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, segment_kind, start_message_id, end_message_id,
            start_sequence, end_sequence, evidence_message_ids_json,
            evidence_sequences, confidence, source, assessment_status,
            trigger_type, prompt_version, is_finalized, dedupe_key,
            created_at, updated_at, canonical_sub_state_code,
            secondary_tags_json, sub_state_confidence, source_stage
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], scope["session_id"], scope["session_no"],
            scope["task_id"], scope["discussion_id"], "frustration", "message_range",
            scope["first_message_id"], scope["first_message_id"], 1, 1,
            "[]", "[]", 0.6, "llm", "confirmed", "legacy_scan",
            "legacy-prompt", 1, "unreferenced-stage2-record", now, now,
            "frustration", "[]", 0.6, "stage2",
        ),
    )
    failed_pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, input_start_sequence, input_end_sequence,
            stage1_status, stage2_status, stage2_started_at, stage2_completed_at,
            failure_code, final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "failed-stage2-export", scope["group_id"], scope["session_id"],
            scope["session_no"], scope["discussion_id"], scope["task_id"],
            "student_message", 2, 3, "completed", "failed", now, now,
            "MODEL_TIMEOUT", "failed", now, now,
        ),
    )
    observation_pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, input_start_sequence, input_end_sequence,
            stage1_status, stage2_status, stage2_started_at, stage2_completed_at,
            canonical_sub_state_code, sub_state_confidence,
            sub_state_evidence_message_ids_json, should_intervene,
            final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "observation-stage2-export", scope["group_id"], scope["session_id"],
            scope["session_no"], scope["discussion_id"], scope["task_id"],
            "student_message", 1, 2, "completed", "completed", now, now,
            "execution_progress", 0.83, json.dumps([scope["first_message_id"]]),
            0, "completed", now, now,
        ),
    )

    response = client.get("/export/all", headers=headers)
    with _zip(response) as archive:
        state_path = next(
            name for name in archive.namelist() if name.endswith("/state_assessments.csv")
        )
        pipeline_path = next(
            name for name in archive.namelist() if name.endswith("/strategy_pipeline.csv")
        )
        states = _csv_rows(archive, state_path)
        pipelines = _csv_rows(archive, pipeline_path)
        by_pipeline = {int(row["pipeline_run_id"]): row for row in states}

        assert set(by_pipeline) == {
            scope["pipeline_id"], failed_pipeline_id, observation_pipeline_id,
        }
        assert by_pipeline[scope["pipeline_id"]]["state_code"] == "interpersonal_conflict"
        assert by_pipeline[failed_pipeline_id]["assessment_status"] == "failed"
        assert by_pipeline[failed_pipeline_id]["failure_code"] == "MODEL_TIMEOUT"
        assert by_pipeline[failed_pipeline_id]["state_code"] == ""
        assert by_pipeline[observation_pipeline_id]["state_code"] == "execution_progress"
        assert all("should_intervene" not in row for row in states)
        assert all(
            row["assessment_id"] != "segment-%s" % unreferenced_segment_id
            for row in states
        )
        pipeline_assessment_ids = {
            int(row["pipeline_run_id"]): row["state_assessment_id"] for row in pipelines
        }
        assert pipeline_assessment_ids == {
            pipeline_id: row["assessment_id"] for pipeline_id, row in by_pipeline.items()
        }

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["deduplication_statistics"]["state_assessments_removed"] == 1
        assert manifest["validation_results"]["duplicate_pipeline_assessments"] == 0


def test_all_export_generates_header_only_group_files(db_and_app, teacher_login):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = seed_running_session(db, session_no=93, member_count=1)
    now = db.now_str()
    db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, created_at, updated_at
        ) VALUES(?,?,'running',?,?)
        """,
        (scope["session_id"], scope["group_id"], now, now),
    )

    response = client.get("/export/all", headers=headers)
    with _zip(response) as archive:
        names = archive.namelist()
        group_csvs = [
            name for name in names
            if name.endswith(".csv") and not name.endswith("/questionnaire_items.csv")
        ]
        assert len(group_csvs) == 8
        expected_empty = {
            "messages.csv", "state_assessments.csv", "strategy_pipeline.csv",
            "interventions.csv", "emotion_checkins.csv", "emotion_feedback.csv",
            "help_requests.csv",
        }
        for filename in expected_empty:
            path = next(name for name in group_csvs if name.endswith("/" + filename))
            assert _csv_rows(archive, path) == []

        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["dataset_inventory"]) == 1
        inventory = manifest["dataset_inventory"][0]
        assert set(inventory["files"]) == {
            "messages.csv", "state_assessments.csv", "strategy_pipeline.csv",
            "interventions.csv", "participation_summary.csv", "emotion_checkins.csv",
            "emotion_feedback.csv", "help_requests.csv",
        }
        assert all(
            inventory["files"][filename]["rows"] == 0
            for filename in expected_empty
        )
        assert manifest["validation_results"]["inventory_row_count_mismatches"] == 0
        readme = archive.read("README.md").decode("utf-8")
        assert "固定生成 8 个小组级 CSV" in readme
        assert "HELP_REQUEST_RESPONSE" in readme
        assert "export_fallback" in readme


def test_unclassified_agent_messages_are_retained_and_warned(db_and_app, teacher_login):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = seed_running_session(db, session_no=94, member_count=1)
    now = db.now_str()
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, created_at, updated_at
        ) VALUES(?,?,'running',?,?)
        """,
        (scope["session_id"], scope["group_id"], now, now),
    )
    message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"], db.get_sera_user_id(), "Unclassified legacy agent text.",
            1, "agent", "agent", scope["session_no"], scope["task_id"],
            scope["session_id"], discussion_id, now,
        ),
    )

    response = client.get("/export/all", headers=headers)
    with _zip(response) as archive:
        message_path = next(
            name for name in archive.namelist() if name.endswith("/messages.csv")
        )
        messages = _csv_rows(archive, message_path)
        row = next(item for item in messages if item["message_id"] == str(message_id))
        assert row["content"] == "Unclassified legacy agent text."
        assert row["agent_type"] == ""
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["agent_message_statistics"]["unclassified"] == 1
        assert manifest["validation_results"]["agent_messages_without_type"] == 1
        assert any("could not be classified" in warning for warning in manifest["warnings"])


def test_ambiguous_emotion_text_match_is_not_selected_arbitrarily():
    from services.research_export_service import _resolve_emotion_message_links

    messages = [
        {
            "message_id": message_id,
            "session_id": 1,
            "group_id": 2,
            "role": "agent",
            "agent_type": "emotion",
            "content": "same text",
            "created_at": "2026-08-06 10:00:00",
        }
        for message_id in (10, 11)
    ]
    events = [{
        "slot_id": 7,
        "session_id": 1,
        "group_id": 2,
        "message_id": None,
        "final_text": "same text",
        "published_at": "2026-08-06 10:00:00",
    }]

    resolved, diagnostics = _resolve_emotion_message_links(messages, events)

    assert resolved == {}
    assert diagnostics["unmatched_event_count"] == 1
    assert diagnostics["warnings"]


def test_questionnaire_wide_table_and_items_manifest(db_and_app, teacher_login):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = _seed_research_export(db)

    questionnaire_response = client.get("/export/questionnaires", headers=headers)
    with _zip(questionnaire_response) as archive:
        questionnaire_path = next(name for name in archive.namelist() if name.endswith("reflection_post.csv"))
        assert questionnaire_path.endswith("/questionnaires/reflection_post.csv")
        assert "/questionnaires/" in questionnaire_path
        assert "/questionnaires/" not in questionnaire_path.replace(
            "/questionnaires/reflection_post.csv", ""
        )
        rows = _csv_rows(archive, questionnaire_path)
        assert len(rows) == 1
        assert list(rows[0]) == ["session_id", "group_id", "participant_code", "R1"]
        assert rows[0]["session_id"] == str(scope["session_id"])
        assert rows[0]["group_id"] == str(scope["group_id"])
        assert rows[0]["participant_code"]
        assert rows[0]["R1"] == "=SUM(1,2)"
        assert "submission_id" not in rows[0]
        assert "response_text" not in rows[0]
        assert "raw_answer" not in rows[0]
        assert "total_score" not in rows[0]
        assert "dimension_score" not in rows[0]
        item_path = questionnaire_path.rsplit("/questionnaires/", 1)[0]
        item_path += "/questionnaire_items.csv"
        item_rows = _csv_rows(archive, item_path)
        assert list(item_rows[0]) == [
            "questionnaire_code", "item_code", "dimension_label", "prompt_text",
        ]
        assert item_rows == [{
            "questionnaire_code": "reflection",
            "item_code": "R1",
            "dimension_label": "反思",
            "prompt_text": "请写下反思",
        }]

    deliverable_response = client.get("/export/deliverables", headers=headers)
    with _zip(deliverable_response) as archive:
        path = next(name for name in archive.namelist() if name.endswith("/deliverable.md"))
        assert "/questionnaires/" not in path
        markdown = archive.read(path).decode("utf-8")
        assert markdown.startswith("---\nsession_id:")
        assert "submitted_at:" in markdown
        assert "# 最终方案\n\n- 保留学生原文" in markdown


def test_questionnaire_export_aggregates_session_and_preserves_raw_answers(
    db_and_app, teacher_login
):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = seed_running_session(db, session_no=97, member_count=2)
    second_group_id = create_group(db, name="Second Group", code="G98")
    second_student_id, _login_key = create_student(db, second_group_id, index=1)
    missing_student_id, _missing_login_key = create_student(db, second_group_id, index=2)
    missing_participant_code = db.query_one(
        "SELECT participant_code FROM users WHERE id=?", (missing_student_id,)
    )["participant_code"]

    questionnaire_id = db.create_questionnaire(
        {
            "code": "mixed_pre_v1",
            "title": "Mixed questionnaire",
            "timing": "pre",
            "active": True,
        },
        items=[
            {
                "item_code": "LIKERT",
                "prompt_text": "量表题",
                "dimension_label": "维度一",
                "question_type": "likert_7",
                "sort_order": 1,
            },
            {
                "item_code": "SINGLE",
                "prompt_text": "选择题",
                "dimension_label": "维度二",
                "question_type": "single_choice",
                "options": [{"key": "D", "label": "说明文本不应导出"}],
                "sort_order": 2,
            },
            {
                "item_code": "TEXT",
                "prompt_text": "文本题",
                "dimension_label": "维度三",
                "question_type": "text",
                "sort_order": 3,
            },
        ],
    )
    item_ids = {
        row["item_code"]: row["id"]
        for row in db.query_all(
            "SELECT id, item_code FROM questionnaire_items WHERE questionnaire_id=?",
            (questionnaire_id,),
        )
    }
    response_values = {
        "LIKERT": 6,
        "SINGLE": {"option_key": "D"},
        "TEXT": {"text": "原文,含逗号\n保留 emoji 😊"},
    }
    first_group_student_ids = [student_id for student_id, _key in scope["students"]]
    first_group_participant_codes = [
        db.query_one("SELECT participant_code FROM users WHERE id=?", (student_id,))["participant_code"]
        for student_id in first_group_student_ids
    ]
    student_ids = first_group_student_ids + [second_student_id]
    for student_id in student_ids:
        group_id = (
            scope["group_id"]
            if student_id in first_group_student_ids
            else second_group_id
        )
        response_payload = {
            str(item_ids[code]): value for code, value in response_values.items()
        }
        if student_id == first_group_student_ids[0]:
            response_payload.pop(str(item_ids["TEXT"]))
        db.create_questionnaire_submission(
            questionnaire_id,
            student_id,
            group_id,
            scope["session_id"],
            scope["session_no"],
            "pre",
            response_payload,
        )

    response = client.get("/export/questionnaires", headers=headers)
    assert response.status_code == 200
    assert response.headers["X-Export-Structure"] == "sessions/session/questionnaires/file"
    with _zip(response) as archive:
        csv_paths = [name for name in archive.namelist() if name.endswith(".csv")]
        item_path = next(name for name in csv_paths if name.endswith("/questionnaire_items.csv"))
        assert len(csv_paths) == 2
        path = next(name for name in csv_paths if name.endswith("/questionnaires/mixed_pre_v1.csv"))
        assert path.endswith("/questionnaires/mixed_pre_v1.csv")
        assert path.count("/questionnaires/") == 1
        rows = _csv_rows(archive, path)
        item_rows = _csv_rows(archive, item_path)

    assert list(rows[0]) == [
        "session_id", "group_id", "participant_code", "LIKERT", "SINGLE", "TEXT",
    ]
    assert len(rows) == len(student_ids)
    assert {row["group_id"] for row in rows} == {
        str(scope["group_id"]), str(second_group_id)
    }
    assert len({row["participant_code"] for row in rows}) == len(student_ids)
    assert missing_participant_code not in {row["participant_code"] for row in rows}
    actual_participant_order = [
        (int(row["group_id"]), row["participant_code"]) for row in rows
    ]
    assert actual_participant_order == sorted(actual_participant_order)
    by_participant = {row["participant_code"]: row for row in rows}
    first_row = by_participant[first_group_participant_codes[0]]
    assert first_row["LIKERT"] == "6"
    assert first_row["SINGLE"] == "D"
    assert first_row["TEXT"] == ""
    second_group_row = by_participant[
        next(row["participant_code"] for row in rows if row["group_id"] == str(second_group_id))
    ]
    assert second_group_row["TEXT"] == "原文,含逗号\n保留 emoji 😊"
    assert item_rows == [
        {
            "questionnaire_code": "mixed_pre_v1",
            "item_code": "LIKERT",
            "dimension_label": "维度一",
            "prompt_text": "量表题",
        },
        {
            "questionnaire_code": "mixed_pre_v1",
            "item_code": "SINGLE",
            "dimension_label": "维度二",
            "prompt_text": "选择题",
        },
        {
            "questionnaire_code": "mixed_pre_v1",
            "item_code": "TEXT",
            "dimension_label": "维度三",
            "prompt_text": "文本题",
        },
    ]
    assert len({(row["questionnaire_code"], row["item_code"]) for row in item_rows}) == len(item_rows)


def test_questionnaire_export_preserves_definition_order_and_rejects_duplicate_item_codes(
    db_and_app, teacher_login
):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = seed_running_session(db, session_no=98, member_count=1)
    questionnaire_id = db.create_questionnaire(
        {
            "code": "ordered_pre_v1",
            "title": "Ordered questionnaire",
            "timing": "pre",
            "active": True,
        },
        items=[
            {"item_code": "ITEM1", "prompt_text": "one", "sort_order": 1},
            {"item_code": "ITEM10", "prompt_text": "ten", "sort_order": 2},
            {"item_code": "ITEM2", "prompt_text": "two", "sort_order": 3},
        ],
    )
    item_rows = db.query_all(
        "SELECT id, item_code FROM questionnaire_items WHERE questionnaire_id=? ORDER BY sort_order, id",
        (questionnaire_id,),
    )
    db.create_questionnaire_submission(
        questionnaire_id,
        scope["students"][0][0],
        scope["group_id"],
        scope["session_id"],
        scope["session_no"],
        "pre",
        {str(row["id"]): index for index, row in enumerate(item_rows, start=1)},
    )

    response = client.get("/export/questionnaires", headers=headers)
    with _zip(response) as archive:
        path = next(name for name in archive.namelist() if name.endswith("ordered_pre_v1.csv"))
        rows = _csv_rows(archive, path)
    assert list(rows[0]) == [
        "session_id", "group_id", "participant_code", "ITEM1", "ITEM10", "ITEM2",
    ]

    duplicate_questionnaire_id = db.create_questionnaire(
        {
            "code": "duplicate_pre_v1",
            "title": "Duplicate questionnaire",
            "timing": "pre",
            "active": True,
        },
        items=[
            {"item_code": "DUP", "prompt_text": "first", "sort_order": 1},
            {"item_code": "DUP", "prompt_text": "second", "sort_order": 2},
        ],
    )
    duplicate_items = db.query_all(
        "SELECT id FROM questionnaire_items WHERE questionnaire_id=? ORDER BY sort_order, id",
        (duplicate_questionnaire_id,),
    )
    db.create_questionnaire_submission(
        duplicate_questionnaire_id,
        scope["students"][0][0],
        scope["group_id"],
        scope["session_id"],
        scope["session_no"],
        "pre",
        {str(row["id"]): index for index, row in enumerate(duplicate_items, start=1)},
    )

    response = client.get("/export/questionnaires", headers=headers)
    with _zip(response) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert not any(name.endswith("duplicate_pre_v1.csv") for name in names)
        item_path = next(name for name in names if name.endswith("/questionnaire_items.csv"))
        duplicate_item_rows = _csv_rows(archive, item_path)
    duplicate_metadata = [
        row for row in duplicate_item_rows
        if row["questionnaire_code"] == "duplicate_pre_v1"
    ]
    assert len(duplicate_metadata) == 1
    assert manifest["questionnaire_validation"]["duplicate_item_code_count"] == 1
    assert any("duplicate item_code" in warning for warning in manifest["warnings"])


def test_export_page_and_legacy_route_policy(db_and_app, teacher_login):
    _db, _app, client = db_and_app
    _client, headers = teacher_login

    page = client.get("/teacher/export", headers=headers, follow_redirects=True)
    html = page.get_data(as_text=True)
    assert "/export/state-assessments" in html
    assert "/export/questionnaires" in html
    assert "全部课次和全部小组" in html
    assert "eBlind" not in html
    assert "eSessionId" not in html
    assert "/export/unified-events.csv" not in html
    assert "/export/audit_logs.csv" not in html

    removed = client.get("/export/unified-events.csv", headers=headers)
    assert removed.status_code == 404
    retained = client.get("/export/messages.csv", headers=headers, follow_redirects=False)
    assert retained.status_code == 308
    assert retained.headers["Location"].endswith("/export/messages")
