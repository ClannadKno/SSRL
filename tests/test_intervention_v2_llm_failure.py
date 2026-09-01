# -*- coding: utf-8 -*-
"""Regression coverage for automatic strategy-review LLM failures."""

import json

from tests.helpers import attach_state_assessment_to_monitor, seed_running_session


def test_auto_intervention_llm_failure_does_not_publish_template(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app

    seeded = seed_running_session(db, session_no=1, member_count=1)
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
    db.create_message(group_id, student_id, "我们都不知道下一步怎么做。", role="student")
    db.execute(
        "UPDATE groups SET last_message_sequence=?, cutoff_sequence=? WHERE id=?",
        (1, 1, group_id),
    )
    rule_result = {
        "winning_state_code": "negative_silence",
        "winning_score": 0.85,
        "assessment_status": "state_detected",
        "trigger_sequence": 1,
    }
    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, rule_result_json,
            final_state, confidence, status, analyzer_version,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            1,
            "new_message",
            json.dumps(rule_result, ensure_ascii=False),
            "negative_silence",
            0.85,
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
        state_code="negative_silence",
        confidence=0.85,
        cutoff_sequence=1,
    )

    from services.intervention_pipeline_v2.intervention_service import InterventionService
    import services.intervention_pipeline_v2.intervention_service as service_module

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    monkeypatch.setattr(
        service_module,
        "review_strategy_context",
        lambda context: {
            "ok": False,
            "reason": "llm_call_failed",
            "profile": "strategy_review_decision",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": False, "failure_type": "llm_call_failed"},
            "validation": None,
        },
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["llm_called"] is True
    assert result["steps"]["failed_without_student_message"] is True
    assert result["fallback_used"] is False

    run = db.query_one(
        """
        SELECT status, generated_message, fallback_used, fallback_template, failure_reason
        FROM intervention_runs
        WHERE monitor_run_id=?
        """,
        (monitor_run_id,),
    )
    assert run["status"] == "FAILED"
    assert run["generated_message"] is None
    assert int(run["fallback_used"] or 0) == 0
    assert run["fallback_template"] is None
    assert run["failure_reason"] == "llm_call_failed"

    message_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (group_id,),
    )["c"]
    assert message_count == 0

    group = db.query_one(
        """
        SELECT state, lock_token, lock_expires_at, active_intervention_run_id
        FROM groups
        WHERE id=?
        """,
        (group_id,),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["lock_expires_at"] is None
    assert group["active_intervention_run_id"] is None
