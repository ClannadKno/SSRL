# -*- coding: utf-8 -*-
"""B3 coverage for learning-assistant strategy and prompt gating."""

from tests.helpers import attach_state_assessment_to_monitor, create_group, create_student, seed_running_session


def test_b3_strategy_candidates_only_for_formal_intervention_states():
    from services.intervention_pipeline_v2.strategy_service import (
        FORMAL_INTERVENTION_STATES,
        StrategyService,
    )

    assert StrategyService.validate_all_strategies() == []

    for state_code in FORMAL_INTERVENTION_STATES:
        candidates = StrategyService.find_strategies_for_state(state_code)
        assert candidates, state_code
        assert all(state_code in item["applicable_states"] for item in candidates)
        assert all(item["strategy_type"] == "active_intervention" for item in candidates)
        assert all(item["cooldown_seconds"] == 120 for item in candidates)
        assert all(item["fallback_template"] for item in candidates)

    for state_code in [
        "positive_collaboration",
        "unknown",
        "participation_imbalance",
        "coordination_disorder",
        "conflict_repair",
        "positive_recovery",
        "insufficient_evidence",
    ]:
        assert StrategyService.find_strategies_for_state(state_code) == []


def test_b3_validator_blocks_observation_states_and_enforces_120s_cooldown(db_and_app):
    db, _app_module, _client = db_and_app
    group_id = create_group(db, name="B3 Gate Group", code="G-B3-GATE")
    db.execute("UPDATE groups SET last_message_sequence=?, cutoff_sequence=? WHERE id=?", (5, 5, group_id))

    from services.intervention_pipeline_v2.intervention_validator import InterventionValidator

    passive = InterventionValidator.validate(
        group_id,
        5,
        {"final_state": "positive_collaboration", "trigger_type": "new_message"},
    )
    assert passive["valid"] is False
    assert passive["triggerable_state_check"]["reason"] == "non_intervention_state_positive_collaboration"

    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, status, created_at, completed_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (group_id, 4, "strategy", "PUBLISHED", db.now_str(), db.now_str()),
    )

    cooled = InterventionValidator.validate(
        group_id,
        5,
        {"final_state": "conflict_tension", "trigger_type": "new_message"},
    )
    assert cooled["valid"] is False
    assert "cooldown_active_1" in cooled["reason"]
    assert cooled["cooldown_check"]["cooldown_seconds"] == 120

    help_triggered = InterventionValidator.validate(
        group_id,
        5,
        {"final_state": "conflict_tension", "trigger_type": "student_help_request"},
    )
    assert help_triggered["valid"] is True
    assert help_triggered["cooldown_check"]["bypassed_by"] == "student_help_request"
    assert help_triggered["trigger_source"] == "student_help_request"


def test_b3_auto_intervention_preserves_student_help_trigger_source(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app

    seeded = seed_running_session(db, session_no=3, member_count=1)
    group_id = seeded["group_id"]
    student_id = seeded["students"][0][0]
    db.execute(
        """
        UPDATE experiment_sessions
        SET agent_mode='strategy',
            strategy_agent_enabled=1,
            emotion_agent_enabled=0,
            agent_intervention_enabled=1
        WHERE id=?
        """,
        (seeded["session_id"],),
    )
    db.create_message(
        group_id,
        student_id,
        "我们对方案依据有冲突，请学习助手帮忙。",
        role="student",
        client_message_id="b3-help-trigger",
    )
    db.execute("UPDATE groups SET cutoff_sequence=last_message_sequence WHERE id=?", (group_id,))
    cutoff = db.query_one("SELECT last_message_sequence FROM groups WHERE id=?", (group_id,))[
        "last_message_sequence"
    ]

    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, status, created_at, completed_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (group_id, cutoff - 1, "strategy", "PUBLISHED", db.now_str(), db.now_str()),
    )
    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, final_state, confidence,
            status, analyzer_version, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            cutoff,
            "student_help_request",
            "conflict_tension",
            0.86,
            "completed",
            "test",
            db.now_str(),
            db.now_str(),
        ),
    )
    attach_state_assessment_to_monitor(
        db,
        monitor_run_id,
        group_id=group_id,
        session_id=seeded["session_id"],
        task_id=seeded["task_id"],
        session_no=seeded["session_no"],
        state_code="conflict_tension",
        confidence=0.86,
        cutoff_sequence=cutoff,
    )

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    import services.intervention_pipeline_v2.intervention_service as service_module

    monkeypatch.setattr(
        service_module,
        "review_strategy_context",
        lambda context: {
            "ok": True,
            "decision": "INTERVENE",
            "strategy": "v2_conflict_evidence",
            "student_message": "先把不同观点各写一个依据，再比较哪些能合并。",
            "teacher_reason": "求助中出现依据冲突",
            "profile": "strategy_review_decision",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True},
            "validation": {"valid": True},
        },
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["validated"] is True
    assert result["validation"]["cooldown_check"]["bypassed_by"] == "student_help_request"
    assert result["steps"]["published"] is True

    run = db.query_one(
        "SELECT trigger_type, status FROM intervention_runs WHERE monitor_run_id=?",
        (monitor_run_id,),
    )
    assert dict(run) == {"trigger_type": "student_help_request", "status": "PUBLISHED"}

    log = db.query_one(
        "SELECT trigger_source, strategy_id FROM intervention_logs WHERE intervention_id=?",
        (result["intervention_run_id"],),
    )
    assert log["trigger_source"] == "student_help_request"
    assert log["strategy_id"].startswith("v2_conflict_")


def test_b3_legacy_strategy_selector_suppresses_positive_and_unknown(db_and_app):
    from agent.strategy import select_intervention_strategy

    context = {
        "group_id": 1,
        "recent_student_messages": [
            {"real_name": "A", "content": "我负责整理证据。"},
            {"real_name": "B", "content": "我补充方案比较。"},
        ],
    }

    for state_code in ["positive_collaboration", "unknown", "participation_imbalance"]:
        result = select_intervention_strategy({"state_code": state_code}, context, "experiment")
        assert result["should_intervene"] is False
        assert result["is_oi_suppressed"] == 1


def test_b3_student_help_flow_uses_student_help_request_log_source(db_and_app):
    db, _app_module, _client = db_and_app
    group_id = create_group(db, name="B3 Direct Help Group", code="G-B3-DIRECT")
    student_id, _login_key = create_student(db, group_id)

    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, status, created_at, completed_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (group_id, 0, "strategy", "PUBLISHED", db.now_str(), db.now_str()),
    )

    from services.student_help_service import request_student_help

    result = request_student_help(
        group_id,
        student_id,
        "请帮我们明确下一步",
        client_message_id="b3-direct-help",
    )

    assert result["ok"] is True
    log = db.query_one(
        "SELECT push_mode, trigger_source FROM intervention_logs WHERE id=?",
        (result["assistant_log_id"],),
    )
    assert dict(log) == {
        "push_mode": "student_request",
        "trigger_source": "student_help_request",
    }
