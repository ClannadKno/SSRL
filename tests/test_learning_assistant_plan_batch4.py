# -*- coding: utf-8 -*-
"""Batch 4 regressions for negative-silence strategy scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.helpers import seed_running_session


def _ts(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _start_discussion(db, context: dict, now: datetime) -> None:
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    runtime = enter_group_discussion_stage(
        context["session_id"],
        context["group_id"],
        context["students"][0][0],
    )
    db.execute(
        """
        UPDATE group_session_discussions
        SET status='running', started_at=?, deadline=?, updated_at=?
        WHERE id=?
        """,
        (
            _ts(now - timedelta(minutes=10)),
            _ts(now + timedelta(minutes=10)),
            _ts(now),
            runtime["id"],
        ),
    )
    db.execute(
        """
        UPDATE experiment_sessions
        SET strategy_agent_enabled=1
        WHERE id=?
        """,
        (context["session_id"],),
    )


def _message(
    db,
    context: dict,
    *,
    sequence: int,
    created_at: datetime,
    role: str = "student",
    agent_type: str = None,
) -> int:
    message_id = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, agent_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["students"][0][0],
            "我们先想一想。" if role == "student" else "给小组的支持消息。",
            sequence,
            role,
            role,
            context["session_no"],
            context["task_id"],
            context["session_id"],
            agent_type,
            _ts(created_at),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=MAX(COALESCE(last_message_sequence, 0), ?)
        WHERE id=?
        """,
        (sequence, context["group_id"]),
    )
    return message_id


def _negative_silence_rule() -> dict:
    return {
        "version": "plan_batch4",
        "assessment_status": "state_detected",
        "winning_state_code": "negative_silence",
        "winning_state_label": "消极沉默",
        "winning_score": 0.9,
        "candidates": [
            {
                "state_code": "negative_silence",
                "score": 0.9,
                "signals": ["silent_seconds=185"],
            }
        ],
    }


def test_first_threshold_schedules_one_strategy_run_per_silence_event(
    db_and_app,
    monkeypatch,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=1001, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    message_at = now - timedelta(seconds=185)
    _message(
        db,
        context,
        sequence=7,
        created_at=message_at,
    )

    import services.discussion_pipeline_v2.monitoring_service as monitoring

    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring, "AUTO_INTERVENTION_V2_ENABLED", True)
    monkeypatch.setattr(
        monitoring.RuleDetector,
        "detect",
        staticmethod(lambda _context, _features: _negative_silence_rule()),
    )
    monkeypatch.setattr(
        monitoring.TriggerPolicy,
        "should_enqueue_strategy_review",
        staticmethod(
            lambda *_args, **_kwargs: (
                True,
                "state_review_negative_silence_confidence_0.90",
            )
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(
            lambda monitor_run_id, **kwargs: (
                scheduled.append({"monitor_run_id": monitor_run_id, **kwargs})
                or {"enqueued": True}
            )
        ),
    )

    from agent.monitoring_tasks import check_room_silence

    first = check_room_silence.call_local(
        context["group_id"],
        7,
        _ts(message_at),
        context["session_id"],
        context["task_id"],
    )
    duplicate = check_room_silence.call_local(
        context["group_id"],
        7,
        _ts(message_at),
        context["session_id"],
        context["task_id"],
    )

    assert first["fused_state"] == "negative_silence"
    assert first["stage1_result"]["coarse_decision"] == "ESCALATE"
    assert first["stage1_result"]["coarse_state_code"] == "POSSIBLE_SILENCE"
    assert first["stage1_lock_result"]["acquired"] is False
    assert first["stage1_lock_result"]["reason"] == "PRELIMINARY_NO_LOCK"
    assert first["stage1_terminal_result"] == {
        "terminalized": True,
        "pipeline_run_id": first["stage1_result"]["pipeline_run_id"],
        "stage2_status": "SKIPPED",
        "publish_status": "SKIPPED",
        "final_status": "SKIPPED",
        "reason": "SILENCE_STAGE1_NO_STAGE2_CONSUMER",
    }
    assert duplicate["reason"] == "duplicate_cutoff"
    assert scheduled == []
    rows = db.query_all(
        """
        SELECT * FROM collaboration_state_segments
        WHERE group_id=? AND state_code='negative_silence'
        """,
        (context["group_id"],),
    )
    assert len(rows) == 1
    segment = rows[0]
    assert segment["intervention_scheduled_at"] is None
    assert segment["intervention_disposition"] is None
    pipeline = db.query_one(
        """
        SELECT stage2_status, publish_status, final_status
        FROM strategy_pipeline_runs
        WHERE group_id=?
        """,
        (context["group_id"],),
    )
    assert pipeline["stage2_status"] == "SKIPPED"
    assert pipeline["publish_status"] == "SKIPPED"
    assert pipeline["final_status"] == "SKIPPED"


def test_silence_precheck_enforces_switch_session_cooldown_and_room_lock(
    db_and_app,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=1002, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    _message(
        db,
        context,
        sequence=10,
        created_at=now - timedelta(seconds=185),
    )

    from services.discussion_pipeline_v2.monitoring_service import (
        _silence_strategy_precheck,
    )

    kwargs = {
        "group_id": context["group_id"],
        "session_id": context["session_id"],
        "session_no": context["session_no"],
        "task_id": context["task_id"],
        "cutoff_sequence": 10,
        "segment_id": None,
        "final_state": "negative_silence",
    }

    db.execute(
        "UPDATE groups SET auto_intervention_enabled=0 WHERE id=?",
        (context["group_id"],),
    )
    disabled = _silence_strategy_precheck(**kwargs)
    assert disabled == {
        "allowed": False,
        "reason": "group_auto_intervention_disabled",
    }

    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1 WHERE id=?",
        (context["group_id"],),
    )
    db.execute(
        "UPDATE group_session_discussions SET status='closed' WHERE group_id=?",
        (context["group_id"],),
    )
    inactive = _silence_strategy_precheck(**kwargs)
    assert inactive["allowed"] is False

    db.execute(
        "UPDATE group_session_discussions SET status='running' WHERE group_id=?",
        (context["group_id"],),
    )
    cooldown_run = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, detected_state, status, agent_type,
            trigger_type, session_id, session_no, task_id, created_at, published_at
        ) VALUES(?,?,'task_detached','PUBLISHED','strategy','auto_state',?,?,?,?,?)
        """,
        (
            context["group_id"],
            8,
            context["session_id"],
            context["session_no"],
            context["task_id"],
            _ts(now),
            _ts(now),
        ),
    )
    cooling = _silence_strategy_precheck(**kwargs)
    assert cooling["allowed"] is False
    assert "cooldown_active" in cooling["reason"]

    db.execute("DELETE FROM intervention_runs WHERE id=?", (cooldown_run,))
    db.execute(
        "UPDATE groups SET state='AI_INTERVENING' WHERE id=?",
        (context["group_id"],),
    )
    locked = _silence_strategy_precheck(**kwargs)
    assert locked["allowed"] is False


def test_silence_cutoff_ignores_agent_messages_but_detects_student_resume(
    db_and_app,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=1003, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    _message(
        db,
        context,
        sequence=10,
        created_at=now - timedelta(seconds=185),
    )
    _message(
        db,
        context,
        sequence=11,
        created_at=now - timedelta(seconds=5),
        role="agent",
        agent_type="emotion",
    )

    from services.intervention_pipeline_v2.intervention_validator import (
        InterventionValidator,
    )

    monitor = {
        "trigger_type": "silence_rule",
        "final_state": "negative_silence",
    }
    agent_only = InterventionValidator._check_cutoff_sequence(
        context["group_id"],
        10,
        monitor,
    )
    assert agent_only["ok"] is True
    assert agent_only["delta"] == 0
    assert agent_only["cutoff_scope"] == "latest_student_message"

    _message(
        db,
        context,
        sequence=12,
        created_at=now,
    )
    resumed = InterventionValidator._check_cutoff_sequence(
        context["group_id"],
        10,
        monitor,
    )
    assert resumed["ok"] is True
    assert resumed["delta"] == 2


def test_strategy_prompt_payload_contains_silence_duration_and_task_context(
    db_and_app,
):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=1004, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    message_at = now - timedelta(seconds=185)
    _message(
        db,
        context,
        sequence=41,
        created_at=message_at,
    )

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )
    from services.intervention_pipeline_v2.context_builder import ContextBuilder
    from services.intervention_pipeline_v2.strategy_review_service import (
        _prompt_safe_context,
    )

    segment = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=context["group_id"],
        expected_sequence=41,
        expected_session_id=context["session_id"],
        expected_task_id=context["task_id"],
        source_run_id=987,
        now_value=_ts(now),
    )
    built = ContextBuilder.build_strategy_review_context(
        group_id=context["group_id"],
        session_id=context["session_id"],
        monitor_run_id=987,
        cutoff_sequence=41,
        candidate_strategies=[],
        state_assessment={
            "id": 654,
            "fused_state_code": "negative_silence",
            "assessment_status": "confirmed",
            "confidence": 0.9,
        },
        trigger_source="silence_rule",
    )
    payload = _prompt_safe_context(built)

    assert payload["task_context"]["task"]["task_id"] == context["task_id"]
    silence = payload["runtime_context"]["silence"]
    assert silence["segment_id"] == segment["segment_id"]
    assert silence["trigger_sequence"] == 41
    assert silence["silent_seconds"] >= 185
    assert silence["last_student_message_at"] == _ts(message_at)
    assert silence["student_has_resumed"] is False


def test_intervention_run_is_linked_back_to_the_silence_event(db_and_app):
    db, _app, _client = db_and_app
    context = seed_running_session(db, session_no=1005, member_count=1)
    now = datetime.now().replace(microsecond=0)
    _start_discussion(db, context, now)
    _message(
        db,
        context,
        sequence=51,
        created_at=now - timedelta(seconds=185),
    )

    from services.collaboration_state_segment_service import (
        CollaborationStateSegmentService,
    )
    from services.intervention_pipeline_v2.intervention_run_repo import (
        InterventionRunRepo,
    )

    segment = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=context["group_id"],
        expected_sequence=51,
        source_run_id=111,
        now_value=_ts(now),
    )
    claim = CollaborationStateSegmentService.claim_silence_intervention(
        segment_id=segment["segment_id"],
        monitor_run_id=111,
        now_value=_ts(now),
    )
    run_id = InterventionRunRepo.create(
        group_id=context["group_id"],
        monitor_run_id=111,
        cutoff_sequence=51,
        detected_state="negative_silence",
        confidence=0.9,
        dry_run=False,
        trigger_type="silence_rule",
        session_id=context["session_id"],
        task_id=context["task_id"],
        target_segment_id=segment["segment_id"],
    )

    assert claim["claimed"] is True
    pending = db.query_one(
        "SELECT * FROM collaboration_state_segments WHERE id=?",
        (segment["segment_id"],),
    )
    assert pending["intervention_run_id"] == run_id
    assert pending["intervention_disposition"] == "PENDING"

    InterventionRunRepo.mark_pass(run_id, teacher_reason="PASS")
    completed = db.query_one(
        "SELECT * FROM collaboration_state_segments WHERE id=?",
        (segment["segment_id"],),
    )
    assert completed["intervention_run_id"] == run_id
    assert completed["intervention_disposition"] == "PASS"
