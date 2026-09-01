# -*- coding: utf-8 -*-
"""B6 coverage for normalized state data exports."""

import csv
import io
import json
import zipfile
from datetime import datetime, timedelta

from tests.helpers import seed_running_session


BASE_TIME = datetime(2026, 7, 17, 12, 0, 0)


def _ts(offset_minutes):
    return (BASE_TIME + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _csv_rows(csv_content):
    text = (csv_content or "").lstrip("\ufeff")
    return list(csv.DictReader(io.StringIO(text)))


def _zip_csv_rows(zip_bytes, suffix):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        matches = [
            name for name in zf.namelist()
            if name.endswith("/" + suffix) or name == suffix
        ]
        assert matches, zf.namelist()
        with zf.open(matches[0]) as handle:
            content = handle.read().decode("utf-8-sig")
    return _csv_rows(content)


def _insert_b6_export_records(db, context):
    student_id = context["students"][0][0]
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, session_no, task_id,
            state_code, state_score,
            window_start, window_end,
            rule_result_json, llm_result_json, fusion_json,
            evidence, assessment_status, confidence,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["session_no"],
            context["task_id"],
            "coordination_disorder",
            0.77,
            _ts(0),
            _ts(3),
            json.dumps(
                {
                    "state_code": "coordination_disorder",
                    "evidence_tags": ["coordination_blocked", "no_next_step"],
                    "candidates": [
                        {"state_code": "blocked_frustration", "score": 0.77},
                    ],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "result": {
                        "state_code": "blocked_frustration",
                        "confidence": 0.73,
                        "evidence_tags": ["process_unclear"],
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "fused_state_code": "blocked_frustration",
                    "legacy_state_code": "coordination_disorder",
                    "normalization_reason": "legacy_coordination_disorder_normalized",
                    "evidence_tags": ["coordination_blocked", "no_next_step"],
                    "candidate_scores": {
                        "blocked_frustration": 0.77,
                        "positive_collaboration": 0.12,
                    },
                },
                ensure_ascii=False,
            ),
            "evidence_tags=coordination_blocked,no_next_step",
            "confirmed",
            0.8,
            _ts(1),
        ),
    )

    db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence,
            sender_type, role, session_no, task_id, session_id,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            student_id,
            "We are stuck on the next step.",
            1,
            "student",
            "student",
            context["session_no"],
            context["task_id"],
            context["session_id"],
            _ts(2),
        ),
    )

    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, status,
            detected_state, confidence, trigger_type,
            metadata_json, candidate_strategies, selected_strategy,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            2,
            "strategy",
            "PUBLISHED",
            "coordination_disorder",
            0.82,
            "auto_intervention",
            json.dumps(
                {
                    "evidence_tags": ["coordination_blocked"],
                    "candidate_scores": {"blocked_frustration": 0.82},
                    "validation": {
                        "triggerable_state_check": {
                            "state_code": "coordination_disorder",
                            "evidence_tags": ["no_next_step"],
                        }
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(["v2_blocked_role_clarity"], ensure_ascii=False),
            json.dumps({"id": "v2_blocked_role_clarity"}, ensure_ascii=False),
            _ts(2),
            _ts(2),
        ),
    )
    db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, push_mode, trigger_source,
            title, message, strategy_id, strategy_type,
            session_id, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            run_id,
            "sera_auto_v2",
            "auto_intervention",
            "Clarify next step",
            "Assign one recorder and list the next action.",
            "v2_blocked_role_clarity",
            "active_intervention",
            context["session_id"],
            context["task_id"],
            _ts(2),
        ),
    )
    return assessment_id, run_id


def test_b6_service_exports_normalized_state_fields(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=61, member_count=2, limit_minutes=20)
    assessment_id, _run_id = _insert_b6_export_records(db, context)

    from services.teacher_export_service import (
        export_detector_outputs_csv,
        export_interventions_csv,
        export_messages_csv,
        export_unified_events_csv,
    )

    detector = _csv_rows(export_detector_outputs_csv(session_id=context["session_id"]))
    assert detector[0]["id"] == str(assessment_id)
    assert detector[0]["state_code"] == "coordination_disorder"
    assert detector[0]["coarse_state_code"] == "blocked_frustration"
    assert "final_state_code" not in detector[0]
    assert detector[0]["final_state_code_legacy"] == "blocked_frustration"
    assert detector[0]["raw_state_code"] == "coordination_disorder"
    assert detector[0]["legacy_state_code"] == "coordination_disorder"
    assert "coordination_blocked" in detector[0]["evidence_tags"]
    assert "blocked_frustration" in detector[0]["candidate_scores_json"]

    messages = _csv_rows(export_messages_csv(session_id=context["session_id"]))
    assert messages[0]["assigned_state_assessment_id"] == ""
    assert messages[0]["final_sub_state_code"] == ""
    assert messages[0]["assigned_state_code"] == ""
    assert messages[0]["assessment_status"] == "observing"
    assert messages[0]["assignment_source"] == "awaiting_detection_conditions"

    interventions = _csv_rows(export_interventions_csv(session_id=context["session_id"]))
    assert interventions[0]["trigger_state_code"] == "blocked_frustration"
    assert interventions[0]["trigger_legacy_state_code"] == "coordination_disorder"
    assert "coordination_blocked" in interventions[0]["trigger_evidence_tags"]
    assert interventions[0]["final_sub_state_code"] == ""

    unified = _csv_rows(export_unified_events_csv(session_id=context["session_id"]))
    state_events = [row for row in unified if row["source_table"] == "state_assessments"]
    assert state_events
    assert state_events[0]["event_category"] == "detector_coarse"
    assert state_events[0]["event_type"] == "detector_coarse_assessment"
    assert state_events[0]["state_code"] == "blocked_frustration"
    assert state_events[0]["final_sub_state_code"] == ""
    assert state_events[0]["legacy_state_code"] == "coordination_disorder"
    assignment = next(
        row for row in unified if row["event_type"] == "state_assignment"
    )
    assert assignment["assessment_status"] == "observing"
    assert assignment["final_sub_state_code"] == ""


def test_b6_export_all_zip_contains_normalized_state_csvs(db_and_app, teacher_login):
    db, _app_module, _unused_client = db_and_app
    client, headers = teacher_login
    context = seed_running_session(db, session_no=62, member_count=2, limit_minutes=20)
    _insert_b6_export_records(db, context)

    response = client.get(
        "/export/all?session_id=%s" % context["session_id"],
        headers=headers,
    )
    assert response.status_code == 200
    assert "application/zip" in response.headers.get("Content-Type", "")

    detector = _zip_csv_rows(response.get_data(), "state_assessments.csv")
    messages = _zip_csv_rows(response.get_data(), "messages.csv")

    # Legacy standalone scans are not formal Stage-2 pipeline assessments.
    assert detector == []
    assert "final_sub_state_code" not in messages[0]
    assert messages[0]["state_code"] == "standard"
    assert messages[0]["state_assignment_source"] == "export_fallback"
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        names = archive.namelist()
        assert any(name.endswith("/interventions.csv") for name in names)
        assert not any(name.endswith("/unified-events.csv") for name in names)
