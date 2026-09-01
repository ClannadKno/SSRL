# -*- coding: utf-8 -*-
"""Batch 6 end-to-end regression for the fixed five-state message stream."""

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


BASE_TIME = datetime(2026, 7, 21, 9, 0, 0)


STREAM = {
    1: ("student", "成员进入页面，先打开资料。", None),
    2: ("student", "我先准备校园共享学习空间的任务材料。", None),
    3: ("student", "我们把任务拆成问题、证据、方案和风险四块。", None),
    4: ("student", "我负责整理观察证据，先看自习区拥挤和插座不足。", None),
    5: ("student", "我负责访谈材料，关注小组讨论区不够的问题。", None),
    6: ("student", "我来做比较表，按成本、证据和可执行性评分。", None),
    7: ("student", "我觉得预约制可以解决高峰期排队。", None),
    8: ("student", "也可以增加开放讨论区。", None),
    9: ("student", "方案先列出来，证据等会儿再补。", None),
    10: ("student", "我先写一个提纲，暂时不下结论。", None),
    11: ("student", "这个任务有点无聊，感觉怎么写都差不多。", None),
    12: ("student", "先不讨论了吧，等会儿吃什么？", None),
    13: ("agent", "先把跑题内容收一下，回到问题和证据。", "strategy"),
    14: ("student", "要不聊会儿游戏，反正最后能交。", None),
    15: ("student", "我拉回来，预约制还需要访谈证据支持。", None),
    16: ("agent", "我注意到大家在重新整理节奏。", "emotion"),
    17: ("student", "预约制会不会让临时自习的人更不方便？", None),
    18: ("student", "小组讨论区经常被占满，可以作为观察证据。", None),
    19: ("student", "我还没想好访谈材料怎么用。", None),
    20: ("student", "我也不确定标准怎么列。", None),
    21: ("student", "不知道怎么把问题、证据和方案连起来。", None),
    22: ("agent", "可以先列出最卡的一步，再拆成可做的小任务。", "strategy"),
    23: ("student", "谁负责证据谁负责成本？我不知道下一步写哪部分。", None),
    24: ("student", "你这个全预约制根本没考虑临时自习的人。", None),
    25: ("student", "你也别一直否定，开放讨论区才是重点。", None),
    26: ("student", "别乱说，你没有看清任务要求，成本也没算。", None),
    27: ("student", "先别争，我们按证据、成本、可执行性给两个方案打分。", None),
    28: ("student", "我来记录评分，M2补预约数据，M3补讨论区访谈。", None),
    29: ("student", "预约方案证据是高峰期座位紧张和排队。", None),
    30: ("student", "讨论区方案证据是访谈里多人说没有地方讨论。", None),
    31: ("student", "阶段总结：保留两个方案，主方案是分区优化。", None),
    32: ("agent", "你们已经开始把分歧转成比较标准。", "emotion"),
    33: ("student", "我把风险补进表格：维护成本和安静区干扰。", None),
    34: ("student", "比较表按问题证据、优化动作和风险三列整理。", None),
    35: ("student", "最终结论先调整分区和预约规则，再看试点数据。", None),
    36: ("student", "我们最后检查成果要求，证据、比较和优先级都覆盖了。", None),
    37: ("agent", "讨论收尾时大家已经形成了比较完整的方案。", "emotion"),
}


def _ts(sequence):
    # Leave a visible four-minute silence between #14 and #15.
    offset = sequence if sequence <= 14 else sequence + 3
    return (BASE_TIME + timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M:%S")


def _agent_user(db):
    now = db.now_str()
    row = db.query_one("SELECT id FROM users WHERE role='agent' ORDER BY id LIMIT 1")
    if row:
        return row["id"]
    return db.execute(
        """
        INSERT INTO users(username, password_hash, real_name, role, created_at)
        VALUES(?,?,?,?,?)
        """,
        ("sera_batch6_e2e", "x", "SERA", "agent", now),
    )


def _insert_message(db, ctx, sequence, role, content, agent_type=None, linked_log_id=None, run_id=None):
    students = ctx["students"]
    user_id = _agent_user(db) if role == "agent" else students[(sequence - 1) % len(students)][0]
    return db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, agent_type,
            linked_log_id, intervention_run_id, strategy_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            user_id,
            content,
            sequence,
            "agent" if role == "agent" else "student",
            role,
            ctx["session_no"],
            ctx["task_id"],
            ctx["session_id"],
            agent_type,
            linked_log_id,
            run_id,
            "v2_batch6_e2e" if agent_type == "strategy" else None,
            _ts(sequence),
        ),
    )


def _insert_intervention(db, ctx, sequence, *, trigger_source, push_mode):
    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, status,
            detected_state, confidence, trigger_type,
            created_at, completed_at, generated_at, published_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            sequence,
            "strategy",
            "PUBLISHED",
            "task_detached" if sequence == 13 else "blocked_frustration",
            0.84,
            trigger_source,
            _ts(sequence),
            _ts(sequence),
            _ts(sequence),
            _ts(sequence),
        ),
    )
    log_id = db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, push_mode, trigger_source,
            title, message, strategy_id, strategy_type,
            session_id, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            run_id,
            push_mode,
            trigger_source,
            "Batch6 intervention",
            STREAM[sequence][1],
            "v2_batch6_e2e",
            "active_intervention",
            ctx["session_id"],
            ctx["task_id"],
            _ts(sequence),
        ),
    )
    return run_id, log_id


def _seed_fixed_stream(db):
    ctx = seed_running_session(db, session_no=601, member_count=4, limit_minutes=40)
    db.execute(
        """
        UPDATE learning_tasks
        SET title=?, question=?, task_goal=?, output_requirement=?
        WHERE id=?
        """,
        (
            "校园共享学习空间优化",
            "怎样兼顾个人学习和小组协作？",
            "形成有证据支持的空间优化方案。",
            "提交包含证据、比较标准、风险和优先级的方案。",
            ctx["task_id"],
        ),
    )
    for sequence, (role, content, agent_type) in STREAM.items():
        linked_log_id = None
        run_id = None
        if sequence == 13:
            run_id, linked_log_id = _insert_intervention(
                db,
                ctx,
                sequence,
                trigger_source="auto_v2",
                push_mode="sera_auto_v2",
            )
        elif sequence == 22:
            run_id, linked_log_id = _insert_intervention(
                db,
                ctx,
                sequence,
                trigger_source="student_help_request",
                push_mode="student_request",
            )
        _insert_message(
            db,
            ctx,
            sequence,
            role,
            content,
            agent_type=agent_type,
            linked_log_id=linked_log_id,
            run_id=run_id,
        )
    db.execute(
        "UPDATE groups SET last_message_sequence=37 WHERE id=?",
        (ctx["group_id"],),
    )
    return ctx


def _save_fixed_segments(db, ctx):
    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    CollaborationStateSegmentService.save_finalization_segments(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_segments=[
            {
                "state": "positive_collaboration",
                "start_message_id": 3,
                "end_message_id": 6,
                "evidence_message_ids": [3, 4, 5, 6],
                "confidence": 0.91,
            },
            {
                "state": "off_task",
                "start_message_id": 11,
                "end_message_id": 14,
                "evidence_message_ids": [11, 12, 14],
                "confidence": 0.86,
            },
            {
                "state": "frustration_stuck",
                "start_message_id": 19,
                "end_message_id": 23,
                "evidence_message_ids": [19, 20, 21, 23],
                "confidence": 0.83,
            },
            {
                "state": "conflict_tension",
                "start_message_id": 24,
                "end_message_id": 26,
                "evidence_message_ids": [24, 25, 26],
                "confidence": 0.88,
            },
            {
                "state": "positive_collaboration",
                "start_message_id": 27,
                "end_message_id": 36,
                "evidence_message_ids": [27, 28, 31, 33, 36],
                "confidence": 0.93,
            },
        ],
        source_run_id=606,
        analysis_anchor_message_id=3,
        analysis_window_start_message_id=1,
        analysis_window_end_message_id=36,
        prompt_version="batch6_fixed_stream_e2e",
    )
    CollaborationStateSegmentService.save_or_update_silence_interval(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        previous_student_message_id=14,
        next_student_message_id=15,
        start_at=_ts(14),
        end_at=_ts(15),
        is_finalized=True,
    )


def test_batch6_fixed_37_message_stream_teacher_review_and_trend(db_and_app):
    db, _app, _client = db_and_app
    watched_tables = [
        "messages",
        "monitor_runs",
        "state_assessments",
        "collaboration_state_segments",
        "collaboration_state_finalizations",
        "intervention_runs",
        "intervention_logs",
        "agent_research_events",
    ]
    before_counts = {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in watched_tables
    }
    ctx = _seed_fixed_stream(db)
    _save_fixed_segments(db, ctx)
    after_counts = {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in watched_tables
    }

    from services.teacher_emotion_review_service import get_emotion_review
    from services.teacher_emotion_trend_service import get_emotion_trend

    review = get_emotion_review(
        ctx["group_id"],
        session_id=ctx["session_id"],
        start_time=_ts(0),
        end_time=_ts(40),
        window_minutes=5,
    )
    trend = get_emotion_trend(
        ctx["group_id"],
        session_id=ctx["session_id"],
        start_time=_ts(0),
        end_time=_ts(40),
        window_minutes=5,
    )

    by_sequence = {message["sequence"]: message for message in review["messages"]}
    assert len(by_sequence) == 37

    observing_sequences = {1, 2, 7, 8, 9, 10, 15, 17, 18}
    for sequence, message in by_sequence.items():
        if message["role"] != "student":
            continue
        state = "observing" if sequence in observing_sequences else "unclassified"
        assert by_sequence[sequence]["role"] == "student"
        assert by_sequence[sequence]["state_code"] == state

    for sequence in (13, 16, 22, 32, 37):
        assert by_sequence[sequence]["role"] == "agent"
        assert by_sequence[sequence]["state_code"] is None

    assert by_sequence[13]["agent_display_label"] == "策略智能体 · 自动介入"
    assert by_sequence[16]["agent_display_label"] == "情绪智能体"
    assert by_sequence[22]["agent_display_label"] == "策略智能体 · 学生求助"
    assert by_sequence[32]["agent_display_label"] == "情绪智能体"
    assert by_sequence[37]["agent_display_label"] == "情绪智能体"

    silence = review["silence_segments"][0]
    assert silence["state_code"] == "negative_silence"
    assert silence["previous_student_message_id"] == 14
    assert silence["next_student_message_id"] == 15
    assert silence["duration_seconds"] == 240

    from services.three_stage_schema import FINAL_SUB_STATE_CODES

    assert [item["code"] for item in trend["state_system"]] == [
        *FINAL_SUB_STATE_CODES,
        "observing",
        "unclassified",
    ]
    assert trend["distribution"]["unclassified"]["segment_count"] == 5
    assert trend["distribution"]["unclassified"]["message_count"] == 23
    assert trend["distribution"]["observing"]["message_count"] == 9
    assert (
        trend["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 13
    )
    assert trend["coarse_distribution"]["conflict_tension"]["message_count"] == 3
    assert trend["coarse_distribution"]["blocked_frustration"]["message_count"] == 4
    assert trend["coarse_distribution"]["task_detached"]["message_count"] == 3
    assert "negative_silence" not in trend["distribution"]
    assert trend["summary"]["latest_state_code"] == "unclassified"
    assert review["quality_warnings"] == []
    assert {
        table: after_counts[table] - before_counts[table]
        for table in watched_tables
    } == {
        "messages": 37,
        "monitor_runs": 0,
        "state_assessments": 0,
        "collaboration_state_segments": 6,
        "collaboration_state_finalizations": 0,
        "intervention_runs": 2,
        "intervention_logs": 2,
        "agent_research_events": 0,
    }


def test_batch6_group_session_isolation_with_same_sequences(db_and_app):
    db, _app, _client = db_and_app
    ctx_a = _seed_fixed_stream(db)
    _save_fixed_segments(db, ctx_a)

    ctx_b = seed_running_session(db, session_no=602, member_count=2, limit_minutes=20)
    _insert_message(db, ctx_b, 1, "student", "另一个课次同样从 1 开始。")
    _insert_message(db, ctx_b, 2, "student", "这个小组只有任务脱离片段。")

    from services.collaboration_state_segment_service import CollaborationStateSegmentService
    from services.teacher_emotion_review_service import get_emotion_review

    CollaborationStateSegmentService.save_finalization_segments(
        group_id=ctx_b["group_id"],
        session_id=ctx_b["session_id"],
        session_no=ctx_b["session_no"],
        task_id=ctx_b["task_id"],
        state_segments=[
            {
                "state": "off_task",
                "start_message_id": 1,
                "end_message_id": 2,
                "evidence_message_ids": [1, 2],
                "confidence": 0.8,
            }
        ],
        source_run_id=607,
        analysis_anchor_message_id=1,
        analysis_window_start_message_id=1,
        analysis_window_end_message_id=2,
        prompt_version="batch6_isolation",
    )

    review_a = get_emotion_review(
        ctx_a["group_id"],
        session_id=ctx_a["session_id"],
        start_time=_ts(0),
        end_time=_ts(40),
    )
    review_b = get_emotion_review(
        ctx_b["group_id"],
        session_id=ctx_b["session_id"],
        start_time=_ts(0),
        end_time=_ts(40),
    )

    assert {message["session_id"] for message in review_a["messages"]} == {ctx_a["session_id"]}
    assert {message["session_id"] for message in review_b["messages"]} == {ctx_b["session_id"]}
    assert review_a["distribution"]["unclassified"]["message_count"] == 23
    assert (
        review_a["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 13
    )
    assert review_a["coarse_distribution"]["task_detached"]["message_count"] == 3
    assert review_b["distribution"]["unclassified"]["message_count"] == 2
    assert (
        review_b["coarse_distribution"]["positive_collaboration"][
            "message_count"
        ]
        == 0
    )
    assert review_b["coarse_distribution"]["task_detached"]["message_count"] == 2
