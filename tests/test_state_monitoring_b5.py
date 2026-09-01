# -*- coding: utf-8 -*-
"""B5 coverage for the teacher agent audit chain."""

import json
import csv
from io import StringIO
from datetime import datetime, timedelta

from tests.helpers import seed_running_session


BASE_TIME = datetime(2026, 7, 17, 11, 0, 0)


def _ts(offset_minutes):
    return (BASE_TIME + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _insert_b5_audit_records(db, context):
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, session_no, task_id,
            state_code, state_score,
            rule_result_json, llm_result_json, fusion_json,
            evidence, assessment_status, confidence,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            context["session_no"],
            context["task_id"],
            "coordination_disorder",
            0.77,
            json.dumps(
                {
                    "state_code": "coordination_disorder",
                    "evidence_tags": ["coordination_blocked", "no_next_step"],
                    "candidates": [
                        {"state_code": "blocked_frustration", "score": 0.77},
                        {"state_code": "positive_collaboration", "score": 0.12},
                    ],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "result": {
                        "state_code": "blocked_frustration",
                        "confidence": 0.74,
                        "evidence_tags": ["process_unclear"],
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "rule_state_code": "blocked_frustration",
                    "rule_score": 0.77,
                    "llm_state_code": "blocked_frustration",
                    "llm_confidence": 0.74,
                    "fused_state_code": "blocked_frustration",
                    "confidence": 0.8,
                    "decision_source": "rule_llm_agree",
                    "legacy_state_code": "coordination_disorder",
                    "normalization_reason": "legacy_coordination_disorder_normalized",
                    "evidence_tags": ["coordination_blocked", "no_next_step"],
                },
                ensure_ascii=False,
            ),
            "evidence_tags=coordination_blocked,no_next_step",
            "confirmed",
            0.8,
            _ts(0),
        ),
    )

    db.execute(
        """
        INSERT INTO intervention_decisions(
            assessment_id, group_id, session_id, should_intervene,
            decision_reason, suppressed_reason, target, priority,
            strategy_category, selected_strategy_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            assessment_id,
            context["group_id"],
            context["session_id"],
            0,
            "cooldown_active_1",
            "cooldown_active_1",
            "blocked_frustration",
            2,
            "active_intervention",
            "v2_blocked_role_clarity",
            _ts(1),
        ),
    )

    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, cutoff_sequence, agent_type, status,
            detected_state, confidence, trigger_type,
            candidate_strategies, selected_strategy, metadata_json,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            12,
            "strategy",
            "PUBLISHED",
            "blocked_frustration",
            0.82,
            "student_help_request",
            json.dumps(["v2_blocked_role_clarity"], ensure_ascii=False),
            json.dumps({"id": "v2_blocked_role_clarity"}, ensure_ascii=False),
            json.dumps(
                {
                    "trigger_source": "student_help_request",
                    "validation": {
                        "valid": True,
                        "action": "INTERVENE",
                        "cooldown_check": {
                            "ok": True,
                            "cooling": False,
                            "bypassed_by": "student_help_request",
                            "cooldown_seconds": 120,
                        },
                        "triggerable_state_check": {
                            "ok": True,
                            "state_code": "blocked_frustration",
                        },
                    },
                },
                ensure_ascii=False,
            ),
            _ts(2),
            _ts(2),
        ),
    )

    db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, intervention_id, push_mode, trigger_source,
            title, message, strategy_id, strategy_type,
            session_id, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            run_id,
            "student_request",
            "student_help_request",
            "明确下一步",
            "先确认谁记录，再各说一个下一步。",
            "v2_blocked_role_clarity",
            "active_intervention",
            context["session_id"],
            context["task_id"],
            _ts(2),
        ),
    )


def _insert_strategy_review_records(db, context):
    now = _ts(3)
    student_one = context["students"][0][0]
    student_two = context["students"][1][0]
    agent_id = db.execute(
        """
        INSERT INTO users(username, password_hash, real_name, participant_code, role, created_at)
        VALUES(?,?,?,?,?,?)
        """,
        ("sera_batch5", "x", "SERA", "SERA-B5", "agent", now),
    )
    other_session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (199, "discussion", context["task_id"], "ended", now, 20, now, now),
    )
    message_rows = [
        (student_one, "先列比较标准吧。", 1, "student", None, None, context["session_id"], None, None),
        (student_two, "这个方案好像没有证据。", 2, "student", None, None, context["session_id"], None, None),
        (agent_id, "大家已经把不同感受说出来了。", 3, "agent", "emotion", None, context["session_id"], None, None),
        (student_one, "我们还是卡在谁负责整理。", 4, "student", None, None, context["session_id"], None, None),
        (student_two, "跨课次消息，不应该被当前证据跳转。", 9, "student", None, None, other_session_id, None, None),
    ]
    for user_id, content, sequence, role, agent_type, run_id, session_id, linked_log_id, strategy_id in message_rows:
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, role, sender_type,
                session_no, task_id, session_id, agent_type,
                intervention_run_id, linked_log_id, strategy_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                context["group_id"],
                user_id,
                content,
                sequence,
                role,
                role,
                context["session_no"],
                context["task_id"],
                session_id,
                agent_type,
                run_id,
                linked_log_id,
                strategy_id,
                _ts(sequence),
            ),
        )

    pass_monitor_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, session_id, cutoff_sequence, trigger_type, rule_result_json,
            final_state, confidence, status, context_from_sequence,
            context_to_sequence, input_message_sequences_json,
            evidence_sequences_json, review_decision, review_final_state,
            review_confidence, review_reason, selected_strategy_id,
            generated_message, prompt_version, review_started_at,
            review_completed_at, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            2,
            "new_message",
            json.dumps({"state_code": "task_detached", "score": 0.62}, ensure_ascii=False),
            "task_detached",
            0.62,
            "completed",
            1,
            2,
            json.dumps([1, 2, 9], ensure_ascii=False),
            json.dumps([1, 9], ensure_ascii=False),
            "PASS",
            "positive_collaboration",
            0.71,
            "任务内推进",
            None,
            None,
            "strategy_review_and_generation_v1",
            _ts(10),
            _ts(10),
            _ts(9),
            _ts(10),
        ),
    )

    intervene_monitor_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, session_id, cutoff_sequence, trigger_type, rule_result_json,
            final_state, confidence, status, context_from_sequence,
            context_to_sequence, input_message_sequences_json,
            evidence_sequences_json, review_decision, review_final_state,
            review_confidence, review_reason, selected_strategy_id,
            generated_message, prompt_version, review_started_at,
            review_completed_at, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            4,
            "new_message",
            json.dumps({"state_code": "blocked_frustration", "score": 0.84}, ensure_ascii=False),
            "blocked_frustration",
            0.84,
            "completed",
            1,
            4,
            json.dumps([1, 2, 3, 4], ensure_ascii=False),
            json.dumps([2, 4], ensure_ascii=False),
            "INTERVENE",
            "blocked_frustration",
            0.88,
            "仍卡在分工",
            "v2_blocked_role_clarity",
            "先确认谁负责整理，再各说一个下一步。",
            "strategy_review_and_generation_v1",
            _ts(11),
            _ts(11),
            _ts(11),
            _ts(11),
        ),
    )

    run_id = db.execute(
        """
        INSERT INTO intervention_runs(
            monitor_run_id, group_id, session_id, cutoff_sequence, status, agent_type,
            trigger_type, detected_state, confidence, context_from_sequence,
            context_to_sequence, input_message_sequences_json,
            evidence_sequences_json, selected_strategy_id, generated_message,
            prompt_version, actual_started_at, actual_published_at,
            created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            intervene_monitor_id,
            context["group_id"],
            context["session_id"],
            4,
            "PUBLISHED",
            "strategy",
            "new_message",
            "blocked_frustration",
            0.88,
            1,
            4,
            json.dumps([1, 2, 3, 4], ensure_ascii=False),
            json.dumps([2, 4], ensure_ascii=False),
            "v2_blocked_role_clarity",
            "先确认谁负责整理，再各说一个下一步。",
            "strategy_review_and_generation_v1",
            _ts(11),
            _ts(12),
            _ts(11),
            _ts(12),
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
            context["group_id"],
            run_id,
            "sera_auto",
            "new_message",
            "明确下一步",
            "先确认谁负责整理，再各说一个下一步。",
            "v2_blocked_role_clarity",
            "active_intervention",
            context["session_id"],
            context["task_id"],
            _ts(12),
        ),
    )
    db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, role, sender_type,
            session_no, task_id, session_id, agent_type,
            intervention_run_id, linked_log_id, strategy_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            agent_id,
            "先确认谁负责整理，再各说一个下一步。",
            5,
            "agent",
            "agent",
            context["session_no"],
            context["task_id"],
            context["session_id"],
            "strategy",
            run_id,
            log_id,
            "v2_blocked_role_clarity",
            _ts(12),
        ),
    )

    db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, session_id, cutoff_sequence, trigger_type, rule_result_json,
            final_state, confidence, status, context_from_sequence,
            context_to_sequence, input_message_sequences_json,
            evidence_sequences_json, review_decision, review_final_state,
            review_confidence, review_reason, prompt_version,
            review_started_at, review_completed_at, review_error,
            failure_reason, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context["group_id"],
            context["session_id"],
            4,
            "new_message",
            json.dumps({"state_code": "blocked_frustration", "score": 0.91}, ensure_ascii=False),
            "blocked_frustration",
            0.91,
            "completed",
            1,
            4,
            json.dumps([1, 2, 4], ensure_ascii=False),
            json.dumps([4], ensure_ascii=False),
            None,
            None,
            None,
            None,
            "strategy_review_and_generation_v1",
            _ts(13),
            _ts(13),
            "invalid_json",
            "invalid_json",
            _ts(13),
            _ts(13),
        ),
    )
    return {"pass_monitor_id": pass_monitor_id, "intervene_monitor_id": intervene_monitor_id}


def test_b5_agent_audit_payload_exposes_explainability_chain(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=51, member_count=2, limit_minutes=20)
    _insert_b5_audit_records(db, context)

    from services.teacher_agent_audit_service import get_agent_audit

    audit = get_agent_audit(context["group_id"], context["session_id"], blinded=False)

    assert [item["code"] for item in audit["state_system"]] == [
        "standard",
        "deep_thinking",
        "execution_progress",
        "constructive_conflict",
        "interpersonal_conflict",
        "confusion",
        "frustration",
        "burnout",
        "off_topic_self_regulated",
        "off_topic_unregulated",
        "perfunctory_detachment",
        "individual_marginalization",
        "observing",
        "unclassified",
    ]

    detector = audit["detector_outputs"][0]
    assert detector["state_code"] == "blocked_frustration"
    assert detector["coarse_state_code"] == "blocked_frustration"
    assert "final_state_code" not in detector
    assert detector["state_semantics"] == "coarse_stage1"
    assert detector["raw_state_code"] == "coordination_disorder"
    assert detector["legacy_state_code"] == "coordination_disorder"
    assert detector["normalization_reason"] == "legacy_coordination_disorder_normalized"
    assert "coordination_blocked" in detector["evidence_tags"]
    assert detector["candidate_scores"]["blocked_frustration"] == 0.8

    gate = audit["gate_records"][0]
    assert gate["gating_result"]["decision"] == "skip"
    assert gate["cooldown_check"]["cooling"] is True
    assert gate["cooldown_check"]["reason"] == "cooldown_active_1"

    intervention = audit["interventions"][0]
    assert intervention["final_sub_state_code"] is None
    assert intervention["coarse_state_code"] == "blocked_frustration"
    assert intervention["trigger_source"] == "student_help_request"
    assert intervention["intervention_trace"]["gating_result"]["action"] == "INTERVENE"
    assert intervention["cooldown_check"]["bypassed_by"] == "student_help_request"
    assert intervention["cooldown_check"]["cooldown_seconds"] == 120


def test_b5_api_page_and_frontend_use_canonical_audit_contract(db_and_app, teacher_login):
    db, _app_module, _unused_client = db_and_app
    client, headers = teacher_login
    context = seed_running_session(db, session_no=52, member_count=2, limit_minutes=20)
    _insert_b5_audit_records(db, context)

    response = client.get(
        "/api/teacher/group/%s/agent-audit?session_id=%s&blinded=false"
        % (context["group_id"], context["session_id"]),
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["detector_outputs"][0]["state_code"] == "blocked_frustration"
    assert "coordination_disorder" not in {
        item["state_code"] for item in payload["detector_outputs"]
    }

    page = client.get("/teacher/audit", headers=headers)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for legacy_code in [
        "positive_collaboration",
        "negative_silence",
        "conflict_tension",
        "blocked_frustration",
        "task_detached",
        "unknown",
    ]:
        assert ('value="%s"' % legacy_code) not in html
    for final_code in [
        "standard",
        "deep_thinking",
        "execution_progress",
        "constructive_conflict",
        "interpersonal_conflict",
        "confusion",
        "frustration",
        "burnout",
        "off_topic_self_regulated",
        "off_topic_unregulated",
        "perfunctory_detachment",
        "individual_marginalization",
        "observing",
        "unclassified",
    ]:
        assert ('value="%s"' % final_code) in html
    assert "needs_attention" not in html
    assert "/static/teacher/agent-audit.js" in html

    with open("static/teacher/agent-audit.js", "r", encoding="utf-8") as handle:
        source = handle.read()
    state_order = source.split("const STATE_ORDER = [", 1)[1].split("];", 1)[0]
    for legacy_code in [
        "positive_collaboration",
        "negative_silence",
        "conflict_tension",
        "blocked_frustration",
        "task_detached",
        "unknown",
    ]:
        assert legacy_code not in state_order
    for final_code in [
        "standard",
        "deep_thinking",
        "execution_progress",
        "constructive_conflict",
        "interpersonal_conflict",
        "confusion",
        "frustration",
        "burnout",
        "off_topic_self_regulated",
        "off_topic_unregulated",
        "perfunctory_detachment",
        "individual_marginalization",
        "observing",
        "unclassified",
    ]:
        assert final_code in state_order
    for legacy_code in [
        "participation_imbalance",
        "coordination_disorder",
        "conflict_repair",
        "positive_recovery",
        "insufficient_evidence",
    ]:
        assert legacy_code not in state_order
    assert "Evidence Tags" in source
    assert "Candidate Scores" in source
    assert "Cooldown Check" in source
    assert "Intervention Gating" in source


def test_b5_strategy_reviews_messages_stats_and_evidence_scope(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=53, member_count=2, limit_minutes=20)
    _insert_strategy_review_records(db, context)

    from services.teacher_agent_audit_service import get_agent_audit

    audit = get_agent_audit(context["group_id"], context["session_id"], blinded=False)

    assert len(audit["strategy_reviews"]) == 3
    decisions = [item["llm_decision"] for item in audit["strategy_reviews"]]
    assert "PASS" in decisions
    assert "INTERVENE" in decisions

    pass_review = next(item for item in audit["strategy_reviews"] if item["llm_decision"] == "PASS")
    assert pass_review["rule_candidate_state"] == "task_detached"
    assert pass_review["context_from_sequence"] == 1
    assert pass_review["context_to_sequence"] == 2
    assert pass_review["evidence_sequences"] == [1, 9]
    availability = {item["sequence"]: item["available"] for item in pass_review["evidence_messages"]}
    assert availability[1] is True
    assert availability[9] is False

    intervene_review = next(item for item in audit["strategy_reviews"] if item["llm_decision"] == "INTERVENE")
    assert intervene_review["strategy_id"] == "v2_blocked_role_clarity"
    assert intervene_review["generated_message"] == "先确认谁负责整理，再各说一个下一步。"

    assert all(message["session_id"] == context["session_id"] for message in audit["message_timeline"])
    assert 9 not in {message["sequence"] for message in audit["message_timeline"]}
    agent_labels = {
        message["agent_display_label"]
        for message in audit["message_timeline"]
        if message["role"] == "agent"
    }
    assert "情绪智能体" in agent_labels
    assert "策略智能体 · 自动介入" in agent_labels

    stats = audit["stats"]
    assert stats["emotion_agent_message_count"] == 1
    assert stats["strategy_auto_intervention_count"] == 1
    assert stats["llm_pass_review_count"] == 1
    assert stats["llm_failed_review_count"] == 1
    assert stats["actual_intervention_count"] == 1


def test_b5_teacher_exports_include_strategy_review_audit_fields(db_and_app):
    db, _app_module, _client = db_and_app
    context = seed_running_session(db, session_no=54, member_count=2, limit_minutes=20)
    _insert_strategy_review_records(db, context)

    from services.teacher_export_service import (
        EXPORT_REGISTRY,
        export_interventions_csv,
        export_messages_csv,
        export_strategy_reviews_csv,
    )

    assert "strategy_reviews.csv" not in EXPORT_REGISTRY

    strategy_rows = list(csv.DictReader(StringIO(export_strategy_reviews_csv(
        group_id=context["group_id"],
        session_id=context["session_id"],
    ))))
    assert {"context_from_sequence", "context_to_sequence", "input_message_sequences",
            "evidence_sequences", "rule_candidate_state", "llm_decision",
            "llm_final_state", "llm_reason", "strategy_id", "agent_type",
            "trigger_source"}.issubset(strategy_rows[0].keys())
    pass_row = next(row for row in strategy_rows if row["llm_decision"] == "PASS")
    assert pass_row["evidence_sequences"] == "[1, 9]"
    assert pass_row["rule_candidate_state"] == "task_detached"

    intervention_rows = list(csv.DictReader(StringIO(export_interventions_csv(
        group_id=context["group_id"],
        session_id=context["session_id"],
    ))))
    assert intervention_rows[0]["context_from_sequence"] == "1"
    assert intervention_rows[0]["evidence_sequences"] == "[2, 4]"
    assert intervention_rows[0]["llm_decision"] == "INTERVENE"
    assert intervention_rows[0]["agent_type"] == "strategy"
    assert intervention_rows[0]["trigger_source"] == "new_message"

    message_rows = list(csv.DictReader(StringIO(export_messages_csv(
        group_id=context["group_id"],
        session_id=context["session_id"],
    ))))
    strategy_message = next(row for row in message_rows if row["role"] == "agent" and row["agent_type"] == "strategy")
    assert strategy_message["trigger_source"] == "new_message"


def test_b5_frontend_contract_exposes_evidence_controls_and_agent_labels():
    with open("static/teacher/agent-audit.js", "r", encoding="utf-8") as handle:
        source = handle.read()
    for expected in [
        "strategy_reviews",
        "renderStrategyReviewDetail",
        "highlightEvidenceSequences",
        "策略判断证据",
        "原始消息不可用",
        "情绪智能体",
        "策略智能体 · 自动介入",
        "策略智能体 · 学生求助",
        "策略智能体 · 教师介入",
    ]:
        assert expected in source

    with open("templates/teacher/audit.html", "r", encoding="utf-8") as handle:
        html = handle.read()
    assert "audit-agent-filter" in html
    assert "audit-message-flow" in html
    assert "audit-stats" in html
    assert "@media print" in html
