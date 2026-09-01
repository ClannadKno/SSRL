# -*- coding: utf-8 -*-
"""Batch 2 coverage for three-stage Stage 1 rule screening."""

from __future__ import annotations

import json

from tests.helpers import create_group, create_student, seed_running_session


def _enable_stage1(monkeypatch, *, auto=False):
    import services.discussion_pipeline_v2.monitoring_service as monitoring

    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring, "AUTO_INTERVENTION_V2_ENABLED", bool(auto))
    return monitoring


def _start_discussion(db, ctx):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy',
               strategy_agent_enabled=1,
               emotion_agent_enabled=0,
               agent_intervention_enabled=1
         WHERE id=?
        """,
        (ctx["session_id"],),
    )
    runtime = None
    for student_id, _login_key in ctx["students"]:
        runtime = enter_group_discussion_stage(ctx["session_id"], ctx["group_id"], student_id)
    assert runtime["status"] == "running"
    ctx["discussion_id"] = runtime["id"]
    return ctx


def _new_group_in_session(db, ctx, *, member_count=2):
    group_id = create_group(db, name=f"Group {ctx['session_no']}-B", code=f"G{ctx['session_no']}B")
    students = [
        create_student(db, group_id, index=index + 1, username_prefix=f"s{ctx['session_no']}b")
        for index in range(member_count)
    ]
    group_ctx = {
        "session_id": ctx["session_id"],
        "session_no": ctx["session_no"],
        "task_id": ctx["task_id"],
        "group_id": group_id,
        "students": students,
    }
    return _start_discussion(db, group_ctx)


def _pipeline_rows(db, group_id):
    return db.query_all(
        """
        SELECT *
        FROM strategy_pipeline_runs
        WHERE group_id=?
        ORDER BY id
        """,
        (group_id,),
    )


def test_positive_stage1_persists_no_strong_intervention_without_lock(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    monitoring = _enable_stage1(monkeypatch, auto=True)
    ctx = _start_discussion(db, seed_running_session(db, session_no=9201, member_count=2))
    db.create_message(ctx["group_id"], ctx["students"][0][0], "我先整理材料中的证据。", role="student")
    latest = db.create_message(ctx["group_id"], ctx["students"][1][0], "我来补充比较标准，然后一起汇总。", role="student")

    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(lambda *_args, **_kwargs: scheduled.append((_args, _kwargs))),
    )

    result = monitoring.MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type="student_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    assert result["steps"]["completed"] is True
    assert scheduled == []
    stage1 = result["stage1_result"]
    assert stage1["coarse_decision"] == "NO_STRONG_INTERVENTION"
    assert stage1["requires_stage2"] is True
    assert stage1["requires_room_lock"] is False
    rows = _pipeline_rows(db, ctx["group_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["input_cutoff_student_sequence"] == latest["sequence"]
    assert row["coarse_decision"] == "NO_STRONG_INTERVENTION"
    assert row["coarse_state_code"] in {"POSSIBLE_POSITIVE", "UNKNOWN_COARSE"}
    assert row["room_lock_token"] is None
    assert db.query_one("SELECT state FROM groups WHERE id=?", (ctx["group_id"],))["state"] == "OPEN"
    quant = json.loads(row["coarse_quantitative_features_json"])
    assert quant["new_student_message_count"] >= 2
    assert quant["candidate_sub_states"]


def test_risk_stage1_stays_lock_free_and_never_publishes_agent_message(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    monitoring = _enable_stage1(monkeypatch, auto=True)
    ctx = _start_discussion(db, seed_running_session(db, session_no=9202, member_count=2))
    db.create_message(ctx["group_id"], ctx["students"][0][0], "你这个方案完全不行，别乱说。", role="student")
    latest = db.create_message(ctx["group_id"], ctx["students"][1][0], "你才错了，根本没有看证据。", role="student")

    def fail_direct_schedule(*_args, **_kwargs):
        raise AssertionError("stage1 must not schedule legacy intervention directly")

    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(fail_direct_schedule),
    )

    result = monitoring.MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type="student_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    assert result["stage1_result"]["coarse_decision"] == "ESCALATE"
    assert result["stage1_result"]["requires_room_lock"] is False
    assert result["stage1_direct_strategy_scheduling_blocked"] is True
    lock_result = result["stage1_lock_result"]
    assert lock_result["attempted"] is False
    assert lock_result["acquired"] is False
    assert lock_result["reason"] == "PRELIMINARY_NO_LOCK"
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (ctx["group_id"],),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["active_intervention_run_id"] is None
    row = _pipeline_rows(db, ctx["group_id"])[0]
    assert row["input_cutoff_student_sequence"] == latest["sequence"]
    assert row["coarse_should_escalate"] == 1
    assert row["room_lock_token"] is None
    assert row["room_lock_acquired_at"] is None
    assert row["final_status"] == "PENDING_STAGE2"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"] == 0


def test_student_help_stage1_is_urgent_and_does_not_steal_help_lock(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    monitoring = _enable_stage1(monkeypatch, auto=True)
    ctx = _start_discussion(db, seed_running_session(db, session_no=9203, member_count=1))
    latest = db.create_message(ctx["group_id"], ctx["students"][0][0], "@学习助手 我们需要帮忙明确下一步。", role="student")

    result = monitoring.MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type="student_help",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    stage1 = result["stage1_result"]
    assert stage1["coarse_decision"] == "URGENT_ESCALATE"
    assert stage1["coarse_state_code"] == "EXPLICIT_HELP"
    assert stage1["trigger_priority"] == 1
    assert result["stage1_lock_result"]["attempted"] is False
    assert result["stage1_lock_result"]["reason"] == "student_help_lock_owned_by_help_pipeline"
    group = db.query_one("SELECT state FROM groups WHERE id=?", (ctx["group_id"],))
    assert group["state"] == "OPEN"
    row = _pipeline_rows(db, ctx["group_id"])[0]
    assert row["trigger_message_id"] == latest["id"]
    assert row["coarse_decision"] == "URGENT_ESCALATE"


def test_duplicate_stage1_delivery_reuses_pipeline_run(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    monitoring = _enable_stage1(monkeypatch, auto=False)
    ctx = _start_discussion(db, seed_running_session(db, session_no=9204, member_count=1))
    db.create_message(ctx["group_id"], ctx["students"][0][0], "我先看材料。", role="student")

    first = monitoring.MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type="student_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )
    second = monitoring.MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type="student_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    assert first["monitor_run_id"] is not None
    assert second["skipped"] is True
    assert second["reason"] == "duplicate_cutoff"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM strategy_pipeline_runs WHERE group_id=?",
        (ctx["group_id"],),
    )["c"] == 1


def test_first_stage_task_requests_background_stage2_for_non_intervention(monkeypatch, db_and_app):
    _db, _app, _client = db_and_app
    from agent import monitoring_tasks
    import services.discussion_pipeline_v2.monitoring_service as monitoring
    import services.state_assessment_scheduler as scheduler

    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(
            lambda **_kwargs: {
                "monitor_run_id": 11,
                "rule_winning_state": "positive_collaboration",
                "state_detector_gate": {"gate": False},
                "stage1_result": {
                    "coarse_decision": "NO_STRONG_INTERVENTION",
                    "requires_stage2": True,
                },
            }
        ),
    )
    requests = []
    monkeypatch.setattr(
        scheduler,
        "request_state_assessment_for_message",
        lambda **kwargs: requests.append(kwargs) or {"created": True, "enqueued": True},
    )

    result = monitoring_tasks.process_new_message_task.call_local(321, 9, "student_message")

    assert requests == [
        {
            "group_id": 321,
            "sequence": 9,
            "trigger_type": "message_count_periodic",
        }
    ]
    assert result["state_assessment_request"]["enqueued"] is True


def test_parallel_preliminary_stage1_runs_leave_both_groups_open(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    monitoring = _enable_stage1(monkeypatch, auto=True)
    ctx_a = _start_discussion(db, seed_running_session(db, session_no=9205, member_count=2))
    ctx_b = _new_group_in_session(db, ctx_a, member_count=2)

    db.create_message(ctx_a["group_id"], ctx_a["students"][0][0], "你这个完全不行。", role="student")
    db.create_message(ctx_a["group_id"], ctx_a["students"][1][0], "你也没看懂证据。", role="student")
    db.create_message(ctx_b["group_id"], ctx_b["students"][0][0], "这个方案不合理。", role="student")
    db.create_message(ctx_b["group_id"], ctx_b["students"][1][0], "你的判断没有依据。", role="student")

    first = monitoring.MonitoringService.run_detection(
        ctx_a["group_id"],
        trigger_type="student_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )
    second = monitoring.MonitoringService.run_detection(
        ctx_b["group_id"],
        trigger_type="student_message",
        allow_state_llm=False,
        persist_state_segment=True,
        schedule_strategy=True,
    )

    assert first["stage1_lock_result"]["reason"] == "PRELIMINARY_NO_LOCK"
    assert second["stage1_lock_result"]["reason"] == "PRELIMINARY_NO_LOCK"
    groups = {
        row["id"]: row
        for row in db.query_all(
            "SELECT id, state, lock_token FROM groups WHERE id IN (?,?) ORDER BY id",
            (ctx_a["group_id"], ctx_b["group_id"]),
        )
    }
    assert groups[ctx_a["group_id"]]["state"] == "OPEN"
    assert groups[ctx_b["group_id"]]["state"] == "OPEN"
    assert groups[ctx_a["group_id"]]["lock_token"] is None
    assert groups[ctx_b["group_id"]]["lock_token"] is None
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM strategy_pipeline_runs WHERE session_id=?",
        (ctx_a["session_id"],),
    )["c"] == 2
