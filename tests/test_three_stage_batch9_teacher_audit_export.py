# -*- coding: utf-8 -*-
"""Batch 9 coverage for teacher-side three-stage audit and exports."""

from __future__ import annotations

import csv
import importlib
import io
import json
import zipfile

from tests.helpers import create_group, create_student, seed_running_session


def _insert_discussion(db, scope):
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    return discussion_id


def _add_message(db, scope, sequence, content, *, role="student", strategy_id=None):
    user_id = scope["student_id"]
    if role == "agent":
        user_id = db.get_sera_user_id()
    return db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_id, session_no, task_id, discussion_id, created_at,
            strategy_id, agent_type, trigger_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            user_id,
            content,
            sequence,
            role,
            role,
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            db.now_str(),
            strategy_id,
            "strategy" if role == "agent" else None,
            "three_stage" if role == "agent" else None,
        ),
    )


def _insert_full_pipeline(db, scope, *, run_uuid="batch9-full", selected_strategy_id="ER-001"):
    for sequence, content in (
        (1, "我们先看证据。"),
        (2, "你总是否定别人，这样没法讨论。"),
        (3, "我担心比较条件还没有说清楚。"),
    ):
        _add_message(db, scope, sequence, content)
    message_id = _add_message(
        db,
        scope,
        4,
        "先把分歧回到证据上，每人说一个最担心的条件。",
        role="agent",
        strategy_id=selected_strategy_id,
    )
    now = db.now_str()
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_message_id, trigger_priority,
            input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
            stage1_status, stage1_started_at, stage1_completed_at,
            coarse_decision, coarse_state_code, coarse_risk_group,
            coarse_confidence, coarse_rule_scores_json,
            coarse_quantitative_features_json, coarse_evidence_message_ids_json,
            coarse_reason_codes_json,
            stage2_status, stage2_started_at, stage2_completed_at,
            raw_sub_state_code, canonical_sub_state_code,
            secondary_sub_state_tags_json, sub_state_confidence, sub_state_reason,
            sub_state_start_sequence, sub_state_end_sequence,
            sub_state_evidence_message_ids_json, all_state_segments_json,
            detected_self_regulation, should_intervene,
            stage3_status, stage3_started_at, stage3_completed_at,
            strategy_candidate_ids_json, selected_strategy_id,
            selected_strategy_name, selected_strategy_type,
            supporting_strategy_ids_json, strategy_selection_reason,
            strategy_library_version, strategy_library_hash,
            generated_intervention_text, validated_intervention_text,
            text_validation_result_json,
            publish_status, published_message_id, published_at,
            observation_status, observation_result,
            observation_previous_sub_state_code, observation_current_sub_state_code,
            observation_details_json,
            final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_uuid,
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "student_message",
            3,
            2,
            1,
            3,
            3,
            "SUCCEEDED",
            now,
            now,
            "ESCALATE",
            "POSSIBLE_CONFLICT",
            "HIGH",
            0.88,
            json.dumps({"conflict": 0.88}, ensure_ascii=False),
            json.dumps({"candidate_sub_states": ["interpersonal_conflict"]}, ensure_ascii=False),
            "[2,3]",
            json.dumps(["CONFLICT_TERMS"], ensure_ascii=False),
            "SUCCEEDED",
            now,
            now,
            "人际性冲突",
            "interpersonal_conflict",
            json.dumps(["psychological_safety_risk"], ensure_ascii=False),
            0.91,
            "分歧转向成员态度评价",
            2,
            3,
            "[2,3]",
            json.dumps(
                [{"canonical_sub_state": "interpersonal_conflict", "evidence_message_ids": [2, 3]}],
                ensure_ascii=False,
            ),
            0,
            1,
            "SUCCEEDED",
            now,
            now,
            json.dumps(["ER-001", "SS-004"], ensure_ascii=False),
            selected_strategy_id,
            "冲突认知重评",
            "情绪调节",
            json.dumps(["SS-004"], ensure_ascii=False),
            "先维护讨论安全，再回到证据条件。",
            "v2.2",
            "batch9-library-hash",
            "先把分歧回到证据上，每人说一个最担心的条件。",
            "先把分歧回到证据上，每人说一个最担心的条件。",
            json.dumps({"passed": True}, ensure_ascii=False),
            "PUBLISHED",
            message_id,
            now,
            "completed",
            "recovered",
            "interpersonal_conflict",
            "constructive_conflict",
            json.dumps({"first_response_seconds": 18}, ensure_ascii=False),
            "PUBLISHED",
            now,
            now,
        ),
    )


def _insert_stage1_only_pipeline(db, scope):
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence, stage1_status, coarse_state_code,
            coarse_rule_scores_json, coarse_quantitative_features_json,
            coarse_evidence_message_ids_json, coarse_reason_codes_json,
            publish_status, final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "batch9-stage1-only",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "student_message",
            1,
            1,
            1,
            "SUCCEEDED",
            "POSSIBLE_POSITIVE",
            "{}",
            "{}",
            "[]",
            "[]",
            "SKIPPED",
            "SUPPRESSED",
            db.now_str(),
            db.now_str(),
        ),
    )


def _batch9_scope(db):
    scope = seed_running_session(db, session_no=9909, member_count=1)
    _insert_discussion(db, scope)
    return scope


def _csv_rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))))


def test_research_export_contract_excludes_large_internal_audit_fields():
    exports = importlib.import_module("services.research_export_service")
    assert "llm_context_json" not in exports.STRATEGY_PIPELINE_EXPORT_COLUMNS
    assert "coarse_quantitative_features_json" not in exports.STRATEGY_PIPELINE_EXPORT_COLUMNS
    assert "generated_intervention_text" not in exports.STRATEGY_PIPELINE_EXPORT_COLUMNS


def test_agent_audit_includes_three_stage_precise_state_and_history_note(db_and_app):
    db, _app_module, _client = db_and_app
    scope = _batch9_scope(db)
    pipeline_id = _insert_full_pipeline(db, scope)
    _insert_stage1_only_pipeline(db, scope)

    other_group = create_group(db, name="Other batch9 group", code="B9-OTHER")
    other_student, _key = create_student(db, other_group)
    other_scope = dict(scope, group_id=other_group, student_id=other_student, students=[(other_student, _key)])
    _insert_discussion(db, other_scope)
    _insert_full_pipeline(db, other_scope, run_uuid="batch9-other")

    service = importlib.import_module("services.teacher_agent_audit_service")
    result = service.get_agent_audit(scope["group_id"], scope["session_id"], blinded=False)

    runs = result["strategy_pipeline_runs"]
    assert [r["pipeline_run_id"] for r in runs] == [pipeline_id, pipeline_id + 1]
    published = runs[0]
    assert published["canonical_sub_state_code"] == "interpersonal_conflict"
    assert published["raw_sub_state_code"] == "人际性冲突"
    assert published["selected_strategy_id"] == "ER-001"
    assert published["strategy_library_version"] == "v2.2"
    assert published["evidence_message_ids"] == [2, 3]
    assert published["evidence_messages"][0]["content"] == "你总是否定别人，这样没法讨论。"
    assert published["observation_result"] == "recovered"
    assert result["stats"]["three_stage_pipeline_count"] == 2
    assert result["stats"]["three_stage_published_count"] == 1

    legacy_like = runs[1]
    assert legacy_like["canonical_sub_state_code"] == "历史数据未记录"
    assert legacy_like["selected_strategy_id"] == "历史数据未记录"


def test_strategy_pipeline_export_preserves_required_fields_scope_and_blind_mode(db_and_app):
    db, _app_module, _client = db_and_app
    scope = _batch9_scope(db)
    pipeline_id = _insert_full_pipeline(db, scope)

    other_group = create_group(db, name="Other export group", code="B9-EXPORT-OTHER")
    other_student, _key = create_student(db, other_group)
    other_scope = dict(scope, group_id=other_group, student_id=other_student, students=[(other_student, _key)])
    _insert_discussion(db, other_scope)
    _insert_full_pipeline(db, other_scope, run_uuid="batch9-export-other")

    exports = importlib.import_module("services.teacher_export_service")
    csv_text = exports.export_strategy_pipeline_runs_csv(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        blind=False,
    )
    rows = _csv_rows(csv_text)
    assert len(rows) == 1
    row = rows[0]
    for field in (
        "coarse_state_code",
        "canonical_sub_state_code",
        "raw_sub_state_code",
        "selected_strategy_id",
        "strategy_library_version",
        "evidence_message_ids",
        "pipeline_run_id",
        "publish_status",
        "skip_reason",
    ):
        assert field in row
    assert row["pipeline_run_id"] == str(pipeline_id)
    assert row["canonical_sub_state_code"] == "interpersonal_conflict"
    assert row["raw_sub_state_code"] == "人际性冲突"
    assert row["selected_strategy_id"] == "ER-001"
    assert row["strategy_library_version"] == "v2.2"
    assert row["evidence_message_ids"] == "[2,3]"
    assert row["publish_status"] == "PUBLISHED"
    assert row["skip_reason"] == "历史数据未记录"

    blind_rows = _csv_rows(
        exports.export_strategy_pipeline_runs_csv(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            blind=True,
        )
    )
    assert blind_rows[0]["selected_strategy_id"] == "ER-001"
    assert blind_rows[0]["canonical_sub_state_code"] == "interpersonal_conflict"
    assert blind_rows[0]["generated_intervention_text"] == "[BLINDED AGENT MESSAGE]"
    assert blind_rows[0]["group_condition"] == ""


def test_export_endpoint_includes_strategy_pipeline_csv_in_structured_zip(db_and_app, teacher_login):
    db, _app_module, client = db_and_app
    client, headers = teacher_login
    scope = _batch9_scope(db)
    _insert_full_pipeline(db, scope)

    response = client.get(
        "/export/strategy-pipeline",
        headers=headers,
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        csv_name = next(name for name in names if name.endswith("strategy_pipeline.csv"))
        exported_rows = _csv_rows(zf.read(csv_name).decode("utf-8-sig"))
    assert exported_rows[0]["state_code"] == "interpersonal_conflict"
    assert "generated_intervention_text" not in exported_rows[0]


def test_teacher_audit_frontend_exposes_three_stage_contract():
    js = open("static/teacher/agent-audit.js", encoding="utf-8").read()
    template = open("templates/teacher/audit.html", encoding="utf-8").read()
    export_page = open("templates/teacher/export.html", encoding="utf-8").read()

    assert "strategy_pipeline_runs" in js
    assert "renderStrategyPipelineDetail" in js
    assert "canonical_sub_state_code" in js
    assert "interpersonal_conflict" in template
    assert "/export/strategy-pipeline" in export_page
