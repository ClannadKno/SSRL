# -*- coding: utf-8 -*-
"""Batch 4 coverage for the unified automatic strategy review pipeline."""

import json

from tests.helpers import seed_running_session


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


def _seed_monitor_run(db, *, state_code="conflict_tension"):
    seeded = seed_running_session(db, session_no=1, member_count=2)
    _enable_strategy_session(db, seeded["session_id"])
    group_id = seeded["group_id"]
    student_1 = seeded["students"][0][0]
    student_2 = seeded["students"][1][0]
    db.create_message(group_id, student_1, "我觉得要先列比较标准。", role="student")
    db.create_message(group_id, student_2, "你这个方案完全没依据。", role="student")
    db.execute(
        "UPDATE groups SET cutoff_sequence=?, last_message_sequence=? WHERE id=?",
        (2, 2, group_id),
    )
    rule_result = {
        "winning_state_code": state_code,
        "winning_score": 0.82,
        "assessment_status": "state_detected",
        "signals": {"recent_conflict_detected": True},
        "trigger_sequence": 2,
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
            2,
            "new_message",
            json.dumps(rule_result, ensure_ascii=False),
            state_code,
            0.82,
            "completed",
            "test",
            db.now_str(),
            db.now_str(),
        ),
    )
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no,
            fused_state_code, assessment_status, confidence, should_intervene,
            evidence_summary, fusion_json, rule_assessment_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            seeded["session_id"],
            seeded["task_id"],
            seeded["session_no"],
            state_code,
            "confirmed",
            0.82,
            1 if state_code not in {"positive_collaboration", "unknown"} else 0,
            "state assessment from monitoring",
            json.dumps({"decision_source": "state_llm"}, ensure_ascii=False),
            json.dumps({"evidence_sequences": [1, 2]}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    rule_result["monitor_audit"] = {"state_assessment_id": assessment_id}
    db.execute(
        "UPDATE monitor_runs SET rule_result_json=? WHERE id=?",
        (json.dumps(rule_result, ensure_ascii=False), monitor_run_id),
    )

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=group_id,
        session_id=seeded["session_id"],
        session_no=seeded["session_no"],
        task_id=seeded["task_id"],
        state_code=state_code,
        start_message_id=1,
        end_message_id=2,
        evidence_message_ids=[1, 2],
        confidence=0.82,
        source_run_id=monitor_run_id,
        assessment_id=assessment_id,
        analysis_window_start_message_id=1,
        analysis_window_end_message_id=2,
    )
    return seeded, monitor_run_id


def _patch_review(monkeypatch, review_result):
    import services.intervention_pipeline_v2.intervention_service as service_module

    monkeypatch.setattr(
        service_module,
        "review_strategy_context",
        lambda context: dict(review_result),
    )


def test_unified_strategy_review_pass_records_and_unlocks(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded, monitor_run_id = _seed_monitor_run(db)
    group_id = seeded["group_id"]

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    _patch_review(
        monkeypatch,
        {
            "ok": True,
            "action": "PASS",
            "decision": "PASS",
            "strategy": None,
            "student_message": "",
            "teacher_reason": "任务内合理推进，暂不打断。",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "latency_ms": 12},
            "validation": {"valid": True},
        },
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["llm_called"] is True
    assert result["steps"]["pass_recorded"] is True
    assert result["published"] is False
    assert result["state_segments"]["reason"] == "state_segments_owned_by_monitoring"

    run = db.query_one(
        """
        SELECT status, decision, skip_reason, generated_message, teacher_reason,
               llm_response_json, context_from_sequence, context_to_sequence,
               evidence_sequences_json
        FROM intervention_runs WHERE monitor_run_id=?
        """,
        (monitor_run_id,),
    )
    assert run["status"] == "PASS"
    assert run["decision"] == "PASS"
    assert run["skip_reason"] == "pass_no_intervention"
    assert run["generated_message"] is None
    assert run["teacher_reason"] == "任务内合理推进，暂不打断。"
    assert run["llm_response_json"] is not None
    assert run["context_from_sequence"] == 1
    assert run["context_to_sequence"] == 2
    assert json.loads(run["evidence_sequences_json"]) == [1, 2]

    monitor = db.query_one(
        """
        SELECT review_decision, review_final_state, review_reason,
               evidence_sequences_json, generated_message
        FROM monitor_runs WHERE id=?
        """,
        (monitor_run_id,),
    )
    assert monitor["review_decision"] == "PASS"
    assert monitor["review_final_state"] == "conflict_tension"
    assert monitor["review_reason"] == "任务内合理推进，暂不打断。"
    assert json.loads(monitor["evidence_sequences_json"]) == [1, 2]
    assert monitor["generated_message"] is None

    segment = db.query_one(
        """
        SELECT state_code, source, analysis_anchor_message_id, is_finalized
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        """,
        (group_id, seeded["session_id"]),
    )
    assert dict(segment) == {
        "state_code": "conflict_tension",
        "source": "state_monitor",
        "analysis_anchor_message_id": 1,
        "is_finalized": 1,
    }
    assert db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM collaboration_state_segments
        WHERE group_id=? AND source='strategy_llm'
        """,
        (group_id,),
    )["c"] == 0

    message_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (group_id,),
    )["c"]
    assert message_count == 0

    group = db.query_one(
        "SELECT state, lock_token, lock_expires_at, active_intervention_run_id, last_intervention_at FROM groups WHERE id=?",
        (group_id,),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["lock_expires_at"] is None
    assert group["active_intervention_run_id"] is None
    assert group["last_intervention_at"] is None


def test_unified_strategy_review_intervene_publishes_and_starts_cooldown(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded, monitor_run_id = _seed_monitor_run(db)
    group_id = seeded["group_id"]
    message = "先不判断谁对谁错，请把不同观点各写出一个依据再比较。"

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    _patch_review(
        monkeypatch,
        {
            "ok": True,
            "action": "INTERVENE",
            "decision": "INTERVENE",
            "strategy": "v2_conflict_evidence",
            "student_message": message,
            "teacher_reason": "最新发言转向否定。",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "latency_ms": 18},
            "validation": {"valid": True},
        },
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["llm_called"] is True
    assert result["published"] is True
    assert result["state_segments"]["reason"] == "state_segments_owned_by_monitoring"

    run = db.query_one(
        """
        SELECT id, status, decision, generated_message, selected_strategy_id,
               prompt_version, evidence_sequences_json, llm_response_json,
               actual_published_at, message_id
        FROM intervention_runs WHERE monitor_run_id=?
        """,
        (monitor_run_id,),
    )
    assert run["status"] == "PUBLISHED"
    assert run["decision"] == "INTERVENE"
    assert run["generated_message"] == message
    assert run["selected_strategy_id"] == "v2_conflict_evidence"
    assert run["prompt_version"] == "strategy_review_decision_v3"
    assert json.loads(run["evidence_sequences_json"]) == [1, 2]
    assert json.loads(run["llm_response_json"])["decision"] == "INTERVENE"
    assert run["actual_published_at"] is not None
    assert run["message_id"] is not None

    agent_message = db.query_one(
        """
        SELECT content, agent_type, strategy_id
        FROM messages
        WHERE group_id=? AND role='agent'
        ORDER BY id DESC LIMIT 1
        """,
        (group_id,),
    )
    assert agent_message["content"] == message
    assert agent_message["agent_type"] == "strategy"
    assert agent_message["strategy_id"] == "v2_conflict_evidence"

    segment = db.query_one(
        """
        SELECT state_code, source, analysis_anchor_message_id, is_finalized
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        """,
        (group_id, seeded["session_id"]),
    )
    assert dict(segment) == {
        "state_code": "conflict_tension",
        "source": "state_monitor",
        "analysis_anchor_message_id": 1,
        "is_finalized": 1,
    }
    assert db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM collaboration_state_segments
        WHERE group_id=? AND source='strategy_llm'
        """,
        (group_id,),
    )["c"] == 0

    log = db.query_one(
        "SELECT strategy_id, prompt_version FROM intervention_logs WHERE intervention_id=?",
        (run["id"],),
    )
    assert log["strategy_id"] == "v2_conflict_evidence"
    assert log["prompt_version"] == "strategy_review_decision_v3"

    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id, last_intervention_at FROM groups WHERE id=?",
        (group_id,),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["active_intervention_run_id"] is None
    assert group["last_intervention_at"] is not None


def test_unified_strategy_review_failure_does_not_publish_and_unlocks(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    seeded, monitor_run_id = _seed_monitor_run(db)
    group_id = seeded["group_id"]

    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    _patch_review(
        monkeypatch,
        {
            "ok": False,
            "action": "fail_without_student_message",
            "reason": "evidence_sequence_not_in_input",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True},
            "validation": {"valid": False, "reason": "evidence_sequence_not_in_input"},
        },
    )

    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["llm_called"] is True
    assert result["steps"]["failed_without_student_message"] is True
    assert result["reason"] == "evidence_sequence_not_in_input"

    run = db.query_one(
        "SELECT status, failure_reason, generated_message FROM intervention_runs WHERE monitor_run_id=?",
        (monitor_run_id,),
    )
    assert run["status"] == "FAILED"
    assert run["failure_reason"] == "evidence_sequence_not_in_input"
    assert run["generated_message"] is None
    assert db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM collaboration_state_segments
        WHERE group_id=? AND source='strategy_llm'
        """,
        (group_id,),
    )["c"] == 0

    message_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (group_id,),
    )["c"]
    assert message_count == 0

    group = db.query_one(
        "SELECT state, lock_token, lock_expires_at, active_intervention_run_id FROM groups WHERE id=?",
        (group_id,),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["lock_expires_at"] is None
    assert group["active_intervention_run_id"] is None
