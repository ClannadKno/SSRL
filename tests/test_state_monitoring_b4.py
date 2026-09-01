# -*- coding: utf-8 -*-
"""B4 coverage for the teacher emotion trend page."""

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


BASE_TIME = datetime(2026, 7, 17, 10, 0, 0)


def _ts(offset_minutes):
    return (BASE_TIME + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _insert_assessment(
    db,
    context,
    offset_minutes,
    state_code,
    *,
    assessment_status="state_detected",
    confidence=0.8,
):
    db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, session_no, task_id,
            window_start, window_end,
            fused_state_code, fused_state_label, assessment_status,
            valence_score, interaction_activation_score, confidence,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["session_no"],
            context["task_id"],
            _ts(offset_minutes),
            _ts(offset_minutes + 2),
            state_code,
            state_code,
            assessment_status,
            0.0,
            0.0,
            confidence,
            _ts(offset_minutes),
        ),
    )


def _insert_message(
    db,
    context,
    sequence,
    user_id,
    content,
    *,
    role="student",
    agent_type=None,
    linked_log_id=None,
    intervention_run_id=None,
    created_offset_minutes=None,
):
    db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence,
            sender_type, role, session_no, task_id, session_id,
            linked_log_id, intervention_run_id, agent_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            user_id,
            content,
            sequence,
            role,
            role,
            context["session_no"],
            context["task_id"],
            context["session_id"],
            linked_log_id,
            intervention_run_id,
            agent_type,
            _ts(created_offset_minutes if created_offset_minutes is not None else sequence),
        ),
    )


def _save_state_segments(db, context, segments, *, anchor=1, source_run_id=700):
    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    return CollaborationStateSegmentService.save_strategy_llm_segments(
        group_id=context["group_id"],
        session_id=context["session_id"],
        session_no=context["session_no"],
        task_id=context["task_id"],
        state_segments=segments,
        source_run_id=source_run_id,
        analysis_anchor_message_id=anchor,
        analysis_window_start_message_id=anchor,
        analysis_window_end_message_id=max(
            [anchor] + [segment["end_message_id"] for segment in segments]
        ),
        prompt_version="b4_teacher_timeline_test",
    )


def _save_silence_segment(db, context, *, previous_sequence, start_offset, end_offset, next_sequence=None):
    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    return CollaborationStateSegmentService.save_or_update_silence_interval(
        group_id=context["group_id"],
        session_id=context["session_id"],
        session_no=context["session_no"],
        task_id=context["task_id"],
        previous_student_message_id=previous_sequence,
        next_student_message_id=next_sequence,
        start_at=_ts(start_offset),
        end_at=_ts(end_offset),
        is_finalized=next_sequence is not None,
    )


def _insert_intervention(
    db,
    context,
    cutoff_sequence,
    *,
    trigger_source,
    push_mode,
    state_code,
    pushed_by_user_id=None,
):
    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, status,
            detected_state, confidence, trigger_type,
            created_at, completed_at, generated_at, published_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            cutoff_sequence,
            "strategy",
            "PUBLISHED",
            state_code,
            0.86,
            trigger_source,
            _ts(cutoff_sequence),
            _ts(cutoff_sequence),
            _ts(cutoff_sequence),
            _ts(cutoff_sequence),
        ),
    )
    log_id = db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, pushed_by_user_id, push_mode, trigger_source,
            title, message, strategy_id, strategy_type,
            session_id, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            run_id,
            pushed_by_user_id,
            push_mode,
            trigger_source,
            "B4 intervention",
            "先各写一个依据，再比较能否合并。",
            "v2_b4_test",
            "active_intervention",
            context["session_id"],
            context["task_id"],
            _ts(cutoff_sequence),
        ),
    )
    return run_id, log_id


def test_b4_trend_reads_normalized_segments_and_counts_distribution(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=41, member_count=2, limit_minutes=20)
    student_a = context["students"][0][0]
    student_b = context["students"][1][0]

    _insert_message(db, context, 1, student_a, "先分工找证据。", created_offset_minutes=1)
    _insert_message(db, context, 2, student_b, "我负责整理标准。", created_offset_minutes=2)
    _insert_message(db, context, 3, student_a, "这个结论我不同意。", created_offset_minutes=3)
    _save_state_segments(
        db,
        context,
        [
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 2,
                "evidence_message_ids": [1, 2],
                "confidence": 0.91,
            },
            {
                "state": "conflict_tension",
                "start_message_id": 3,
                "end_message_id": 3,
                "evidence_message_ids": [3],
                "confidence": 0.72,
            },
        ],
    )

    from services.teacher_emotion_trend_service import get_emotion_trend

    trend = get_emotion_trend(
        context["group_id"],
        session_id=context["session_id"],
        start_time=_ts(0),
        end_time=_ts(10),
        window_minutes=5,
    )

    assert [segment["state_code"] for segment in trend["state_segments"]] == [
        "unclassified",
        "unclassified",
    ]
    assert trend["distribution"]["unclassified"]["segment_count"] == 2
    assert trend["distribution"]["unclassified"]["message_count"] == 3
    assert (
        trend["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 2
    )
    assert (
        trend["coarse_distribution"]["conflict_tension"]["message_count"]
        == 1
    )
    assert trend["summary"]["latest_state_code"] == "unclassified"


def test_b4_trend_ignores_legacy_assessments_when_no_segments_exist(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=43, member_count=2, limit_minutes=20)

    _insert_assessment(db, context, 0, "positive_collaboration", confidence=0.75)
    db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, session_no, task_id,
            fused_state_code, fused_state_label, assessment_status,
            confidence, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["session_no"],
            context["task_id"],
            "negative_silence",
            "消极沉默",
            "state_detected",
            0.91,
            _ts(2),
        ),
    )

    from services.teacher_emotion_trend_service import get_emotion_trend

    trend = get_emotion_trend(
        context["group_id"],
        session_id=context["session_id"],
        start_time=_ts(0),
        end_time=_ts(10),
        window_minutes=0,
    )

    assert trend["state_segments"] == []
    assert trend["silence_segments"] == []
    assert trend["snapshots"] == []
    assert trend["summary"]["latest_state_code"] == "observing"
    assert all(item["segment_count"] == 0 for item in trend["distribution"].values())


def test_b4_persist_state_assessment_writes_context_window_fields(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=44, member_count=2, limit_minutes=20)

    from services.state_assessment_service import persist_state_assessment

    result = persist_state_assessment(
        {
            "group_id": context["group_id"],
            "session_id": context["session_id"],
            "session_no": context["session_no"],
            "task_id": context["task_id"],
            "state_code": "negative_silence",
            "state_score": 0.78,
            "rule_assessment": {"assessment_status": "state_detected"},
            "context_json": {
                "window_start": _ts(3),
                "window_end": _ts(5),
            },
            "feature_json": {
                "behavior": {"interaction_intensity_score": 0.22},
                "text": {"affective_polarity_score": -0.45},
            },
        }
    )

    row = db.query_one(
        """
        SELECT fused_state_code, window_start, window_end,
               valence_score, interaction_activation_score
        FROM state_assessments
        WHERE id=?
        """,
        (result["assessment_id"],),
    )
    assert row["fused_state_code"] == "negative_silence"
    assert row["window_start"] == _ts(3)
    assert row["window_end"] == _ts(5)
    assert row["valence_score"] == -0.45
    assert row["interaction_activation_score"] == 0.22


def test_b4_review_keeps_all_states_and_participation_minutes(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=45, member_count=2, limit_minutes=20)
    student_a = context["students"][0][0]
    student_b = context["students"][1][0]

    _insert_message(
        db,
        context,
        1,
        student_a,
        "第一分钟第一条。",
        created_offset_minutes=1.12,
    )
    _insert_message(
        db,
        context,
        2,
        student_b,
        "第一分钟第二条。",
        created_offset_minutes=1.75,
    )
    _insert_message(
        db,
        context,
        3,
        student_a,
        "第二分钟继续。",
        created_offset_minutes=2.05,
    )
    _save_state_segments(
        db,
        context,
        [
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 2,
                "evidence_message_ids": [1, 2],
                "confidence": 0.8,
            }
        ],
    )

    from services.teacher_emotion_review_service import get_emotion_review

    review = get_emotion_review(
        context["group_id"],
        session_id=context["session_id"],
        start_time=_ts(1),
        end_time=_ts(3),
        window_minutes=1,
    )

    assert [segment["state_code"] for segment in review["state_segments"]] == [
        "unclassified",
    ]
    assert review["distribution"]["unclassified"]["segment_count"] == 1
    assert review["distribution"]["unclassified"]["message_count"] == 2
    assert (
        review["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 2
    )
    by_sequence_state = {
        message["sequence"]: message["state_code"]
        for message in review["messages"]
        if message["role"] == "student"
    }
    assert by_sequence_state == {
        1: "unclassified",
        2: "unclassified",
        3: "observing",
    }

    by_minute = {
        item["bucket_start"]: item
        for item in review["participation_timeline"]
    }
    assert by_minute[_ts(1)]["student_message_count"] == 2
    assert by_minute[_ts(1)]["active_student_count"] == 2
    assert by_minute[_ts(1)]["first_sequence"] == 1
    assert by_minute[_ts(2)]["student_message_count"] == 1
    assert review["window_minutes"] == 1


def test_b4_review_normalizes_insufficient_and_classifies_agent_messages(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=42, member_count=2, limit_minutes=20)
    student_id = context["students"][0][0]

    _insert_assessment(
        db,
        context,
        0,
        "positive_collaboration",
        assessment_status="insufficient_evidence",
    )
    _insert_message(db, context, 1, student_id, "我先看一下材料。")
    _insert_message(
        db,
        context,
        2,
        student_id,
        "普通提示：请继续观察小组节奏。",
        role="agent",
    )

    auto_run_id, auto_log_id = _insert_intervention(
        db,
        context,
        3,
        trigger_source="auto_intervention",
        push_mode="auto",
        state_code="conflict_tension",
    )
    _insert_message(
        db,
        context,
        3,
        student_id,
        "先把分歧对应的证据列出来。",
        role="agent",
        agent_type="strategy",
        linked_log_id=auto_log_id,
        intervention_run_id=auto_run_id,
    )

    help_run_id, help_log_id = _insert_intervention(
        db,
        context,
        4,
        trigger_source="student_help_request",
        push_mode="student_request",
        state_code="blocked_frustration",
    )
    _insert_message(
        db,
        context,
        4,
        student_id,
        "每个人说一个下一步。",
        role="agent",
        agent_type="strategy",
        linked_log_id=help_log_id,
        intervention_run_id=help_run_id,
    )

    from services.teacher_emotion_review_service import get_emotion_review

    review = get_emotion_review(
        context["group_id"],
        session_id=context["session_id"],
        start_time=_ts(0),
        end_time=_ts(10),
        window_minutes=5,
    )

    assert review["state_snapshots"] == []
    assert review["summary"]["latest_state_code"] == "observing"
    assert all(item["segment_count"] == 0 for item in review["distribution"].values())

    by_sequence = {message["sequence"]: message for message in review["messages"]}
    assert by_sequence[1]["state_code"] == "observing"
    assert by_sequence[2]["agent_message_kind"] == "legacy_agent"
    assert by_sequence[2]["agent_display_label"] == "Agent · legacy/未知来源"
    assert by_sequence[2]["state_code"] is None
    assert by_sequence[3]["agent_message_kind"] == "strategy_auto"
    assert by_sequence[3]["agent_display_label"] == "策略智能体 · 自动介入"
    assert by_sequence[4]["agent_message_kind"] == "strategy_student_help"
    assert by_sequence[4]["agent_display_label"] == "策略智能体 · 学生求助"

    intervention_kinds = {item["intervention_kind"] for item in review["interventions"]}
    assert intervention_kinds == {"strategy_auto", "strategy_student_help"}


def test_b4_review_returns_silence_as_interval_not_message(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=46, member_count=2, limit_minutes=20)
    student_a = context["students"][0][0]
    student_b = context["students"][1][0]

    _insert_message(db, context, 14, student_a, "我们先暂停一下整理材料。", created_offset_minutes=4)
    _insert_message(db, context, 15, student_b, "我回来了，继续写证据。", created_offset_minutes=8)
    _save_silence_segment(
        db,
        context,
        previous_sequence=14,
        start_offset=4,
        end_offset=8,
        next_sequence=15,
    )

    from services.teacher_emotion_review_service import get_emotion_review

    review = get_emotion_review(
        context["group_id"],
        session_id=context["session_id"],
        start_time=_ts(0),
        end_time=_ts(10),
        window_minutes=1,
    )

    assert len(review["silence_segments"]) == 1
    silence = review["silence_segments"][0]
    assert silence["state_code"] == "negative_silence"
    assert silence["previous_student_message_id"] == 14
    assert silence["next_student_message_id"] == 15
    assert silence["duration_seconds"] == 240
    assert "negative_silence" not in review["distribution"]
    assert review["active_silence"]["active"] is False
    assert {message["sequence"] for message in review["messages"]} == {14, 15}
    assert all(message["state_code"] == "observing" for message in review["messages"])


def test_b4_auto_intervention_label_wins_over_pushed_by_user_id(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=47, member_count=2, limit_minutes=20)
    student_id = context["students"][0][0]

    auto_run_id, auto_log_id = _insert_intervention(
        db,
        context,
        13,
        trigger_source="auto_v2",
        push_mode="sera_auto_v2",
        state_code="conflict_tension",
        pushed_by_user_id=student_id,
    )
    _insert_message(
        db,
        context,
        13,
        student_id,
        "先把争议点拆成证据和观点。",
        role="agent",
        agent_type="strategy",
        linked_log_id=auto_log_id,
        intervention_run_id=auto_run_id,
    )
    teacher_run_id, teacher_log_id = _insert_intervention(
        db,
        context,
        14,
        trigger_source="teacher_confirmed",
        push_mode="teacher_confirmed",
        state_code="conflict_tension",
        pushed_by_user_id=student_id,
    )
    _insert_message(
        db,
        context,
        14,
        student_id,
        "老师提醒：请先确认分工。",
        role="agent",
        agent_type="strategy",
        linked_log_id=teacher_log_id,
        intervention_run_id=teacher_run_id,
    )

    from services.teacher_emotion_review_service import get_emotion_review

    review = get_emotion_review(
        context["group_id"],
        session_id=context["session_id"],
        start_time=_ts(0),
        end_time=_ts(20),
        window_minutes=1,
    )

    by_sequence = {message["sequence"]: message for message in review["messages"]}
    assert by_sequence[13]["agent_message_kind"] == "strategy_auto"
    assert by_sequence[13]["agent_display_label"] == "策略智能体 · 自动介入"
    assert by_sequence[14]["agent_message_kind"] == "strategy_teacher"
    assert by_sequence[14]["agent_display_label"] == "教师介入"
    intervention_labels = {
        item["linked_sequence"]: item["display_label"]
        for item in review["interventions"]
    }
    assert intervention_labels[13] == "策略智能体 · 自动介入"
    assert intervention_labels[14] == "教师介入"


def test_b4_page_and_frontend_state_constants_are_six_state_only(teacher_login):
    client, headers = teacher_login
    response = client.get("/teacher/emotion-trend", headers=headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "emotion-agent-marker.ordinary" in html
    assert "/static/teacher/emotion-trend.js" in html

    with open("static/teacher/emotion-trend.js", "r", encoding="utf-8") as handle:
        source = handle.read()
    state_order = source.split("const PRIMARY_STATE_ORDER = [", 1)[1].split("];", 1)[0]
    for legacy_code in [
        "participation_imbalance",
        "coordination_disorder",
        "conflict_repair",
        "positive_recovery",
        "insufficient_evidence",
        "blocked_frustration",
        "task_detached",
        "unknown",
    ]:
        assert legacy_code not in state_order
    for final_code in [
        "standard",
        "deep_thinking",
        "execution_progress",
        "constructive_conflict",
        "interpersonal_conflict",
        "confusion",
        "frustration",
        "burnout",
        "off_topic_self_regulated",
        "off_topic_unregulated",
        "perfunctory_detachment",
        "individual_marginalization",
    ]:
        assert final_code in state_order
    for legacy_code in [
        "positive_collaboration",
        "conflict_tension",
        "off_task",
        "frustration_stuck",
        "negative_silence",
    ]:
        assert legacy_code not in state_order
    assert "UNCLASSIFIED_STATE" in source
    assert "silence_segments" in source
