# -*- coding: utf-8 -*-
"""Focused orchestration contract tests for automatic strategy review."""

import json

from tests.helpers import (
    attach_state_assessment_to_monitor,
    seed_running_session,
)


def _enable_strategy_session(db, session_id):
    db.execute(
        """
        UPDATE experiment_sessions
        SET agent_mode='strategy',
            strategy_agent_enabled=1,
            emotion_agent_enabled=0,
            agent_intervention_enabled=1
        WHERE id=?
        """,
        (session_id,),
    )


def _monitor_with_assessment(
    db,
    seeded,
    *,
    cutoff_sequence,
    state_code="conflict_tension",
    confidence=0.86,
):
    rule_result = {
        "winning_state_code": state_code,
        "winning_score": confidence,
        "assessment_status": "state_detected",
        "trigger_sequence": cutoff_sequence,
        "evidence_sequences": [cutoff_sequence],
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
            seeded["group_id"],
            cutoff_sequence,
            "new_message",
            "{}",
            state_code,
            confidence,
            "completed",
            "contract_test",
            db.now_str(),
            db.now_str(),
        ),
    )
    assessment_id = attach_state_assessment_to_monitor(
        db,
        monitor_run_id,
        group_id=seeded["group_id"],
        session_id=seeded["session_id"],
        task_id=seeded["task_id"],
        session_no=seeded["session_no"],
        state_code=state_code,
        confidence=confidence,
        cutoff_sequence=cutoff_sequence,
    )
    db.execute(
        "UPDATE monitor_runs SET rule_result_json=? WHERE id=?",
        (json.dumps({**rule_result, "monitor_audit": {"state_assessment_id": assessment_id}}), monitor_run_id),
    )
    return monitor_run_id, assessment_id


def _patch_pass_review(monkeypatch):
    import services.intervention_pipeline_v2.intervention_service as service_module

    calls = []

    def fake_review(context):
        calls.append(context)
        return {
            "ok": True,
            "decision": "PASS",
            "strategy": None,
            "student_message": "",
            "teacher_reason": "confirmed state exists, but no student-facing interruption is needed.",
            "profile": "strategy_review_decision",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True},
            "validation": {"valid": True},
        }

    monkeypatch.setattr(service_module, "review_strategy_context", fake_review)
    return calls


def test_same_assessment_retry_is_idempotent_but_new_assessment_same_state_runs(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=71, member_count=1)
    _enable_strategy_session(db, seeded["session_id"])
    student_id = seeded["students"][0][0]

    first = db.create_message(seeded["group_id"], student_id, "We disagree about the first plan.", role="student")
    first_monitor, first_assessment = _monitor_with_assessment(db, seeded, cutoff_sequence=first["sequence"])
    calls = _patch_pass_review(monkeypatch)

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))

    first_result = InterventionService.execute(first_monitor)
    retry_result = InterventionService.execute(first_monitor)
    assert first_result["steps"]["pass_recorded"] is True
    assert retry_result["existing_status"] == "PASS"
    assert retry_result["existing_id"] == first_result["intervention_run_id"]
    assert len(calls) == 1

    second = db.create_message(seeded["group_id"], student_id, "We still disagree about the evidence.", role="student")
    second_monitor, second_assessment = _monitor_with_assessment(db, seeded, cutoff_sequence=second["sequence"])
    second_result = InterventionService.execute(second_monitor)

    assert second_assessment != first_assessment
    assert second_result["steps"]["pass_recorded"] is True
    rows = db.query_all(
        "SELECT state_assessment_id, status FROM intervention_runs WHERE group_id=? ORDER BY id",
        (seeded["group_id"],),
    )
    assert [dict(row) for row in rows] == [
        {"state_assessment_id": first_assessment, "status": "PASS"},
        {"state_assessment_id": second_assessment, "status": "PASS"},
    ]


def test_cooldown_rejection_records_skipped_without_llm(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = seed_running_session(db, session_no=72, member_count=1)
    _enable_strategy_session(db, seeded["session_id"])
    student_id = seeded["students"][0][0]
    msg = db.create_message(seeded["group_id"], student_id, "We are arguing again.", role="student")
    monitor_run_id, _assessment_id = _monitor_with_assessment(db, seeded, cutoff_sequence=msg["sequence"])
    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, cutoff_sequence, agent_type, trigger_type, status,
            session_id, session_no, task_id, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"],
            0,
            "strategy",
            "auto_state",
            "PUBLISHED",
            seeded["session_id"],
            seeded["session_no"],
            seeded["task_id"],
            db.now_str(),
            db.now_str(),
        ),
    )

    import services.intervention_pipeline_v2.intervention_service as service_module
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    monkeypatch.setattr(
        service_module,
        "review_strategy_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cooldown should skip before LLM")),
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["skipped"] is True
    assert "cooldown_active" in result["reason"]
    run = db.query_one(
        """
        SELECT status, decision, skip_reason, cooldown_result
        FROM intervention_runs
        WHERE monitor_run_id=?
        """,
        (monitor_run_id,),
    )
    assert run["status"] == "SKIPPED"
    assert run["decision"] == "SKIPPED"
    assert "cooldown_active" in run["skip_reason"]
    assert run["cooldown_result"] is not None


def test_session_ended_and_locked_document_skip_before_llm(db_and_app, monkeypatch):
    db, _app, _client = db_and_app

    import services.intervention_pipeline_v2.intervention_service as service_module
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    monkeypatch.setattr(
        service_module,
        "review_strategy_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("gate skip should happen before LLM")),
    )

    ended = seed_running_session(db, session_no=73, member_count=1)
    _enable_strategy_session(db, ended["session_id"])
    student_id = ended["students"][0][0]
    ended_msg = db.create_message(ended["group_id"], student_id, "We are stuck.", role="student")
    ended_monitor, _ = _monitor_with_assessment(db, ended, cutoff_sequence=ended_msg["sequence"])
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE id=?", (ended["session_id"],))

    ended_result = InterventionService.execute(ended_monitor)
    assert ended_result["skipped"] is True
    assert ended_result["reason"] == "session_not_active"

    locked = seed_running_session(db, session_no=74, member_count=1)
    _enable_strategy_session(db, locked["session_id"])
    student_id = locked["students"][0][0]
    db.execute(
        """
        INSERT INTO collaborative_documents(
            group_id, task_id, session_no, title, status, created_by, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            locked["group_id"],
            locked["task_id"],
            locked["session_no"],
            "Locked doc",
            "locked",
            student_id,
            db.now_str(),
            db.now_str(),
        ),
    )
    locked_msg = db.create_message(locked["group_id"], student_id, "We are stuck again.", role="student")
    locked_monitor, _ = _monitor_with_assessment(db, locked, cutoff_sequence=locked_msg["sequence"])

    locked_result = InterventionService.execute(locked_monitor)
    assert locked_result["skipped"] is True
    assert locked_result["reason"] == "document_locked"

    skipped = db.query_all(
        "SELECT status, decision, skip_reason FROM intervention_runs WHERE group_id IN (?, ?) ORDER BY group_id",
        (ended["group_id"], locked["group_id"]),
    )
    assert [dict(row) for row in skipped] == [
        {"status": "SKIPPED", "decision": "SKIPPED", "skip_reason": "session_not_active"},
        {"status": "SKIPPED", "decision": "SKIPPED", "skip_reason": "document_locked"},
    ]
