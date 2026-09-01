# -*- coding: utf-8 -*-
"""Batch 5 contract tests for canonical research exports."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timedelta

from services.three_stage_schema import FINAL_SUB_STATE_CODES
from tests.helpers import seed_running_session


def _stamp(value):
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _csv_rows(text):
    return list(csv.DictReader(io.StringIO((text or "").lstrip("\ufeff"))))


def _seed_batch5_scope(db):
    scope = seed_running_session(
        db,
        session_no=850,
        member_count=1,
        limit_minutes=30,
    )
    now = datetime.now().replace(microsecond=0)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, deadline,
            created_at, updated_at
        ) VALUES(?,?,'running',?,?,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            _stamp(now - timedelta(minutes=10)),
            _stamp(now + timedelta(minutes=20)),
            _stamp(now - timedelta(minutes=10)),
            _stamp(now),
        ),
    )
    scope["discussion_id"] = discussion_id
    student_id = scope["students"][0][0]
    for sequence, content in (
        (1, "我先整理约束条件。"),
        (2, "然后把方案逐项写出来。"),
        (3, "你总是否定别人，这样没法讨论。"),
        (4, "这里的证据还没有完成判断。"),
    ):
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_no, task_id, session_id, discussion_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                student_id,
                content,
                sequence,
                "student",
                "student",
                scope["session_no"],
                scope["task_id"],
                scope["session_id"],
                discussion_id,
                _stamp(now - timedelta(minutes=8) + timedelta(seconds=sequence)),
            ),
        )
    agent_message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, discussion_id, created_at,
            strategy_id, agent_type, trigger_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            db.get_sera_user_id(),
            "请先把分歧回到证据上。",
            5,
            "agent",
            "agent",
            scope["session_no"],
            scope["task_id"],
            scope["session_id"],
            discussion_id,
            _stamp(now - timedelta(minutes=7)),
            "ER-001",
            "strategy",
            "three_stage",
        ),
    )
    scope["agent_message_id"] = agent_message_id

    def insert_batch(start, end, status, terminal_status, error_code=None):
        return db.execute(
            """
            INSERT INTO state_assessment_batches(
                group_id, session_id, session_no, task_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence,
                trigger_type, window_key, status, terminal_status,
                error_code, fallback_action, fallback_segment_count,
                completed_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,'message_batch',?,?,?,?,?,?,?, ?,?)
            """,
            (
                scope["group_id"],
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                discussion_id,
                start,
                end,
                "batch5-%s-%s-%s" % (scope["session_id"], start, end),
                status,
                terminal_status,
                error_code,
                "unclassified" if status == "failed" else None,
                1 if status == "failed" else 0,
                _stamp(now - timedelta(minutes=6)),
                _stamp(now - timedelta(minutes=7)),
                _stamp(now - timedelta(minutes=6)),
            ),
        )

    batch_execution = insert_batch(1, 2, "succeeded", "completed")
    batch_conflict = insert_batch(3, 3, "succeeded", "completed")
    insert_batch(4, 4, "failed", "quarantined", "schema_validation_error")

    pipeline_execution = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            input_start_sequence, input_end_sequence,
            stage1_status, coarse_state_code, stage2_status,
            raw_sub_state_code, canonical_sub_state_code,
            secondary_sub_state_tags_json, sub_state_start_sequence,
            sub_state_end_sequence, sub_state_evidence_message_ids_json,
            should_intervene, inhibition_strategy_id, stage3_status,
            publish_status, final_status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'SKIPPED',
                 'SKIPPED','SUPPRESSED',?,?)
        """,
        (
            "batch5-execution-%s" % scope["session_id"],
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            discussion_id,
            scope["task_id"],
            1,
            2,
            "SUCCEEDED",
            "positive_collaboration",
            "SUCCEEDED",
            "执行推进",
            "execution_progress",
            json.dumps(["stage_achievement"], ensure_ascii=False),
            1,
            2,
            "[1,2]",
            0,
            "OI-004",
            _stamp(now - timedelta(minutes=6)),
            _stamp(now - timedelta(minutes=6)),
        ),
    )
    pipeline_conflict = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            input_start_sequence, input_end_sequence,
            stage1_status, coarse_state_code, stage2_status,
            raw_sub_state_code, canonical_sub_state_code,
            secondary_sub_state_tags_json, sub_state_start_sequence,
            sub_state_end_sequence, sub_state_evidence_message_ids_json,
            should_intervene, stage3_status, selected_strategy_id,
            strategy_library_version, publish_status, final_status,
            published_message_id, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'SUCCEEDED','ER-001',
                 'v2.2','SELECTED','SELECTED',?,?,?)
        """,
        (
            "batch5-conflict-%s" % scope["session_id"],
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            discussion_id,
            scope["task_id"],
            3,
            3,
            "SUCCEEDED",
            "conflict_tension",
            "SUCCEEDED",
            "人际性冲突",
            "interpersonal_conflict",
            json.dumps(["psychological_safety_risk"], ensure_ascii=False),
            3,
            3,
            "[3]",
            agent_message_id,
            _stamp(now - timedelta(minutes=6)),
            _stamp(now - timedelta(minutes=6)),
        ),
    )

    def insert_segment(
        start,
        end,
        coarse_code,
        final_code,
        batch_id,
        pipeline_id,
        selected_strategy_id=None,
    ):
        return db.execute(
            """
            INSERT INTO collaboration_state_segments(
                group_id, session_id, session_no, task_id, discussion_id,
                state_code, coarse_state_code, raw_sub_state_code,
                canonical_sub_state_code, secondary_tags_json,
                segment_kind, start_message_id, end_message_id,
                start_sequence, end_sequence, assessment_batch_id,
                evidence_message_ids_json, evidence_sequences,
                confidence, fallback_reason, source, assessment_status,
                is_finalized, dedupe_key, strategy_pipeline_run_id,
                should_intervene, selected_strategy_id, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'message_range',?,?,?,?,?,?,?,
                     0.91,NULL,'llm','confirmed',1,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                discussion_id,
                coarse_code,
                coarse_code,
                final_code,
                final_code,
                "[]",
                start,
                end,
                start,
                end,
                batch_id,
                json.dumps(list(range(start, end + 1))),
                json.dumps(list(range(start, end + 1))),
                "batch5-segment-%s-%s-%s" % (
                    scope["session_id"],
                    start,
                    end,
                ),
                pipeline_id,
                0 if str(selected_strategy_id or "").startswith("OI-") else (
                    1 if selected_strategy_id else 0
                ),
                selected_strategy_id,
                _stamp(now - timedelta(minutes=5)),
                _stamp(now - timedelta(minutes=5)),
            ),
        )

    insert_segment(
        1,
        2,
        "positive_collaboration",
        "execution_progress",
        batch_execution,
        pipeline_execution,
    )
    insert_segment(
        3,
        3,
        "conflict_tension",
        "interpersonal_conflict",
        batch_conflict,
        pipeline_conflict,
        "ER-001",
    )
    db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, coarse_state_code, segment_kind, start_at, end_at,
            is_active, gap_seconds, source, assessment_status, is_finalized,
            dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,'negative_silence','negative_silence',
                 'time_range',?,?,0,90,'silence_rule','confirmed',1,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            _stamp(now - timedelta(minutes=4)),
            _stamp(now - timedelta(minutes=3)),
            "batch5-silence-%s" % scope["session_id"],
            _stamp(now - timedelta(minutes=4)),
            _stamp(now - timedelta(minutes=3)),
        ),
    )
    db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, rule_state_code, llm_state_code, fused_state_code,
            assessment_status, confidence, created_at
        ) VALUES(?,?,?,?,?,'task_detached','task_detached',
                 'task_detached','task_detached','confirmed',0.77,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            _stamp(now - timedelta(minutes=5)),
        ),
    )
    intervention_run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, session_no, task_id, discussion_id,
            cutoff_sequence, status, strategy_pipeline_run_id,
            canonical_sub_state_code, selected_strategy_id,
            publish_status, validated_text, message_id, created_at
        ) VALUES(?,?,?,?,?,3,'PUBLISHED',?,'interpersonal_conflict',
                 'ER-001','PUBLISHED','请先把分歧回到证据上。',?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            pipeline_conflict,
            agent_message_id,
            _stamp(now - timedelta(minutes=6)),
        ),
    )
    db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, push_mode, trigger_source,
            title, message, strategy_id, strategy_type,
            session_id, task_id, created_at
        ) VALUES(?,?,'three_stage','student_message','冲突调节',
                 '请先把分歧回到证据上。','ER-001','active_intervention',?,?,?)
        """,
        (
            scope["group_id"],
            intervention_run_id,
            scope["session_id"],
            scope["task_id"],
            _stamp(now - timedelta(minutes=6)),
        ),
    )
    return scope


def test_messages_export_uses_canonical_assignment_and_preserves_strategy_ids(
    db_and_app,
):
    db, _app, _client = db_and_app
    scope = _seed_batch5_scope(db)
    seed_running_session(db, session_no=851, member_count=1, limit_minutes=30)

    from services.teacher_export_service import (
        export_interventions_csv,
        export_messages_csv,
        export_strategy_pipeline_runs_csv,
    )

    rows = _csv_rows(
        export_messages_csv(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
        )
    )
    assert [row["sequence"] for row in rows] == ["1", "2", "3", "4", "5"]
    by_sequence = {int(row["sequence"]): row for row in rows}
    assert by_sequence[1]["final_sub_state_code"] == "execution_progress"
    assert by_sequence[1]["inhibition_strategy_id"] == "OI-004"
    assert by_sequence[1]["selected_strategy_id"] == ""
    assert by_sequence[3]["final_sub_state_code"] == "interpersonal_conflict"
    assert by_sequence[3]["selected_strategy_id"] == "ER-001"
    assert by_sequence[4]["final_sub_state_code"] == ""
    assert by_sequence[4]["assessment_status"] == "unclassified"
    assert by_sequence[4]["assignment_source"] == "batch_unclassified"
    assert by_sequence[4]["error_code"] == "schema_validation_error"
    assert by_sequence[5]["role"] == "agent"
    assert by_sequence[5]["final_sub_state_code"] == ""
    assert by_sequence[5]["context_sub_state_code"] == "interpersonal_conflict"
    assert {
        row["final_sub_state_code"]
        for row in rows
        if row["final_sub_state_code"]
    } <= set(FINAL_SUB_STATE_CODES)
    assert all(row["group_id"] == str(scope["group_id"]) for row in rows)
    assert all(row["session_id"] == str(scope["session_id"]) for row in rows)

    interventions = _csv_rows(
        export_interventions_csv(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
        )
    )
    assert interventions[0]["final_sub_state_code"] == "interpersonal_conflict"
    assert interventions[0]["canonical_sub_state_code"] == "interpersonal_conflict"
    assert interventions[0]["selected_strategy_id"] == "ER-001"
    assert interventions[0]["inhibition_strategy_id"] == ""
    assert interventions[0]["status"] == "PUBLISHED"
    assert interventions[0]["publish_status"] == "PUBLISHED"
    assert interventions[0]["message_id"] == by_sequence[5]["id"]
    assert interventions[0]["discussion_id"]
    assert interventions[0]["assessment_status"] == "confirmed"
    assert interventions[0]["assignment_source"] == "strategy_pipeline"

    pipeline_rows = _csv_rows(
        export_strategy_pipeline_runs_csv(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
        )
    )
    by_state = {
        row["canonical_sub_state_code"]: row for row in pipeline_rows
    }
    assert by_state["execution_progress"]["inhibition_strategy_id"] == "OI-004"
    assert "stage_achievement" in by_state["execution_progress"]["state_overlays"]
    assert by_state["interpersonal_conflict"]["selected_strategy_id"] == "ER-001"
    assert "psychological_safety_risk" in (
        by_state["interpersonal_conflict"]["state_overlays"]
    )


def test_detector_and_unified_exports_separate_coarse_assignments_and_silence(
    db_and_app,
):
    db, _app, _client = db_and_app
    scope = _seed_batch5_scope(db)

    from services.teacher_export_service import (
        export_detector_outputs_csv,
        export_unified_events_csv,
    )

    detector = _csv_rows(
        export_detector_outputs_csv(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
        )
    )[0]
    assert detector["coarse_state_code"] == "task_detached"
    assert detector["rule_state_code"] == "task_detached"
    assert detector["llm_coarse_state_code"] == "task_detached"
    assert detector["fused_coarse_state_code"] == "task_detached"
    assert "final_state_code" not in detector
    assert detector["final_state_code_legacy"] == "task_detached"

    events = _csv_rows(
        export_unified_events_csv(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
        )
    )
    assignment_events = [
        row for row in events if row["event_type"] == "state_assignment"
    ]
    assert len(assignment_events) == 4
    assert {
        row["assessment_status"] for row in assignment_events
    } == {"confirmed", "unclassified"}
    assert {
        row["final_sub_state_code"]
        for row in assignment_events
        if row["final_sub_state_code"]
    } == {"execution_progress", "interpersonal_conflict"}
    detector_events = [
        row
        for row in events
        if row["event_type"] == "detector_coarse_assessment"
    ]
    assert detector_events[0]["final_sub_state_code"] == ""
    assert detector_events[0]["coarse_state_code"] == "task_detached"
    silence = [
        row for row in events if row["event_type"] == "silence_time_range"
    ]
    assert len(silence) == 1
    assert silence[0]["related_table"] == "collaboration_state_segments"
    assert silence[0]["final_sub_state_code"] == ""
    assert silence[0]["start_at"]
    assert silence[0]["end_at"]


def test_structured_zip_keeps_v2_schema_and_utf8_labels(
    db_and_app,
    teacher_login,
):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    scope = _seed_batch5_scope(db)

    response = client.get(
        "/export/all",
        query_string={
            "group_id": scope["group_id"],
            "session_id": scope["session_id"],
        },
        headers=headers,
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["package_format_version"] == "1.0"
        assert manifest["export_mode"] == "full_nonblinded"
        message_name = next(
            name
            for name in archive.namelist()
            if name.endswith("messages.csv")
        )
        message_rows = _csv_rows(
            archive.read(message_name).decode("utf-8-sig")
        )
        assert list(message_rows[0]) == [
            "session_id", "group_id", "message_id", "sequence", "sender_role",
            "participant_code", "agent_type", "agent_event_id", "agent_reference_code",
            "state_code", "state_assignment_source", "selected_strategy_id", "content",
            "reply_to_message_id", "created_at",
        ]
        assert "final_sub_state_label" not in message_rows[0]
