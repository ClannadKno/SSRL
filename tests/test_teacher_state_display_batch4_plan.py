# -*- coding: utf-8 -*-
"""Plan batch 4: confirmed / observing / unclassified teacher display."""

from __future__ import annotations

import importlib

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def display_env(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=904, member_count=1, limit_minutes=20)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, created_at, updated_at
        ) VALUES(?,?,'running',?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            db.now_str(),
            db.now_str(),
        ),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    service = importlib.import_module(
        "services.state_assessment_batch_service"
    ).StateAssessmentBatchService
    return db, service, scope


def _student_message(db, scope, content):
    return db.create_message(
        scope["group_id"],
        scope["student_id"],
        content,
        role="student",
        sender_type="student",
        session_no=scope["session_no"],
        task_id=scope["task_id"],
    )


def _save_batch(service, scope, start, end, segments, *, reason="constructive_progress"):
    created = service.create_batch(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        candidate_start_sequence=start,
        candidate_end_sequence=end,
        context_start_sequence=start,
        context_end_sequence=end,
        trigger_type="post_intervention_observation",
        trigger_sequence=end,
        model="batch4-test-model",
        prompt_version="batch4-display-v1",
    )
    batch_id = created["batch"]["id"]
    assert service.claim_batch(batch_id)["claimed"] is True
    parsed = {
        "segments": segments,
        "active_segment_index": None,
        "intervention": {
            "needed": False,
            "target_segment_index": None,
            "reason_code": reason,
            "message": None,
        },
    }
    return service.save_successful_segments(
        batch_id,
        segments,
        parsed_response=parsed,
        model="batch4-test-model",
        prompt_version="batch4-display-v1",
    )


def _review(scope):
    from services.teacher_emotion_review_service import get_emotion_review

    return get_emotion_review(
        scope["group_id"],
        session_id=scope["session_id"],
        start_time="2000-01-01 00:00:00",
        end_time="2099-01-01 00:00:00",
        window_minutes=1,
    )


def test_canonical_view_uses_sequence_fields_and_keeps_other_persisted_segments(
    display_env,
):
    db, service, scope = display_env
    _student_message(db, scope, "first")
    _student_message(db, scope, "second")
    _student_message(db, scope, "not enough evidence")
    legacy_service = importlib.import_module(
        "services.collaboration_state_segment_service"
    ).CollaborationStateSegmentService
    legacy_service.save_strategy_llm_segments(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        session_no=scope["session_no"],
        task_id=scope["task_id"],
        state_segments=[
            {
                "state": "task_detached",
                "start_message_id": 3,
                "end_message_id": 3,
                "evidence_message_ids": [3],
                "confidence": 0.99,
            }
        ],
        analysis_anchor_message_id=3,
        analysis_window_start_message_id=3,
        analysis_window_end_message_id=3,
        prompt_version="legacy-row-that-must-not-mix",
    )
    saved = _save_batch(
        service,
        scope,
        1,
        2,
        [
            {
                "state": "positive_collaboration",
                "canonical_sub_state_code": "execution_progress",
                "start_sequence": 1,
                "end_sequence": 2,
                "evidence_sequences": [1, 2],
                "confidence": 0.86,
                "is_active_at_batch_end": True,
            }
        ],
    )
    segment_id = saved["segment_ids"][0]
    # The compatibility columns are intentionally misleading. Canonical reads
    # must still use explicit sequence fields while retaining the other legal
    # persisted segment.
    db.execute(
        "UPDATE collaboration_state_segments SET start_message_id=901, end_message_id=902 WHERE id=?",
        (segment_id,),
    )

    review = _review(scope)
    messages = {m["sequence"]: m for m in review["messages"] if m["role"] == "student"}
    assert messages[1]["semantic_state"] == "execution_progress"
    assert messages[1]["assessment_status"] == "confirmed"
    assert messages[1]["state_segment_id"] == segment_id
    assert messages[3]["semantic_state"] is None
    assert messages[3]["assessment_status"] == "unclassified"
    assert messages[3]["state_code"] == "unclassified"
    assert review["state_source_mode"] == "canonical"
    assert review["state_segments"][0]["start_sequence"] == 1
    assert review["state_segments"][0]["source"] == "llm"


def test_strategy_publish_waits_then_first_student_message_starts_observing(display_env):
    db, _service, scope = display_env
    before = _student_message(db, scope, "before intervention")
    publisher = importlib.import_module("services.agent_intervention_publisher")
    published = publisher.publish_agent_intervention(
        group_id=scope["group_id"],
        message="先对齐目标，再继续讨论。",
        trigger_source="auto_state",
        agent_type="strategy",
        session_id=scope["session_id"],
        session_no=scope["session_no"],
        task_id=scope["task_id"],
        detected_state="conflict_tension",
        confidence=0.9,
        prompt_version="batch4-observation-test",
    )
    assert published["ok"] is True
    cursor_waiting = db.query_one(
        "SELECT * FROM discussion_assessment_cursors WHERE discussion_id=?",
        (scope["discussion_id"],),
    )
    assert cursor_waiting["observation_status"] == "observing"
    assert cursor_waiting["observation_started_sequence"] is None

    after = _student_message(db, scope, "after intervention")
    cursor_started = db.query_one(
        "SELECT * FROM discussion_assessment_cursors WHERE discussion_id=?",
        (scope["discussion_id"],),
    )
    assert cursor_started["observation_started_sequence"] == after["sequence"]

    review = _review(scope)
    messages_by_sequence = {m["sequence"]: m for m in review["messages"]}
    agent_message = next(m for m in review["messages"] if m["id"] == published["message_id"])
    assert messages_by_sequence[before["sequence"]]["assessment_status"] == "unclassified"
    assert agent_message["assessment_status"] is None
    assert messages_by_sequence[after["sequence"]]["assessment_status"] == "observing"
    assert review["current_state"]["semantic_state"] is None
    assert review["current_state"]["assessment_status"] == "observing"


def test_confirmed_segment_overrides_only_covered_observation_messages(display_env):
    db, service, scope = display_env
    _student_message(db, scope, "before")
    conn = db.db()
    try:
        db.begin_discussion_observation(
            conn,
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            intervention_sequence=1,
            updated_at=db.now_str(),
        )
        conn.commit()
    finally:
        conn.close()
    first_observed = _student_message(db, scope, "clear conflict")
    second_observed = _student_message(db, scope, "unclear follow-up")
    _save_batch(
        service,
        scope,
        first_observed["sequence"],
        first_observed["sequence"],
        [
            {
                "state": "conflict_tension",
                "canonical_sub_state_code": "interpersonal_conflict",
                "start_sequence": first_observed["sequence"],
                "end_sequence": first_observed["sequence"],
                "evidence_sequences": [first_observed["sequence"]],
                "confidence": 0.91,
                "is_active_at_batch_end": True,
            }
        ],
        reason="continue_observing",
    )

    review = _review(scope)
    messages = {m["sequence"]: m for m in review["messages"] if m["role"] == "student"}
    assert messages[first_observed["sequence"]]["assessment_status"] == "confirmed"
    assert (
        messages[first_observed["sequence"]]["semantic_state"]
        == "interpersonal_conflict"
    )
    assert messages[second_observed["sequence"]]["assessment_status"] == "observing"
    assert review["current_state"]["assessment_status"] == "observing"
    assert review["current_state"]["semantic_state"] is None


def test_two_successful_observation_rounds_expire_old_gaps_but_not_future_messages(display_env):
    db, service, scope = display_env
    conn = db.db()
    try:
        db.begin_discussion_observation(
            conn,
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            intervention_sequence=0,
            updated_at=db.now_str(),
        )
        conn.commit()
    finally:
        conn.close()
    first = _student_message(db, scope, "round one unclear")
    second = _student_message(db, scope, "round two unclear")
    third = _student_message(db, scope, "future observation")
    _save_batch(
        service,
        scope,
        first["sequence"],
        first["sequence"],
        [],
        reason="continue_observing",
    )
    _save_batch(
        service,
        scope,
        second["sequence"],
        second["sequence"],
        [],
        reason="insufficient_evidence",
    )

    review = _review(scope)
    messages = {m["sequence"]: m for m in review["messages"] if m["role"] == "student"}
    assert messages[first["sequence"]]["assessment_status"] == "unclassified"
    assert messages[second["sequence"]]["assessment_status"] == "unclassified"
    assert messages[third["sequence"]]["assessment_status"] == "observing"
    assert review["message_state_context"]["observation_expired_through_sequence"] == second["sequence"]
    assert service.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=first["sequence"],
    )["assessment_status"] == "unclassified"


def test_successful_recovery_ends_observing_and_updates_current_state(display_env):
    db, service, scope = display_env
    conn = db.db()
    try:
        db.begin_discussion_observation(
            conn,
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            intervention_sequence=0,
            updated_at=db.now_str(),
        )
        conn.commit()
    finally:
        conn.close()
    recovered = _student_message(db, scope, "we have a shared plan now")
    _save_batch(
        service,
        scope,
        recovered["sequence"],
        recovered["sequence"],
        [
            {
                "state": "positive_collaboration",
                "canonical_sub_state_code": "execution_progress",
                "start_sequence": recovered["sequence"],
                "end_sequence": recovered["sequence"],
                "evidence_sequences": [recovered["sequence"]],
                "confidence": 0.94,
                "is_active_at_batch_end": True,
            }
        ],
        reason="constructive_progress",
    )
    review = _review(scope)
    assert review["current_state"]["assessment_status"] == "confirmed"
    assert review["current_state"]["semantic_state"] == "execution_progress"
    cursor = db.query_one(
        "SELECT observation_status FROM discussion_assessment_cursors WHERE discussion_id=?",
        (scope["discussion_id"],),
    )
    assert cursor["observation_status"] == "inactive"


def test_overlap_uses_newer_successful_batch_and_reports_warning(display_env):
    db, service, scope = display_env
    for content in ("one", "two", "three"):
        _student_message(db, scope, content)
    first = _save_batch(
        service,
        scope,
        1,
        2,
        [
            {
                "state": "positive_collaboration",
                "canonical_sub_state_code": "execution_progress",
                "start_sequence": 1,
                "end_sequence": 2,
                "evidence_sequences": [1, 2],
                "confidence": 0.99,
            }
        ],
    )
    second = _save_batch(
        service,
        scope,
        2,
        3,
        [
            {
                "state": "conflict_tension",
                "canonical_sub_state_code": "interpersonal_conflict",
                "start_sequence": 2,
                "end_sequence": 3,
                "evidence_sequences": [2, 3],
                "confidence": 0.51,
            }
        ],
    )
    assert second["batch"]["id"] > first["batch"]["id"]

    review = _review(scope)
    message_two = next(m for m in review["messages"] if m["sequence"] == 2)
    assert message_two["semantic_state"] == "interpersonal_conflict"
    assert any(
        warning["type"] == "overlapping_state_segments"
        for warning in review["quality_warnings"]
    )


def test_persisted_legacy_segment_is_not_promoted_to_final_state(display_env):
    db, _service, scope = display_env
    _student_message(db, scope, "legacy covered")
    _student_message(db, scope, "legacy gap")
    legacy_service = importlib.import_module(
        "services.collaboration_state_segment_service"
    ).CollaborationStateSegmentService
    legacy_service.save_strategy_llm_segments(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        session_no=scope["session_no"],
        task_id=scope["task_id"],
        state_segments=[
            {
                "state": "task_detached",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.8,
            }
        ],
        analysis_anchor_message_id=1,
        analysis_window_start_message_id=1,
        analysis_window_end_message_id=1,
        prompt_version="legacy-test",
    )
    review = _review(scope)
    messages = {m["sequence"]: m for m in review["messages"] if m["role"] == "student"}
    assert review["state_source_mode"] == "canonical"
    assert review["state_segments"][0]["source"] == "strategy_llm"
    assert review["state_segments"][0]["raw_source"] == "strategy_llm"
    assert messages[1]["assessment_status"] == "unclassified"
    assert messages[1]["semantic_state"] is None
    assert messages[2]["assessment_status"] == "observing"


def test_frontend_distinguishes_processing_states_without_red_unclassified_warning():
    source = open(
        "static/teacher/emotion-trend.js", "r", encoding="utf-8"
    ).read()
    page = open("routes/pages.py", "r", encoding="utf-8").read()
    assert "UNCLASSIFIED_STATE" in source
    assert "message.assessment_status" in source
    assert "emotion-chip observing" in source
    assert "emotion-state-empty" in source
    assert ".emotion-chip.observing" in page
    assert ".emotion-message.status-unclassified" in page
