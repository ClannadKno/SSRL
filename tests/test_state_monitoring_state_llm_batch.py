# -*- coding: utf-8 -*-
"""Batch coverage for conditional state-detector monitoring."""

import json

from tests.helpers import login_with_key, seed_running_session


def _enable_state_llm(monkeypatch):
    import services.discussion_pipeline_v2.monitoring_service as monitoring_service
    import services.discussion_pipeline_v2.trigger_policy as trigger_policy
    import services.discussion_pipeline_v2.llm_state_detector as detector

    monkeypatch.setattr(monitoring_service, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(trigger_policy, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(trigger_policy, "STATE_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)


def _mock_state_detector(monkeypatch, state_code, *, confidence=0.82, evidence_ids=None, meta=None, calls=None):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    calls = calls if calls is not None else []

    def fake_detect(context, rule_assessment=None, features=None):
        calls.append(
            {
                "context": context,
                "rule_assessment": rule_assessment,
                "features": features,
            }
        )
        return {
            "result": {
                "primary_state": state_code,
                "state_code": state_code,
                "confidence": confidence,
                "evidence_message_ids": list(evidence_ids or []),
                "secondary_state": None,
                "reason": "mocked state detector",
            },
            "meta": meta
            or {
                "analysis_skipped": False,
                "analysis_failed": False,
                "llm_required": True,
                "success": True,
                "validation_status": "passed",
                "schema_valid": True,
                "model_name": "mock-state-detector",
                "prompt_version": "mock-state-v1",
            },
        }

    monkeypatch.setattr(detector.LLMStateDetector, "detect", staticmethod(fake_detect))
    return calls


def _state_segments(db, group_id):
    return db.query_all(
        """
        SELECT state_code, segment_kind, start_message_id, end_message_id,
               evidence_message_ids_json, source, assessment_id, source_run_id,
               is_finalized
        FROM collaboration_state_segments
        WHERE group_id=?
        ORDER BY id
        """,
        (group_id,),
    )


def test_student_message_routes_queue_regular_and_help_monitoring(db_and_app, monkeypatch):
    db, _app_module, client = db_and_app
    context = seed_running_session(db, session_no=81, member_count=1)
    user_id, login_key = context["students"][0]
    headers = login_with_key(client, login_key)

    import services.discussion_pipeline_v2.monitoring_service as monitoring_service
    import routes.api as api_routes
    import agent.help_tasks as help_tasks

    monitoring_calls = []
    scheduled_help = []
    monkeypatch.setattr(api_routes, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(
        monitoring_service.MonitoringService,
        "process_new_message",
        staticmethod(lambda **kwargs: monitoring_calls.append(kwargs)),
    )
    monkeypatch.setattr(
        help_tasks.process_student_help,
        "schedule",
        lambda *args, **kwargs: scheduled_help.append((args, kwargs)),
    )

    regular = client.post(
        "/api/message",
        json={"group_id": context["group_id"], "content": "我们先分工整理证据。"},
        headers=headers,
    )
    assert regular.status_code == 200
    assert monitoring_calls[-1]["trigger_type"] == "student_message"

    help_response = client.post(
        "/api/message",
        json={"group_id": context["group_id"], "content": "@学习助手 帮我们明确下一步"},
        headers=headers,
    )
    assert help_response.status_code == 200
    assert help_response.json["help_request_detected"] is True
    assert monitoring_calls[-1]["trigger_type"] == "student_help"
    assert len(scheduled_help) == 1

    help_count = db.query_one(
        "SELECT COUNT(*) AS c FROM help_requests WHERE group_id=?",
        (context["group_id"],),
    )["c"]
    assert help_count == 1


def test_direct_student_help_api_also_enters_monitoring(db_and_app, monkeypatch):
    db, _app_module, client = db_and_app
    context = seed_running_session(db, session_no=82, member_count=1)
    _user_id, login_key = context["students"][0]
    headers = login_with_key(client, login_key)

    import services.discussion_pipeline_v2.monitoring_service as monitoring_service
    import routes.api as api_routes
    import agent.help_tasks as help_tasks

    monitoring_calls = []
    monkeypatch.setattr(api_routes, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(
        monitoring_service.MonitoringService,
        "process_new_message",
        staticmethod(lambda **kwargs: monitoring_calls.append(kwargs)),
    )
    monkeypatch.setattr(help_tasks.process_student_help, "schedule", lambda *args, **kwargs: None)

    response = client.post(
        "/api/student/help",
        json={"group_id": context["group_id"], "request_text": "请帮我们拆下一步"},
        headers=headers,
    )

    assert response.status_code == 202
    assert monitoring_calls[-1]["trigger_type"] == "student_help"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM help_requests WHERE group_id=?",
        (context["group_id"],),
    )["c"] == 1


def test_risk_rule_gate_calls_state_detector_and_persists_evidence(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    _enable_state_llm(monkeypatch)
    context = seed_running_session(db, session_no=83, member_count=2)
    group_id = context["group_id"]
    student_a = context["students"][0][0]
    student_b = context["students"][1][0]
    first = db.create_message(group_id, student_a, "卡住了，不知道怎么推进。", role="student")
    second = db.create_message(group_id, student_b, "证据不足，结论连不起来。", role="student")
    calls = _mock_state_detector(
        monkeypatch,
        "blocked_frustration",
        confidence=0.87,
        evidence_ids=[first["id"], second["id"]],
    )

    from services.discussion_pipeline_v2.monitoring_service import MonitoringService

    result = MonitoringService.run_detection(group_id, trigger_type="student_message")

    assert result["llm_called"] is True
    assert len(calls) == 1
    assert result["fused_state"] == "blocked_frustration"
    detector_ids = calls[0]["context"]["state_detector_allowed_evidence_message_ids"]
    assert first["id"] in detector_ids and second["id"] in detector_ids

    assessment = db.query_one(
        """
        SELECT llm_state_code, fused_state_code, llm_assessment_json, fusion_json
        FROM state_assessments
        WHERE group_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (group_id,),
    )
    assert assessment["llm_state_code"] == "blocked_frustration"
    assert assessment["fused_state_code"] == "blocked_frustration"
    llm_payload = json.loads(assessment["llm_assessment_json"])
    assert llm_payload["result"]["evidence_message_ids"] == [first["id"], second["id"]]
    assert json.loads(assessment["fusion_json"])["decision_source"] == "state_llm"

    segments = _state_segments(db, group_id)
    assert len(segments) == 1
    assert segments[0]["state_code"] == "blocked_frustration"
    assert segments[0]["segment_kind"] == "message_range"
    assert segments[0]["start_message_id"] == 1
    assert segments[0]["end_message_id"] == 2
    assert json.loads(segments[0]["evidence_message_ids_json"]) == [1, 2]
    assert segments[0]["source"] == "state_monitor"
    assert segments[0]["assessment_id"] is not None
    assert segments[0]["source_run_id"] == result["monitor_run_id"]
    assert segments[0]["is_finalized"] == 1


def test_periodic_unknown_after_four_messages_runs_positive_confirmation(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    _enable_state_llm(monkeypatch)
    context = seed_running_session(db, session_no=84, member_count=2)
    group_id = context["group_id"]
    student_a = context["students"][0][0]
    student_b = context["students"][1][0]
    messages = [
        db.create_message(group_id, student_a, "我看到了材料。", role="student"),
        db.create_message(group_id, student_b, "我也打开了。", role="student"),
        db.create_message(group_id, student_a, "先确认一下题目。", role="student"),
        db.create_message(group_id, student_b, "嗯，等下开始写。", role="student"),
    ]
    calls = _mock_state_detector(
        monkeypatch,
        "positive_collaboration",
        confidence=0.76,
        evidence_ids=[messages[0]["id"], messages[-1]["id"]],
    )

    from services.discussion_pipeline_v2.monitoring_service import MonitoringService

    result = MonitoringService.run_detection(group_id, trigger_type="student_message")

    assert result["llm_called"] is True
    assert result["state_detector_gate"]["gate_reason"] == "periodic_unknown_confirmation"
    assert len(calls) == 1
    assert result["fused_state"] == "positive_collaboration"

    segments = _state_segments(db, group_id)
    assert len(segments) == 1
    assert segments[0]["state_code"] == "positive_collaboration"
    assert segments[0]["segment_kind"] == "message_range"
    assert segments[0]["start_message_id"] == 1
    assert segments[0]["end_message_id"] == 4
    assert json.loads(segments[0]["evidence_message_ids_json"]) == [1, 4]
    assert segments[0]["source"] == "state_monitor"


def test_llm_invalid_state_degrades_to_rule_without_auto_intervention(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    _enable_state_llm(monkeypatch)
    context = seed_running_session(db, session_no=85, member_count=2)
    group_id = context["group_id"]
    student_a = context["students"][0][0]
    student_b = context["students"][1][0]
    db.create_message(group_id, student_a, "你这个想法不行。", role="student")
    db.create_message(group_id, student_b, "你错了，别乱说。", role="student")
    calls = _mock_state_detector(
        monkeypatch,
        "unknown",
        confidence=0.0,
        evidence_ids=[],
        meta={
            "analysis_skipped": False,
            "analysis_failed": True,
            "llm_required": True,
            "success": False,
            "validation_status": "failed",
            "schema_valid": False,
            "schema_error": "invalid_state",
            "failure_reason": "invalid_state",
            "failure_type": "invalid_state",
            "failure_message": "bad primary_state",
            "fallback_required": True,
        },
    )

    import services.discussion_pipeline_v2.monitoring_service as monitoring_service

    monkeypatch.setattr(monitoring_service, "AUTO_INTERVENTION_V2_ENABLED", True)
    scheduled = []
    monkeypatch.setattr(
        monitoring_service.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(lambda run_id: scheduled.append(run_id) or {"enqueued": True}),
    )

    result = monitoring_service.MonitoringService.run_detection(group_id, trigger_type="student_message")

    assert len(calls) == 1
    assert result["fused_state"] == "conflict_tension"
    assert scheduled == []
    run = db.query_one("SELECT rule_result_json FROM monitor_runs WHERE id=?", (result["monitor_run_id"],))
    audit = json.loads(run["rule_result_json"])["monitor_audit"]
    assert audit["decision_source"] == "rule_high_confidence_fallback"
    assert audit["invalid_state"] == "bad primary_state"

    segments = _state_segments(db, group_id)
    assert len(segments) == 1
    assert segments[0]["state_code"] == "conflict_tension"
    assert segments[0]["source"] == "state_monitor"


def test_student_help_trigger_saves_state_but_does_not_schedule_auto_strategy(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    _enable_state_llm(monkeypatch)
    context = seed_running_session(db, session_no=86, member_count=1)
    group_id = context["group_id"]
    student_id = context["students"][0][0]
    msg = db.create_message(group_id, student_id, "@学习助手 我们有冲突，需要帮忙。", role="student")
    _mock_state_detector(monkeypatch, "conflict_tension", confidence=0.86, evidence_ids=[msg["id"]])

    import services.discussion_pipeline_v2.monitoring_service as monitoring_service

    monkeypatch.setattr(monitoring_service, "AUTO_INTERVENTION_V2_ENABLED", True)
    scheduled = []
    monkeypatch.setattr(
        monitoring_service.MonitoringService,
        "_schedule_v2_intervention",
        staticmethod(lambda run_id: scheduled.append(run_id) or {"enqueued": True}),
    )

    result = monitoring_service.MonitoringService.run_detection(group_id, trigger_type="student_help")

    assert result["fused_state"] == "conflict_tension"
    assert scheduled == []
    run = db.query_one("SELECT rule_result_json FROM monitor_runs WHERE id=?", (result["monitor_run_id"],))
    audit = json.loads(run["rule_result_json"])["monitor_audit"]
    assert audit["help_request_strategy_blocked"] is True
    assert audit["help_request_strategy_block_reason"] == "student_help_request_trigger"
    assert audit["strategy_review_enqueued"] is False

    segments = _state_segments(db, group_id)
    assert len(segments) == 1
    assert segments[0]["state_code"] == "conflict_tension"
    assert segments[0]["source"] == "state_monitor"
    assert segments[0]["source_run_id"] == result["monitor_run_id"]


def test_same_student_cutoff_does_not_repeat_llm_or_state_write(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    _enable_state_llm(monkeypatch)
    context = seed_running_session(db, session_no=87, member_count=1)
    group_id = context["group_id"]
    student_id = context["students"][0][0]
    first_msg = db.create_message(group_id, student_id, "卡住了，不知道下一步。", role="student")
    second_msg = db.create_message(group_id, student_id, "还是没思路，证据也不够。", role="student")
    calls = _mock_state_detector(
        monkeypatch,
        "blocked_frustration",
        confidence=0.84,
        evidence_ids=[first_msg["id"], second_msg["id"]],
    )

    from services.discussion_pipeline_v2.monitoring_service import MonitoringService

    first = MonitoringService.run_detection(group_id, trigger_type="student_message")
    second = MonitoringService.run_detection(group_id, trigger_type="student_help")

    assert first["llm_called"] is True
    assert second["skipped"] is True
    assert second["reason"] == "duplicate_cutoff"
    assert len(calls) == 1
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM state_assessments WHERE group_id=?",
        (group_id,),
    )["c"] == 1
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_segments WHERE group_id=?",
        (group_id,),
    )["c"] == 1
