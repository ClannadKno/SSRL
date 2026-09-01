# -*- coding: utf-8 -*-
"""Batch 1 regressions for the teacher canonical state view."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.helpers import seed_running_session


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def canonical_teacher_state(db_and_app):
    db, _app, _client = db_and_app
    scope = seed_running_session(
        db,
        session_no=811,
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
    for sequence in range(1, 7):
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_no, task_id, session_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                student_id,
                f"student message {sequence}",
                sequence,
                "student",
                "student",
                scope["session_no"],
                scope["task_id"],
                scope["session_id"],
                _stamp(now - timedelta(minutes=4) + timedelta(seconds=sequence)),
            ),
        )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=6, cutoff_sequence=6
        WHERE id=?
        """,
        (scope["group_id"],),
    )

    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            context_start_sequence, context_end_sequence,
            trigger_type, trigger_sequence, window_key, status,
            attempt_count, max_attempts, completed_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'succeeded',1,2,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            discussion_id,
            1,
            2,
            1,
            2,
            "message_batch",
            2,
            f"batch1-canonical-{scope['session_id']}",
            _stamp(now - timedelta(minutes=3)),
            _stamp(now - timedelta(minutes=3)),
            _stamp(now - timedelta(minutes=3)),
        ),
    )
    llm_segment_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id,
            state_code, raw_sub_state_code, canonical_sub_state_code,
            coarse_state_code, segment_kind,
            start_message_id, end_message_id, start_sequence, end_sequence,
            assessment_batch_id, evidence_message_ids_json, evidence_sequences,
            confidence, sub_state_confidence, source, assessment_status, segment_order,
            is_active_at_batch_end, is_finalized, dedupe_key,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,'message_range',?,?,?,?,?,?,?,?,?,'llm','confirmed',
                 0,1,1,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            "positive_collaboration",
            "execution_progress",
            "execution_progress",
            "positive_collaboration",
            1,
            2,
            1,
            2,
            batch_id,
            json.dumps([1, 2]),
            json.dumps([1, 2]),
            0.93,
            0.93,
            f"batch1-llm-{scope['session_id']}",
            _stamp(now - timedelta(minutes=3)),
            _stamp(now - timedelta(minutes=3)),
        ),
    )
    rule_assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no,
            fused_state_code, assessment_status, confidence,
            should_intervene, evidence_summary,
            fusion_json, rule_assessment_json, context_json, created_at
        ) VALUES(?,?,?,?,?,'confirmed',0.88,1,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["task_id"],
            scope["session_no"],
            "task_detached",
            "rule evidence",
            json.dumps({"evidence_sequences": [2, 3, 4]}),
            json.dumps({"evidence_sequences": [2, 3, 4]}),
            json.dumps({"evidence_sequences": [2, 3, 4]}),
            _stamp(now - timedelta(minutes=2)),
        ),
    )
    rule_segment_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id,
            state_code, segment_kind,
            start_message_id, end_message_id, start_sequence, end_sequence,
            evidence_message_ids_json, evidence_sequences,
            confidence, source, assessment_status, assessment_id,
            is_finalized, dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,'message_range',?,?,?,?,?,?,0.88,'state_monitor',
                 'confirmed',?,1,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            "task_detached",
            2,
            4,
            2,
            4,
            json.dumps([2, 3, 4]),
            json.dumps([2, 3, 4]),
            rule_assessment_id,
            f"batch1-rule-{scope['session_id']}",
            _stamp(now - timedelta(minutes=2)),
            _stamp(now - timedelta(minutes=2)),
        ),
    )
    silence_segment_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id,
            state_code, segment_kind, start_at, end_at,
            trigger_sequence, last_observed_at, is_active,
            evidence_message_ids_json, evidence_sequences,
            source, assessment_id, is_finalized, dedupe_key,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,'time_range',?,NULL,?,?,1,'[]','[]',
                 'silence_rule',?,0,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            "negative_silence",
            _stamp(now - timedelta(seconds=30)),
            6,
            _stamp(now),
            rule_assessment_id,
            f"batch1-silence-{scope['session_id']}",
            _stamp(now - timedelta(seconds=30)),
            _stamp(now),
        ),
    )
    db.execute(
        """
        INSERT INTO discussion_assessment_cursors(
            group_id, session_id, discussion_id,
            last_finalized_student_sequence, observation_status, updated_at
        ) VALUES(?,?,?,?, 'inactive', ?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            discussion_id,
            4,
            _stamp(now),
        ),
    )
    intervention_run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            state_assessment_id, group_id, session_id, discussion_id, task_id,
            cutoff_sequence, status, target_segment_id, trigger_type,
            detected_state, confidence, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,'PUBLISHED',?,'auto_state',?,?,?,?)
        """,
        (
            rule_assessment_id,
            scope["group_id"],
            scope["session_id"],
            discussion_id,
            scope["task_id"],
            4,
            rule_segment_id,
            "task_detached",
            0.88,
            _stamp(now - timedelta(minutes=1)),
            _stamp(now - timedelta(minutes=1)),
        ),
    )
    db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, trigger_source, message,
            state_assessment_id, intervention_run_id, agent_type,
            session_id, task_id, created_at
        ) VALUES(?,?,'auto_state','请回到任务证据。',?,?, 'strategy',?,?,?)
        """,
        (
            scope["group_id"],
            intervention_run_id,
            rule_assessment_id,
            intervention_run_id,
            scope["session_id"],
            scope["task_id"],
            _stamp(now - timedelta(minutes=1)),
        ),
    )
    scope.update(
        {
            "llm_segment_id": llm_segment_id,
            "rule_segment_id": rule_segment_id,
            "silence_segment_id": silence_segment_id,
            "rule_assessment_id": rule_assessment_id,
        }
    )
    return db, scope


def _review(scope, session_id):
    from services.teacher_emotion_review_service import get_emotion_review

    return get_emotion_review(
        scope["group_id"],
        session_id=session_id,
        start_time="2000-01-01 00:00:00",
        end_time="2099-01-01 00:00:00",
        window_minutes=1,
    )


def _message_signature(review):
    return [
        (
            message["sequence"],
            message["semantic_state"],
            message["assessment_status"],
            message["canonical_segment_id"],
            message["state_assignment_reason"],
        )
        for message in review["messages"]
        if message["role"] == "student"
    ]


def _segment_signature(review):
    return [
        (
            segment["id"],
            segment["state_code"],
            segment["start_sequence"],
            segment["end_sequence"],
            segment["segment_source"],
        )
        for segment in review["state_segments"]
    ]


def test_implicit_and_explicit_session_share_one_canonical_view(
    canonical_teacher_state,
):
    _db, scope = canonical_teacher_state
    implicit = _review(scope, None)
    explicit = _review(scope, scope["session_id"])

    assert implicit["scope_mode"] == "resolved_active_session"
    assert explicit["scope_mode"] == "explicit_session"
    assert implicit["resolved_session_id"] == explicit["resolved_session_id"]
    assert implicit["state_source_mode"] == explicit["state_source_mode"] == "canonical"
    assert implicit["state_source_policy"] == explicit["state_source_policy"]
    assert _message_signature(implicit) == _message_signature(explicit)
    assert _segment_signature(implicit) == _segment_signature(explicit)
    assert implicit["distribution"] == explicit["distribution"]
    assert implicit["current_state"] == explicit["current_state"]


def test_emotion_review_api_exposes_the_same_canonical_payload(
    canonical_teacher_state,
    teacher_login,
):
    _db, scope = canonical_teacher_state
    client, headers = teacher_login
    path = f"/api/teacher/group/{scope['group_id']}/emotion-review"
    common_query = {
        "start_time": "2000-01-01 00:00:00",
        "end_time": "2099-01-01 00:00:00",
        "window_minutes": 1,
    }

    implicit_response = client.get(
        path,
        query_string=common_query,
        headers=headers,
    )
    explicit_response = client.get(
        path,
        query_string={
            **common_query,
            "session_id": scope["session_id"],
        },
        headers=headers,
    )

    assert implicit_response.status_code == explicit_response.status_code == 200
    implicit = implicit_response.get_json()
    explicit = explicit_response.get_json()
    assert _message_signature(implicit) == _message_signature(explicit)
    assert _segment_signature(implicit) == _segment_signature(explicit)
    assert implicit["distribution"] == explicit["distribution"]
    assert implicit["current_state"] == explicit["current_state"]


def test_llm_and_rule_segments_coexist_and_overlap_is_deterministic(
    canonical_teacher_state,
):
    _db, scope = canonical_teacher_state
    review = _review(scope, scope["session_id"])
    messages = {
        message["sequence"]: message
        for message in review["messages"]
        if message["role"] == "student"
    }

    assert {segment["id"] for segment in review["state_segments"]} == {
        scope["llm_segment_id"],
        scope["rule_segment_id"],
    }
    assert messages[2]["semantic_state"] == "execution_progress"
    assert messages[2]["state_segment_id"] == scope["llm_segment_id"]
    assert messages[3]["semantic_state"] is None
    assert messages[3]["final_sub_state_code"] is None
    assert messages[3]["assignment_source"] == "legacy_monitor_only"
    assert messages[3]["assessment_status"] == "unclassified"
    assert messages[3]["state_segment_id"] == scope["rule_segment_id"]
    assert any(
        warning["type"] == "overlapping_state_segments"
        for warning in review["quality_warnings"]
    )


def test_message_state_counts_are_conserved(canonical_teacher_state):
    _db, scope = canonical_teacher_state
    review = _review(scope, scope["session_id"])
    summary = review["summary"]
    messages = {
        message["sequence"]: message
        for message in review["messages"]
        if message["role"] == "student"
    }
    assert summary["student_state_count_invariant"] is True
    assert (
        summary["confirmed_student_message_count"]
        + summary["observing_student_message_count"]
        + summary["unclassified_student_message_count"]
        == summary["student_message_count"]
    )
    assert sum(
        item["message_count"] for item in review["distribution"].values()
    ) == summary["student_message_count"]
    assert messages[5]["assessment_status"] == "observing"
    assert messages[6]["assessment_status"] == "observing"
    assert messages[5]["state_assignment_reason"] == "awaiting_assessment_batch"


def test_non_overlapping_llm_and_rule_segments_are_both_retained(
    canonical_teacher_state,
):
    db, scope = canonical_teacher_state
    db.execute(
        """
        UPDATE collaboration_state_segments
        SET start_message_id=3, start_sequence=3,
            evidence_message_ids_json=?, evidence_sequences=?
        WHERE id=?
        """,
        (
            json.dumps([3, 4]),
            json.dumps([3, 4]),
            scope["rule_segment_id"],
        ),
    )

    review = _review(scope, scope["session_id"])
    segments = {
        segment["id"]: segment for segment in review["state_segments"]
    }
    messages = {
        message["sequence"]: message
        for message in review["messages"]
        if message["role"] == "student"
    }

    assert set(segments) == {
        scope["llm_segment_id"],
        scope["rule_segment_id"],
    }
    assert segments[scope["llm_segment_id"]]["end_sequence"] == 2
    assert segments[scope["rule_segment_id"]]["start_sequence"] == 3
    assert messages[2]["semantic_state"] == "execution_progress"
    assert messages[3]["semantic_state"] is None
    assert messages[3]["assignment_source"] == "legacy_monitor_only"
    assert messages[3]["assessment_status"] == "unclassified"


def test_different_sessions_never_share_messages_or_segments(
    canonical_teacher_state,
):
    db, scope = canonical_teacher_state
    now = datetime.now().replace(microsecond=0)
    other_task_id = db.execute(
        """
        INSERT INTO learning_tasks(
            title, question, time_limit_minutes, created_at
        ) VALUES(?,?,?,?)
        """,
        ("Other session task", "Keep state scopes isolated", 30, _stamp(now)),
    )
    other_session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            scope["session_no"] + 1,
            "discussion",
            other_task_id,
            "ended",
            _stamp(now - timedelta(minutes=10)),
            30,
            _stamp(now - timedelta(minutes=10)),
            _stamp(now),
        ),
    )
    student_id = scope["students"][0][0]
    for sequence in (7, 8):
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_no, task_id, session_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                student_id,
                f"other session message {sequence}",
                sequence,
                "student",
                "student",
                scope["session_no"] + 1,
                other_task_id,
                other_session_id,
                _stamp(now - timedelta(minutes=1) + timedelta(seconds=sequence)),
            ),
        )
    other_segment_id = db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id,
            state_code, segment_kind,
            start_message_id, end_message_id, start_sequence, end_sequence,
            evidence_message_ids_json, evidence_sequences,
            confidence, source, assessment_status,
            is_finalized, dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,'message_range',?,?,?,?,?,?,0.9,'state_monitor',
                 'confirmed',1,?,?,?)
        """,
        (
            scope["group_id"],
            other_session_id,
            scope["session_no"] + 1,
            other_task_id,
            "conflict_tension",
            7,
            8,
            7,
            8,
            json.dumps([7, 8]),
            json.dumps([7, 8]),
            f"batch1-other-session-{other_session_id}",
            _stamp(now),
            _stamp(now),
        ),
    )

    original = _review(scope, scope["session_id"])
    other = _review(scope, other_session_id)

    assert {message["sequence"] for message in original["messages"]} == set(
        range(1, 7)
    )
    assert {segment["id"] for segment in original["state_segments"]} == {
        scope["llm_segment_id"],
        scope["rule_segment_id"],
    }
    assert {message["sequence"] for message in other["messages"]} == {7, 8}
    assert {segment["id"] for segment in other["state_segments"]} == {
        other_segment_id
    }


def test_active_silence_and_rule_intervention_survive_explicit_scope(
    canonical_teacher_state,
):
    _db, scope = canonical_teacher_state
    implicit = _review(scope, None)
    explicit = _review(scope, scope["session_id"])

    for review in (implicit, explicit):
        assert review["current_state"]["semantic_state"] is None
        assert review["current_state"]["assessment_status"] == "observing"
        assert review["active_silence"]["active"] is True
        assert (
            review["active_silence"]["segment_id"]
            == scope["silence_segment_id"]
        )
        assert any(
            segment["id"] == scope["rule_segment_id"]
            for segment in review["state_segments"]
        )
        intervention = review["interventions"][0]
        assert intervention["target_segment_id"] == scope["rule_segment_id"]
        assert intervention["state_assessment_id"] == scope["rule_assessment_id"]


def test_frontend_ignores_stale_session_requests():
    source = open(
        "static/teacher/emotion-trend.js",
        "r",
        encoding="utf-8",
    ).read()

    assert "latestLoadRequestId" in source
    assert "const requestId = ++latestLoadRequestId" in source
    assert source.count("requestId !== latestLoadRequestId") >= 2


def test_precise_sub_state_is_exposed_with_debug_only_coarse_distribution(
    canonical_teacher_state,
):
    db, scope = canonical_teacher_state
    db.execute(
        """
        UPDATE collaboration_state_segments
        SET raw_sub_state_code='建设性争论',
            canonical_sub_state_code='constructive_conflict',
            sub_state_confidence=0.96,
            source_stage='stage2'
        WHERE id=?
        """,
        (scope["llm_segment_id"],),
    )

    review = _review(scope, scope["session_id"])
    detailed = next(
        segment
        for segment in review["state_segments"]
        if segment["id"] == scope["llm_segment_id"]
    )
    messages = {
        message["sequence"]: message
        for message in review["messages"]
        if message["role"] == "student"
    }

    assert detailed["state_code"] == "constructive_conflict"
    assert detailed["state_label"] == "建设性冲突"
    assert detailed["canonical_sub_state_code"] == "constructive_conflict"
    assert detailed["coarse_state_code"] == "positive_collaboration"
    assert messages[1]["semantic_state"] == "constructive_conflict"
    assert messages[1]["state_code"] == "constructive_conflict"
    assert (
        review["detailed_distribution"]["constructive_conflict"][
            "message_count"
        ]
        == 2
    )
    assert review["distribution"]["constructive_conflict"]["message_count"] == 2
    assert (
        review["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 2
    )
    assert any(
        item["code"] == "constructive_conflict"
        for item in review["detailed_state_system"]
    )


def test_frontend_uses_detailed_state_system_and_dynamic_lanes():
    source = open(
        "static/teacher/emotion-trend.js",
        "r",
        encoding="utf-8",
    ).read()

    assert "PRIMARY_STATE_ORDER" in source
    assert "data.detailed_state_system || data.state_system" in source
    assert "data.detailed_distribution || data.distribution" in source
    assert "const laneOrder = stateLaneOrder(data)" in source
