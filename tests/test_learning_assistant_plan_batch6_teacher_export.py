# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import io
import json
import zipfile

from tests.helpers import seed_running_session


def _seed_feedback_and_states(db):
    scope = seed_running_session(db, session_no=96, member_count=2)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running','2026-08-04 10:00:00',
                 '2026-08-04 10:00:00','2026-08-04 10:00:00')
        """,
        (scope["session_id"], scope["group_id"]),
    )
    slot_id = db.execute(
        """
        INSERT INTO emotion_reflection_slots(
            group_id, session_id, discussion_id, slot_index, scheduled_at,
            previous_window_start, previous_window_end,
            current_window_start, current_window_end, window_frozen_at,
            previous_metrics_json, current_metrics_json,
            previous_message_ids_json, current_message_ids_json,
            input_message_ids_json, previous_messages_json,
            current_messages_json, status, started_at, completed_at,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'sent',?,?,?,?)
        """,
        (
            scope["group_id"], scope["session_id"], discussion_id, 2,
            "2026-08-04 10:10:00", "2026-08-04 10:00:00",
            "2026-08-04 10:05:00", "2026-08-04 10:05:00",
            "2026-08-04 10:10:00", "2026-08-04 10:10:00",
            '{"message_count":2}', '{"message_count":4}', "[1,2]",
            "[3,4,5,6]", "[1,2,3,4,5,6]", "[]", "[]",
            "2026-08-04 10:10:01", "2026-08-04 10:10:03",
            "2026-08-04 10:10:00", "2026-08-04 10:10:03",
        ),
    )
    assessment_id = db.execute(
        """
        INSERT INTO emotion_feedback_assessments(
            slot_id, group_id, session_id, discussion_id, slot_index,
            prompt_version, status, model_name, emotion_feedback_state,
            confidence, comparison_summary, current_window_summary,
            previous_window_summary, previous_metrics_json,
            current_metrics_json, input_message_ids_json,
            evidence_message_ids_json, validation_status,
            started_at, completed_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,'succeeded','test-model','GROUP_IMPROVING',
                 0.88,?,?,?,?,?,?,?,'VALID',?,?,?,?)
        """,
        (
            slot_id, scope["group_id"], scope["session_id"], discussion_id,
            2, "emotion_feedback_e1_v1", "互动较上一窗口更充分",
            "当前窗口多人承接观点", "上一窗口仅有少量交流",
            '{"message_count":2}', '{"message_count":4}',
            "[1,2,3,4,5,6]", "[3,4,5,6]",
            "2026-08-04 10:10:01", "2026-08-04 10:10:02",
            "2026-08-04 10:10:00", "2026-08-04 10:10:02",
        ),
    )
    db.execute(
        """
        INSERT INTO emotion_feedback_generations(
            slot_id, assessment_id, attempt_no, prompt_version,
            emotion_feedback_state, status, final_text, fallback_used,
            validation_status, started_at, completed_at, published_at,
            created_at, updated_at
        ) VALUES(?,?,1,'emotion_feedback_e2_v1','GROUP_IMPROVING',
                 'PUBLISHED',?,0,'VALID',?,?,?,?,?)
        """,
        (
            slot_id, assessment_id,
            "大家这一阶段的观点承接更充分了，继续保持这样的共同推进节奏。",
            "2026-08-04 10:10:02", "2026-08-04 10:10:03",
            "2026-08-04 10:10:03", "2026-08-04 10:10:00",
            "2026-08-04 10:10:03",
        ),
    )

    for state_code, detected_at, dedupe_key in (
        ("confusion", "2026-08-04 10:04:00", "batch6-state-before"),
        ("execution_progress", "2026-08-04 10:16:00", "batch6-state-after"),
    ):
        db.execute(
            """
            INSERT INTO collaboration_state_segments(
                group_id, session_id, session_no, task_id, discussion_id,
                state_code, canonical_sub_state_code, segment_kind,
                start_at, end_at, detected_at, is_active, source,
                assessment_status, confidence, sub_state_confidence,
                is_finalized, dedupe_key, evidence_message_ids_json,
                evidence_sequences, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,'time_range',?,?,?,0,'state_monitor',
                     'confirmed',0.9,0.9,1,?,'[]','[]',?,?)
            """,
            (
                scope["group_id"], scope["session_id"], scope["session_no"],
                scope["task_id"], discussion_id, state_code, state_code,
                detected_at, detected_at, detected_at, dedupe_key,
                detected_at, detected_at,
            ),
        )
    scope.update(discussion_id=discussion_id, slot_id=slot_id)
    return scope


def test_teacher_api_separates_emotion_feedback_from_canonical_state(
    db_and_app, teacher_login
):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = _seed_feedback_and_states(db)

    response = client.get(
        f"/api/teacher/session/{scope['session_id']}/emotion-feedbacks",
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) >= {
        "emotion_feedbacks", "canonical_states", "data_separation"
    }
    feedback = payload["emotion_feedbacks"][0]
    assert feedback["slot_index"] == 2
    assert feedback["feedback_state"] == "GROUP_IMPROVING"
    assert feedback["fallback_used"] is False
    assert feedback["final_text"].startswith("大家")
    assert "canonical_sub_state_code" not in feedback
    assert "strategy_id" not in feedback

    canonical_codes = {
        row["canonical_sub_state_code"] for row in payload["canonical_states"]
    }
    assert canonical_codes == {"confusion", "execution_progress"}
    assert all("emotion_feedback_state" not in row for row in payload["canonical_states"])
    assert payload["data_separation"]["shared_runtime_input"] is False


def test_emotion_feedback_research_export_has_full_contract_and_export_only_links(
    db_and_app, teacher_login
):
    db, _app, client = db_and_app
    _client, headers = teacher_login
    scope = _seed_feedback_and_states(db)

    response = client.get("/export/emotion-feedback", headers=headers)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        path = next(
            name for name in archive.namelist() if name.endswith("/emotion_feedback.csv")
        )
        rows = list(
            csv.DictReader(
                io.StringIO(archive.read(path).decode("utf-8-sig"))
            )
        )
    assert len(rows) == 1
    row = rows[0]
    from services.research_export_service import EMOTION_FEEDBACK_EXPORT_COLUMNS

    assert list(row) == EMOTION_FEEDBACK_EXPORT_COLUMNS
    assert row["session_id"] == str(scope["session_id"])
    assert row["discussion_id"] == str(scope["discussion_id"])
    assert row["emotion_feedback_state"] == "GROUP_IMPROVING"
    assert json.loads(row["evidence_message_ids"]) == [3, 4, 5, 6]
    assert json.loads(row["current_metrics"])["message_count"] == 4
    assert row["nearest_previous_canonical_state"] == "confusion"
    assert row["nearest_next_canonical_state"] == "execution_progress"
    assert "strategy_id" not in row


def test_teacher_ui_exposes_unified_mode_and_separate_record_sections(
    teacher_login,
):
    client, headers = teacher_login
    page = client.get("/teacher/session/control", headers=headers)
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "createAgentModeStrategy" in html
    assert "createStrategyAgentEnabled" not in html
    assert "createEmotionAgentEnabled" not in html

    js = client.get("/static/teacher/session-control.js").get_data(as_text=True)
    assert "/emotion-feedbacks?limit=200" in js
    assert "情绪反馈记录（独立分类）" in js
    assert "Canonical 状态时间线（独立监测）" in js
    assert "后台状态监测：启用" in js
