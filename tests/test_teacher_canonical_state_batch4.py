# -*- coding: utf-8 -*-
"""Batch 4 contract tests for canonical teacher-facing state surfaces."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from services.three_stage_schema import FINAL_SUB_STATE_CODES
from tests.helpers import seed_running_session


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _seed_teacher_scope(db):
    scope = seed_running_session(
        db,
        session_no=840,
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
            _stamp(now - timedelta(minutes=5)),
            _stamp(now + timedelta(minutes=25)),
            _stamp(now - timedelta(minutes=5)),
            _stamp(now),
        ),
    )
    scope["discussion_id"] = discussion_id
    student_id = scope["students"][0][0]
    for sequence in (1, 2):
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
                "canonical batch4 message %s" % sequence,
                sequence,
                "student",
                "student",
                scope["session_no"],
                scope["task_id"],
                scope["session_id"],
                discussion_id,
                _stamp(now - timedelta(minutes=4) + timedelta(seconds=sequence)),
            ),
        )
    db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, raw_sub_state_code, canonical_sub_state_code,
            coarse_state_code, segment_kind,
            start_message_id, end_message_id, start_sequence, end_sequence,
            evidence_message_ids_json, evidence_sequences,
            confidence, source, assessment_status, is_finalized, dedupe_key,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'message_range',?,?,?,?,?,?,
                 0.91,'session_finalizer','confirmed',1,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            discussion_id,
            "positive_collaboration",
            "execution_progress",
            "execution_progress",
            "positive_collaboration",
            1,
            2,
            1,
            2,
            json.dumps([1, 2]),
            json.dumps([1, 2]),
            "batch4-canonical-%s" % scope["session_id"],
            _stamp(now - timedelta(minutes=3)),
            _stamp(now - timedelta(minutes=3)),
        ),
    )
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no, discussion_id,
            fused_state_code, fused_state_label, assessment_status,
            confidence, risk_level, risk_label, created_at
        ) VALUES(?,?,?,?,?,?,'任务脱离','confirmed',0.83,2,'medium',?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["task_id"],
            scope["session_no"],
            discussion_id,
            "task_detached",
            _stamp(now - timedelta(minutes=2)),
        ),
    )
    db.execute(
        """
        INSERT INTO group_states(
            group_id, state_code, state_label, risk_level, risk_label,
            evidence, task_id, session_no, session_id, discussion_id,
            state_score, state_assessment_id, assessment_status, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            "task_detached",
            "任务脱离",
            2,
            "medium",
            "coarse audit evidence",
            scope["task_id"],
            scope["session_no"],
            scope["session_id"],
            discussion_id,
            0.83,
            assessment_id,
            "confirmed",
            _stamp(now - timedelta(minutes=2)),
        ),
    )
    return scope


def test_trend_main_contract_uses_only_primary_and_process_states(db_and_app):
    db, _app, _client = db_and_app
    scope = _seed_teacher_scope(db)

    from services.teacher_emotion_trend_service import get_emotion_trend

    trend = get_emotion_trend(
        scope["group_id"],
        session_id=scope["session_id"],
        start_time="2000-01-01 00:00:00",
        end_time="2099-01-01 00:00:00",
    )
    expected = list(FINAL_SUB_STATE_CODES) + ["observing", "unclassified"]
    assert [item["code"] for item in trend["state_system"]] == expected
    assert set(trend["distribution"]) == set(expected)
    assert trend["distribution"]["execution_progress"]["message_count"] == 2
    assert "positive_collaboration" not in trend["distribution"]
    assert (
        trend["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 2
    )
    assert trend["coarse_state_debug_only"] is True


def test_group_list_and_detail_use_canonical_current_state(
    db_and_app,
    teacher_login,
):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    scope = _seed_teacher_scope(db)

    response = client.get(
        "/api/teacher/groups",
        query_string={"all": "1", "session_id": scope["session_id"]},
        headers=headers,
    )
    assert response.status_code == 200
    group = next(
        item
        for item in response.get_json()["groups"]
        if item["group_id"] == scope["group_id"]
    )
    assert group["final_sub_state_code"] == "execution_progress"
    assert group["state_code"] == "execution_progress"
    assert group["state_label"] == "执行推进"
    assert group["coarse_state_code"] == "task_detached"
    assert group["assignment_source"] == "session_finalizer_segment"

    detail_response = client.get(
        "/api/teacher/group/%s/detail" % scope["group_id"],
        query_string={"session_id": scope["session_id"]},
        headers=headers,
    )
    assert detail_response.status_code == 200
    detail = detail_response.get_json()
    assert "recent_assessments" not in detail
    assert detail["recent_coarse_assessments"][0]["fused_state_code"] == "task_detached"
    assert detail["current_state"]["final_sub_state_code"] == "execution_progress"
    assert {
        segment["final_sub_state_code"]
        for segment in detail["canonical_segments"]
    } == {"execution_progress"}
    assert all(
        segment["discussion_id"] == scope["discussion_id"]
        for segment in detail["canonical_segments"]
    )


def test_agent_audit_separates_three_stages_and_batch_failures(db_and_app):
    db, _app, _client = db_and_app
    scope = _seed_teacher_scope(db)
    now = db.now_str()
    db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            trigger_type, window_key, status, terminal_status,
            attempt_count, max_attempts, error_code, error_detail,
            fallback_action, fallback_segment_count, created_at, updated_at
        ) VALUES(?,?,?,?,?,'message_batch',?,'failed','quarantined',
                 2,2,'truncated_response','finish_reason=length',
                 'unclassified',1,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            1,
            2,
            "batch4-failed-%s" % scope["session_id"],
            now,
            now,
        ),
    )
    db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, discussion_id, task_id,
            coarse_state_code, stage1_status, stage2_status,
            raw_sub_state_code, canonical_sub_state_code,
            sub_state_evidence_message_ids_json,
            should_intervene, inhibition_strategy_id,
            stage3_status, selected_strategy_id, final_status,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,'POSSIBLE_POSITIVE','completed','completed',
                 '执行推进','execution_progress','[1,2]',
                 0,'OI-004','skipped',NULL,'SKIPPED',?,?)
        """,
        (
            "batch4-pipeline-%s" % scope["session_id"],
            scope["group_id"],
            scope["session_id"],
            scope["discussion_id"],
            scope["task_id"],
            now,
            now,
        ),
    )

    from services.teacher_agent_audit_service import get_agent_audit

    audit = get_agent_audit(
        scope["group_id"],
        scope["session_id"],
        blinded=False,
    )
    detector = audit["detector_outputs"][0]
    assert detector["coarse_state_code"] == "task_detached"
    assert detector["state_semantics"] == "coarse_stage1"
    assert "final_state_code" not in detector
    pipeline = audit["strategy_pipeline_runs"][0]
    assert pipeline["final_sub_state_code"] == "execution_progress"
    assert pipeline["coarse_state_code"] == "POSSIBLE_POSITIVE"
    assert pipeline["inhibition_strategy_id"] == "OI-004"
    assert pipeline["selected_strategy_id"] == "历史数据未记录"
    batch = audit["assessment_batches"][0]
    assert batch["assessment_status"] == "unclassified"
    assert batch["assignment_source"] == "batch_unclassified"
    assert batch["llm_error"]["error_code"] == "truncated_response"


def test_teacher_frontends_do_not_offer_legacy_states_as_main_filters():
    emotion_source = open(
        "static/teacher/emotion-trend.js",
        encoding="utf-8",
    ).read()
    audit_source = open(
        "static/teacher/agent-audit.js",
        encoding="utf-8",
    ).read()
    audit_template = open(
        "templates/teacher/audit.html",
        encoding="utf-8",
    ).read()

    emotion_order = emotion_source.split(
        "const PRIMARY_STATE_ORDER = [", 1
    )[1].split("];", 1)[0]
    audit_order = audit_source.split("const STATE_ORDER = [", 1)[1].split(
        "];", 1
    )[0]
    for code in FINAL_SUB_STATE_CODES:
        assert code in emotion_order
        assert code in audit_order
        assert ('value="%s"' % code) in audit_template
    for legacy_code in (
        "positive_collaboration",
        "negative_silence",
        "conflict_tension",
        "blocked_frustration",
        "task_detached",
    ):
        assert legacy_code not in emotion_order
        assert legacy_code not in audit_order
        assert ('value="%s"' % legacy_code) not in audit_template
    assert "renderAssessmentBatchDetail" in audit_source
    assert "消极沉默区间" in emotion_source
