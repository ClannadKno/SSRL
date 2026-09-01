# -*- coding: utf-8 -*-

from tests.helpers import attach_state_assessment_to_monitor, seed_running_session


def _seed_help_window(db):
    context = seed_running_session(db, session_no=61, member_count=1, limit_minutes=20)
    group_id = context["group_id"]
    student_id = context["students"][0][0]
    source = db.create_message(
        group_id,
        student_id,
        "@sera we are stuck connecting evidence to the final plan",
        role="student",
        client_message_id="help-unified-source",
    )
    db.execute(
        """
        INSERT INTO group_states(
            group_id, state_code, state_label, state_score,
            evidence, session_no, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            "blocked_frustration",
            "Blocked frustration",
            0.88,
            "student asked for help",
            context["session_no"],
            context["task_id"],
            db.now_str(),
        ),
    )
    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, final_state, confidence,
            status, analyzer_version, session_id, task_id, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            source["sequence"],
            "student_help",
            "blocked_frustration",
            0.88,
            "completed",
            "test",
            context["session_id"],
            context["task_id"],
            db.now_str(),
            db.now_str(),
        ),
    )
    assessment_id = attach_state_assessment_to_monitor(
        db,
        monitor_run_id,
        group_id=group_id,
        session_id=context["session_id"],
        task_id=context["task_id"],
        session_no=context["session_no"],
        state_code="blocked_frustration",
        confidence=0.88,
        cutoff_sequence=source["sequence"],
    )
    help_request_id = db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, request_text, source_message_id, created_at
        ) VALUES(?,?,?,?,?,'QUEUED',?,?,?)
        """,
        (
            group_id,
            student_id,
            context["task_id"],
            context["session_no"],
            context["session_id"],
            "we are stuck connecting evidence to the final plan",
            source["id"],
            db.now_str(),
        ),
    )
    return context, source, monitor_run_id, assessment_id, help_request_id


def test_student_help_uses_unified_publish_chain_and_is_idempotent(db_and_app):
    db, _app_module, _client = db_and_app
    context, source, monitor_run_id, assessment_id, help_request_id = _seed_help_window(db)

    from agent.help_tasks import _execute_help_flow

    _execute_help_flow(help_request_id)

    help_row = db.query_one("SELECT * FROM help_requests WHERE id=?", (help_request_id,))
    assert help_row["status"] == "COMPLETED_WITH_FALLBACK"
    assert help_row["response_message_id"] is not None
    assert help_row["intervention_run_id"] is not None

    messages = db.query_all(
        """
        SELECT id, trigger_source, agent_type, intervention_run_id, session_id, task_id
          FROM messages
         WHERE role='agent' AND group_id=?
        """,
        (context["group_id"],),
    )
    assert len(messages) == 1
    msg = dict(messages[0])
    assert msg["id"] == help_row["response_message_id"]
    assert msg["trigger_source"] == "student_help_request"
    assert msg["agent_type"] == "strategy"
    assert msg["session_id"] == context["session_id"]
    assert msg["task_id"] == context["task_id"]

    run = db.query_one(
        "SELECT * FROM intervention_runs WHERE id=?",
        (help_row["intervention_run_id"],),
    )
    assert run["status"] == "FALLBACK"
    assert run["trigger_type"] == "student_help_request"
    assert run["help_request_id"] == help_request_id
    assert run["state_assessment_id"] == assessment_id
    assert run["monitor_run_id"] == monitor_run_id
    assert run["message_id"] == msg["id"]
    assert run["session_id"] == context["session_id"]
    assert run["task_id"] == context["task_id"]

    log = db.query_one(
        "SELECT * FROM intervention_logs WHERE intervention_run_id=?",
        (run["id"],),
    )
    assert log["trigger_source"] == "student_help_request"
    assert log["help_request_id"] == help_request_id
    assert log["message_id"] == msg["id"]
    assert log["session_id"] == context["session_id"]
    assert log["task_id"] == context["task_id"]

    before = {
        "messages": db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"],
        "runs": db.query_one("SELECT COUNT(*) AS c FROM intervention_runs WHERE help_request_id=?", (help_request_id,))["c"],
        "logs": db.query_one("SELECT COUNT(*) AS c FROM intervention_logs WHERE help_request_id=?", (help_request_id,))["c"],
    }
    _execute_help_flow(help_request_id)
    after = {
        "messages": db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"],
        "runs": db.query_one("SELECT COUNT(*) AS c FROM intervention_runs WHERE help_request_id=?", (help_request_id,))["c"],
        "logs": db.query_one("SELECT COUNT(*) AS c FROM intervention_logs WHERE help_request_id=?", (help_request_id,))["c"],
    }
    assert after == before

    from services.teacher_agent_audit_service import get_agent_audit

    audit = get_agent_audit(context["group_id"], context["session_id"], blinded=False)
    assert audit["stats"]["student_help_reply_count"] == 1
    assert audit["stats"]["actual_intervention_count"] == 1
    agent_message = next(item for item in audit["message_timeline"] if item["role"] == "agent")
    assert agent_message["agent_message_kind"] == "strategy_student_help"
    assert agent_message["agent_display_label"] == "策略智能体 · 学生求助"
    assert agent_message["agent_trigger_source"] == "student_help_request"


def test_auto_state_skip_for_help_window_is_audited_but_not_counted(db_and_app):
    db, _app_module, _client = db_and_app
    context, source, _help_monitor_id, _help_assessment_id, _help_request_id = _seed_help_window(db)

    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, final_state, confidence,
            status, analyzer_version, session_id, task_id, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            source["sequence"],
            "student_message",
            "blocked_frustration",
            0.9,
            "completed",
            "test",
            context["session_id"],
            context["task_id"],
            db.now_str(),
            db.now_str(),
        ),
    )
    assessment_id = attach_state_assessment_to_monitor(
        db,
        monitor_run_id,
        group_id=context["group_id"],
        session_id=context["session_id"],
        task_id=context["task_id"],
        session_no=context["session_no"],
        state_code="blocked_frustration",
        confidence=0.9,
        cutoff_sequence=source["sequence"],
    )

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    result = InterventionService.record_skipped_for_monitor(
        monitor_run_id,
        state_assessment_id=assessment_id,
        group_id=context["group_id"],
        session_id=context["session_id"],
        task_id=context["task_id"],
        cutoff_sequence=source["sequence"],
        trigger_source="auto_state",
        reason="pending_help_request",
    )

    assert result["skipped"] is True
    run = db.query_one(
        "SELECT status, decision, skip_reason, trigger_type, message_id FROM intervention_runs WHERE id=?",
        (result["intervention_run_id"],),
    )
    assert dict(run) == {
        "status": "SKIPPED",
        "decision": "SKIPPED",
        "skip_reason": "pending_help_request",
        "trigger_type": "auto_state",
        "message_id": None,
    }
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0

    from services.teacher_agent_audit_service import get_agent_audit

    audit = get_agent_audit(context["group_id"], context["session_id"], blinded=False)
    skipped = [
        item for item in audit["interventions"]
        if item.get("intervention_run_id") == result["intervention_run_id"]
    ]
    assert skipped
    assert skipped[0]["student_visible_message"] is False
    assert skipped[0]["agent_message_kind"] == "strategy_auto"
    assert audit["stats"]["actual_intervention_count"] == 0


def test_legacy_strategy_message_without_trigger_source_is_not_auto(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=62, member_count=1, limit_minutes=20)
    sera = db.query_one("SELECT id FROM users WHERE username='sera'")["id"]
    db.create_message(
        context["group_id"],
        sera,
        "legacy agent text",
        role="agent",
        sender_type="agent",
        client_message_id="legacy-agent",
    )
    db.execute("UPDATE messages SET agent_type='strategy' WHERE client_message_id='legacy-agent'")

    from services.teacher_agent_audit_service import get_agent_audit

    audit = get_agent_audit(context["group_id"], context["session_id"], blinded=False)
    agent_message = next(item for item in audit["message_timeline"] if item["role"] == "agent")
    assert agent_message["agent_message_kind"] == "legacy_agent"
    assert agent_message["agent_display_label"] == "Agent · legacy/未知来源"
    assert audit["stats"]["strategy_auto_intervention_count"] == 0
    assert audit["stats"]["actual_intervention_count"] == 0
