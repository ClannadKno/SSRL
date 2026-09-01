# -*- coding: utf-8 -*-
"""Regression coverage for strategy-review timeout retry and idempotency."""

from __future__ import annotations

import json

from tests.helpers import attach_state_assessment_to_monitor, seed_running_session


def _seed_strategy_monitor(db, *, state_code="conflict_tension"):
    seeded = seed_running_session(db, session_no=41, member_count=1)
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
        "我们现在卡住了，也不知道该听谁的依据。",
        role="student",
        client_message_id=f"strategy-retry-{state_code}",
    )
    db.execute("UPDATE groups SET cutoff_sequence=last_message_sequence WHERE id=?", (group_id,))
    cutoff = db.query_one("SELECT last_message_sequence FROM groups WHERE id=?", (group_id,))[
        "last_message_sequence"
    ]
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
            "new_message",
            state_code,
            0.86,
            "completed",
            "test",
            db.now_str(),
            db.now_str(),
        ),
    )
    assessment_id = attach_state_assessment_to_monitor(
        db,
        monitor_run_id,
        group_id=group_id,
        session_id=seeded["session_id"],
        task_id=seeded["task_id"],
        session_no=seeded["session_no"],
        state_code=state_code,
        confidence=0.86,
        cutoff_sequence=cutoff,
    )
    return {
        **seeded,
        "monitor_run_id": monitor_run_id,
        "assessment_id": assessment_id,
        "cutoff": cutoff,
    }


def _patch_service(monkeypatch, responses):
    from services.intervention_pipeline_v2.intervention_service import InterventionService
    import services.intervention_pipeline_v2.intervention_service as service_module

    calls = []
    queue = list(responses)

    def fake_review(_context):
        calls.append(_context)
        item = queue.pop(0)
        if callable(item):
            return item()
        return item

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    monkeypatch.setattr(service_module, "review_strategy_context", fake_review)
    monkeypatch.setattr(service_module.time, "sleep", lambda _seconds: None)
    return calls


def _pass_result():
    return {
        "ok": True,
        "decision": "PASS",
        "strategy": None,
        "strategy_id": None,
        "student_message": "",
        "message": None,
        "teacher_reason": "暂不打断。",
        "reason": "暂不打断。",
        "profile": "strategy_review_and_generation",
        "prompt_version": "strategy_review_decision_v3",
        "payload": {"messages": []},
        "llm_result": {"success": True, "attempt_count": 1},
        "validation": {"valid": True},
    }


def _intervene_result():
    return {
        "ok": True,
        "decision": "INTERVENE",
        "strategy": "v2_conflict_evidence",
        "strategy_id": "v2_conflict_evidence",
        "student_message": "先把各自依据说清楚，再决定采用哪个方案。",
        "message": "先把各自依据说清楚，再决定采用哪个方案。",
        "teacher_reason": "出现依据冲突。",
        "reason": "出现依据冲突。",
        "profile": "strategy_review_and_generation",
        "prompt_version": "strategy_review_decision_v3",
        "payload": {"messages": []},
        "llm_result": {"success": True, "attempt_count": 1},
        "validation": {"valid": True},
    }


def _read_timeout_result():
    return {
        "ok": False,
        "reason": "read_timeout",
        "profile": "strategy_review_and_generation",
        "prompt_version": "strategy_review_decision_v3",
        "payload": {"messages": []},
        "llm_result": {
            "success": False,
            "failure_type": "read_timeout",
            "retryable": True,
            "attempt_count": 1,
        },
    }


def test_strategy_review_intervene_publishes_once_and_releases_lock(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)
    calls = _patch_service(monkeypatch, [_intervene_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    result = InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 1
    assert result["published"] is True
    run = db.query_one("SELECT id, status, decision, message_id FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert run["status"] == "PUBLISHED"
    assert run["decision"] == "INTERVENE"
    assert run["message_id"] is not None
    messages = db.query_all("SELECT intervention_run_id FROM messages WHERE group_id=? AND role='agent'", (seeded["group_id"],))
    assert len(messages) == 1
    assert messages[0]["intervention_run_id"] == run["id"]
    group = db.query_one("SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?", (seeded["group_id"],))
    assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}


def test_strategy_review_pass_does_not_publish_and_releases_lock(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)
    calls = _patch_service(monkeypatch, [_pass_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    result = InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 1
    assert result["published"] is False
    run = db.query_one("SELECT status, decision, message_id FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert dict(run) == {"status": "PASS", "decision": "PASS", "message_id": None}
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (seeded["group_id"],))["c"] == 0
    assert db.query_one("SELECT state FROM groups WHERE id=?", (seeded["group_id"],))["state"] == "OPEN"


def test_strategy_review_read_timeout_retry_then_publish_once(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)
    calls = _patch_service(monkeypatch, [_read_timeout_result(), _intervene_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    result = InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 2
    assert result["published"] is True
    run = db.query_one("SELECT status, generator_params_json FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert run["status"] == "PUBLISHED"
    params = json.loads(run["generator_params_json"])
    assert params["attempt_count"] == 2
    assert params["attempts"][0]["timeout_type"] == "read_timeout"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (seeded["group_id"],))["c"] == 1
    assert db.query_one("SELECT state FROM groups WHERE id=?", (seeded["group_id"],))["state"] == "OPEN"


def test_strategy_review_two_timeouts_fail_without_publish(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)
    calls = _patch_service(monkeypatch, [_read_timeout_result(), _read_timeout_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    result = InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 2
    assert result["steps"]["failed_without_student_message"] is True
    run = db.query_one("SELECT status, failure_reason, generator_params_json FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert run["status"] == "FAILED"
    assert run["failure_reason"] == "read_timeout"
    assert json.loads(run["generator_params_json"])["attempt_count"] == 2
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (seeded["group_id"],))["c"] == 0
    assert db.query_one("SELECT state FROM groups WHERE id=?", (seeded["group_id"],))["state"] == "OPEN"


def test_strategy_review_retry_rechecks_session_before_second_call(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)

    def first_timeout_then_end_session():
        db.execute("UPDATE experiment_sessions SET status='ended' WHERE id=?", (seeded["session_id"],))
        return _read_timeout_result()

    calls = _patch_service(monkeypatch, [first_timeout_then_end_session, _intervene_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    result = InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 1
    assert result["steps"]["retry_cancelled"] is True
    run = db.query_one("SELECT status, decision, skip_reason FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert run["status"] == "SKIPPED"
    assert run["decision"] == "SKIPPED"
    assert run["skip_reason"]
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (seeded["group_id"],))["c"] == 0
    assert db.query_one("SELECT state FROM groups WHERE id=?", (seeded["group_id"],))["state"] == "OPEN"


def test_strategy_review_invalid_json_allows_one_fix_retry(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)
    invalid_json = {
        "ok": False,
        "reason": "invalid_json",
        "profile": "strategy_review_and_generation",
        "payload": {"messages": []},
        "llm_result": {"success": True, "retryable": None},
    }
    calls = _patch_service(monkeypatch, [invalid_json, _pass_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 2
    run = db.query_one("SELECT status, decision, generator_params_json FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert run["status"] == "PASS"
    assert run["decision"] == "PASS"
    assert json.loads(run["generator_params_json"])["attempt_count"] == 2


def test_strategy_review_non_retryable_auth_error_fails_immediately(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded = _seed_strategy_monitor(db)
    auth_error = {
        "ok": False,
        "reason": "authentication_error",
        "profile": "strategy_review_and_generation",
        "payload": {"messages": []},
        "llm_result": {
            "success": False,
            "failure_type": "authentication_error",
            "retryable": False,
            "status_code": 401,
        },
    }
    calls = _patch_service(monkeypatch, [auth_error, _intervene_result()])

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    InterventionService.execute(seeded["monitor_run_id"])

    assert len(calls) == 1
    run = db.query_one("SELECT status, failure_reason FROM intervention_runs WHERE monitor_run_id=?", (seeded["monitor_run_id"],))
    assert run["status"] == "FAILED"
    assert run["failure_reason"] == "authentication_error"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (seeded["group_id"],))["c"] == 0


def test_emotion_tick_duplicate_uses_structured_tick_index(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=42, member_count=1)
    db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, task_id, cutoff_sequence, agent_type,
            trigger_type, status, tick_index, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"],
            seeded["session_id"],
            seeded["task_id"],
            0,
            "emotion",
            "scheduled_10min",
            "PUBLISHED",
            3,
            db.now_str(),
            db.now_str(),
        ),
    )

    from agent.emotion_tasks import _is_duplicate_tick

    assert _is_duplicate_tick(seeded["group_id"], seeded["session_id"], 3) is True
    assert _is_duplicate_tick(seeded["group_id"], seeded["session_id"], 4) is False
