# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from tests.helpers import login_with_key, seed_running_session


WATCHED_TABLES = (
    "messages",
    "monitor_runs",
    "state_assessments",
    "group_states",
    "collaboration_state_segments",
    "help_requests",
    "intervention_runs",
    "intervention_logs",
)


SCRIPT_39 = [
    "我们先确认任务，是比较两个方案并给出证据化建议。",
    "我负责把问题拆成研究问题、证据、访谈和方案比较四块。",
    "我来整理证据，先找成本、距离和可维护性三个维度。",
    "我负责访谈材料，把同学反馈按支持和担忧分类。",
    "那我做方案比较表，先把评价标准列出来。",
    "我们每两分钟同步一次，最后一起合并结论。",
    "我先把已有资料复制到共享文档里。",
    "访谈里有三条支持方案 A 的理由。",
    "方案 B 的距离近，但成本可能高一点。",
    "我们先不要急着选，按证据把优缺点对齐。",
    "这里先停一下，看看还有没有遗漏的证据。",
    "有点无聊了，这个表格填来填去都差不多。",
    "等会儿吃什么？我有点饿了。",
    "先聊会儿游戏吧，反正最后能交上去就行。",
    "我不太确定这个比较标准是不是够用。",
    "是不是还要把访谈和成本连起来说明？",
    "我还没想好访谈材料怎么用。",
    "不知道怎么把问题和方案连起来。",
    "现在没有下一步，越整理越乱。",
    "证据很多，但都不知道放到哪一列。",
    "我把访谈摘要放进来以后，结论反而更乱了。",
    "我们是不是需要先确定一个最小目标？",
    "我卡在方案比较和最终建议之间。",
    "我们现在卡住了，请帮我们明确下一步。",
    "这个方案不合理，你这个标准根本没有证据。",
    "你也别一直否定，我是在按访谈结果说。",
    "你的方案太片面了，别乱说。",
    "先停一下，我们按证据、成本、可执行性重新比较。",
    "我把 A 的成本优势写清楚，你补 B 的距离优势。",
    "访谈材料显示大家更在意维护成本，这个要进结论。",
    "那最终建议可以是优先 B，但保留 A 的低成本备选。",
    "我们把冲突点改成评价标准差异，不写个人判断。",
    "我补一条风险：B 的初期预算高，需要解释来源。",
    "共享文档里我加了比较表标题和三列证据。",
    "最后一段我来总结：选择 B，因为证据覆盖更多维度。",
    "我检查一下任务要求，确认要提交建议和理由。",
    "我把每个人负责的证据来源附在结尾。",
    "结论已经形成，我们再读一遍有没有跳步。",
    "最终提交：推荐 B，同时说明 A 的成本优势作为备选。",
]


def _counts(db, group_id):
    result = {}
    for table in WATCHED_TABLES:
        result[table] = db.query_one(
            f"SELECT COUNT(*) AS c FROM {table} WHERE group_id=?",
            (group_id,),
        )["c"]
    result["collaboration_state_finalizations"] = db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_finalizations WHERE group_id=?",
        (group_id,),
    )["c"]
    return result


def _enable_strategy_runtime(db, ctx):
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
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (ctx["task_id"],),
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1, state='OPEN' WHERE id=?",
        (ctx["group_id"],),
    )


def _login_teacher_client(db, app_module):
    from werkzeug.security import generate_password_hash

    teacher_row = db.query_one("SELECT id FROM users WHERE role='teacher' ORDER BY id LIMIT 1")
    if teacher_row:
        teacher_user_id = teacher_row["id"]
    else:
        teacher_user_id = db.execute(
            "INSERT INTO users(username, password_hash, real_name, role, created_at) VALUES(?,?,?,?,?)",
            (
                "batch7_teacher",
                generate_password_hash("unused"),
                "Batch7 Teacher",
                "teacher",
                db.now_str(),
            ),
        )

    plaintext = "BATCH7-TEACHER-KEY"
    db.execute("DELETE FROM teacher_access_keys WHERE key_name='batch7_teacher_key'")
    db.execute(
        """
        INSERT INTO teacher_access_keys(key_name, key_hash, teacher_user_id, is_active, created_at)
        VALUES(?,?,?,?,?)
        """,
        (
            "batch7_teacher_key",
            generate_password_hash(plaintext),
            teacher_user_id,
            1,
            db.now_str(),
        ),
    )
    teacher_client = app_module.app.test_client()
    response = teacher_client.post("/login", data={"login_key": plaintext}, follow_redirects=False)
    assert response.status_code == 302
    tab_token = parse_qs(urlparse(response.headers["Location"]).query)["tab_token"][0]
    return teacher_client, {"X-Tab-Token": tab_token}


def _login_student_clients(app_module, ctx):
    member_sessions = []
    for _student_id, login_key in ctx["students"]:
        student_client = app_module.app.test_client()
        headers = login_with_key(student_client, login_key)
        member_sessions.append((student_client, headers))
    return member_sessions


def _start_group_discussion(db, ctx):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    for student_id, _login_key in ctx["students"]:
        enter_group_discussion_stage(ctx["session_id"], ctx["group_id"], student_id)
    started = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    deadline = (datetime.now() + timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE experiment_sessions SET start_time=?, time_limit_minutes=50 WHERE id=?",
        (started, ctx["session_id"]),
    )
    db.execute(
        """
        UPDATE group_session_discussions
           SET status='running', started_at=?, deadline=?
         WHERE session_id=? AND group_id=?
        """,
        (started, deadline, ctx["session_id"], ctx["group_id"]),
    )


def _script_index_for_content(content: str) -> int | None:
    text = str(content or "")
    for index, expected in enumerate(SCRIPT_39, start=1):
        if expected in text:
            return index
    return None


def _mock_state_for_script_index(index: int | None) -> str:
    if index is None:
        return "unknown"
    if 3 <= index <= 6:
        return "positive_collaboration"
    if 12 <= index <= 14:
        return "task_detached"
    if 17 <= index <= 24:
        return "blocked_frustration"
    if 25 <= index <= 27:
        return "conflict_tension"
    if 28 <= index <= 39:
        return "positive_collaboration"
    return "unknown"


def _patch_batch7_pipeline(monkeypatch):
    import config
    import routes.api as api_routes
    import services.discussion_pipeline_v2.llm_state_detector as detector_module
    import services.discussion_pipeline_v2.monitoring_service as monitoring_module
    import services.discussion_pipeline_v2.trigger_policy as trigger_policy
    import services.intervention_pipeline_v2.intervention_service as intervention_module
    import services.intervention_pipeline_v2.intervention_validator as intervention_validator
    import services.llm_analyzer as llm_analyzer
    import services.state_assessment_scheduler as assessment_scheduler
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(config, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(config, "LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED", True)
    monkeypatch.setattr(api_routes, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring_module, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(monitoring_module, "DISCUSSION_PIPELINE_V2_SHADOW", False)
    monkeypatch.setattr(monitoring_module, "AUTO_INTERVENTION_V2_ENABLED", True)
    monkeypatch.setattr(monitoring_module, "LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED", True)
    monkeypatch.setattr(monitoring_module, "PIPELINE_V2_MIN_INTERVENTION_CONFIDENCE", 0.4)
    monkeypatch.setattr(
        monitoring_module.MonitoringService,
        "schedule_silence_check",
        staticmethod(lambda group_id, expected_sequence: None),
    )

    monkeypatch.setattr(trigger_policy, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(trigger_policy, "STATE_LLM_ENABLED", True)
    monkeypatch.setattr(trigger_policy, "STATE_LLM_MIN_NEW_MESSAGES", 1)
    monkeypatch.setattr(trigger_policy, "STATE_LLM_GATE_MIN_RULE_SCORE", 0.0)
    monkeypatch.setattr(trigger_policy, "STATE_LLM_FORCE_AFTER_NEW_MESSAGES", 1)
    monkeypatch.setattr(detector_module, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(intervention_validator, "INTERVENTION_V2_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    monkeypatch.setattr(
        assessment_scheduler,
        "request_state_assessment_for_message",
        lambda **_kwargs: {
            "created": False,
            "reason": "batch7_explicit_recovery_batch",
        },
    )

    detector_calls = []

    def fake_state_detect(context, rule_assessment=None, features=None):
        rows = list(context.get("state_detector_messages") or [])
        indexed_rows = [
            (_script_index_for_content(row.get("content")), row)
            for row in rows
        ]
        latest_index = max([idx for idx, _row in indexed_rows if idx is not None] or [None])
        state = _mock_state_for_script_index(latest_index)
        evidence_rows = [
            row for idx, row in indexed_rows
            if idx == latest_index and row.get("id") is not None
        ]
        evidence_ids = [int(evidence_rows[-1]["id"])] if state != "unknown" and evidence_rows else []
        detector_calls.append({"index": latest_index, "state": state, "evidence_ids": evidence_ids})
        return {
            "result": {
                "primary_state": state,
                "state_code": state,
                "confidence": 0.91 if state != "unknown" else 0.2,
                "evidence_message_ids": evidence_ids,
                "secondary_state": None,
                "reason": f"batch7 mock index={latest_index}",
            },
            "meta": {
                "analysis_skipped": False,
                "success": True,
                "validation_status": "passed",
                "schema_valid": True,
                "profile": "state_detector",
                "model_name": "mock-state-detector",
                "prompt_version": "batch7_mock_state",
                "latency_ms": 1,
            },
        }

    monkeypatch.setattr(detector_module.LLMStateDetector, "detect", staticmethod(fake_state_detect))

    review_counts = {}

    def fake_strategy_review(context):
        state = (context.get("state_assessment") or {}).get("detected_state")
        review_counts[state] = review_counts.get(state, 0) + 1
        if state == "task_detached" and review_counts[state] == 1:
            decision = "INTERVENE"
            strategy = "v2_offtask_refocus"
            message = "先把话题拉回任务：确认现在要完成什么，再定一个马上能做的小步骤。"
            reason = "off-task discussion needs one refocus."
        elif state == "negative_silence" and review_counts[state] == 1:
            decision = "INTERVENE"
            strategy = "v2_silence_restart"
            message = "可以从一个最容易回答的小问题重新开始讨论。"
            reason = "continuous silence needs one low-pressure restart."
        elif state == "conflict_tension" and review_counts[state] == 1:
            decision = "INTERVENE"
            strategy = "v2_conflict_evidence"
            message = "先不判断谁对谁错，请把不同观点各写出一条证据，再看哪些可以合并。"
            reason = "conflict needs evidence-based repair."
        else:
            decision = "PASS"
            strategy = None
            message = ""
            reason = "state exists but student visible intervention is not needed now."
        return {
            "ok": True,
            "action": decision,
            "decision": decision,
            "strategy": strategy,
            "strategy_id": strategy,
            "student_message": message,
            "message": message or None,
            "teacher_reason": reason,
            "reason": reason,
            "state_assessment": context.get("state_assessment") or {},
            "confirmed_state": state,
            "confirmed_confidence": (context.get("state_assessment") or {}).get("confidence"),
            "evidence_sequences": (context.get("state_assessment") or {}).get("evidence_message_ids") or [],
            "state_segments": [],
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "latency_ms": 1, "model_name": "mock-strategy"},
            "validation": {"valid": True},
        }

    monkeypatch.setattr(intervention_module, "review_strategy_context", fake_strategy_review)

    monkeypatch.setattr(
        llm_analyzer,
        "generate_student_help_response",
        lambda *args, **kwargs: "先把卡点写成一句话，再选一个五分钟内能完成的最小下一步。",
    )
    return {"detector_calls": detector_calls, "review_counts": review_counts}


def _post_script_item(member_sessions, ctx, index, text):
    member_index = (index - 1) % len(member_sessions)
    client, headers = member_sessions[member_index]
    if index == 24:
        response = client.post(
            "/api/student/help",
            json={
                "group_id": ctx["group_id"],
                "request_text": text,
                "client_message_id": f"batch7-help-{index}",
            },
            headers=headers,
        )
        assert response.status_code == 202, response.get_data(as_text=True)
        return response.get_json()
    response = client.post(
        "/api/message",
        json={
            "group_id": ctx["group_id"],
            "content": text,
            "client_message_id": f"batch7-msg-{index}",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _latest_student_sequence(db, group_id):
    row = db.query_one(
        """
        SELECT id, sequence
          FROM messages
         WHERE group_id=? AND role='student'
         ORDER BY sequence DESC, id DESC
         LIMIT 1
        """,
        (group_id,),
    )
    return dict(row)


def _run_silence_from_latest_student(db, ctx, monkeypatch):
    latest = _latest_student_sequence(db, ctx["group_id"])
    now = datetime.now().replace(microsecond=0)
    old_time = (now - timedelta(seconds=220)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE messages SET created_at=? WHERE id=?", (old_time, latest["id"]))
    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    created = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=latest["sequence"],
        expected_last_student_message_at=old_time,
        expected_session_id=ctx["session_id"],
        expected_task_id=ctx["task_id"],
        now_value=(now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert created.get("skipped") is False, created

    def negative_silence_rule(_context, _features):
        return {
            "version": "plan_batch7",
            "assessment_status": "state_detected",
            "winning_state_code": "negative_silence",
            "winning_state_label": "消极沉默",
            "winning_score": 0.93,
            "candidates": [
                {
                    "state_code": "negative_silence",
                    "score": 0.93,
                    "signals": ["silent_seconds=220"],
                }
            ],
        }

    import services.discussion_pipeline_v2.monitoring_service as monitoring
    from agent.monitoring_tasks import check_room_silence

    before_agent_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"]
    with monkeypatch.context() as silence_patch:
        silence_patch.setattr(
            monitoring.RuleDetector,
            "detect",
            staticmethod(negative_silence_rule),
        )
        silence_patch.setattr(
            monitoring.TriggerPolicy,
            "should_enqueue_strategy_review",
            staticmethod(
                lambda *_args, **_kwargs: (
                    True,
                    "state_review_negative_silence_confidence_0.93",
                )
            ),
        )
        result = check_room_silence.call_local(
            ctx["group_id"],
            latest["sequence"],
            old_time,
            ctx["session_id"],
            ctx["task_id"],
        )
        duplicate = check_room_silence.call_local(
            ctx["group_id"],
            latest["sequence"],
            old_time,
            ctx["session_id"],
            ctx["task_id"],
        )

    assert result["fused_state"] == "negative_silence"
    assert result["strategy_review_enqueue_result"]["enqueued"] is True
    assert duplicate["skipped"] is True
    assert duplicate["reason"] == "duplicate_cutoff"
    after_agent_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"]
    assert after_agent_count == before_agent_count + 1
    segment = db.query_one(
        """
        SELECT * FROM collaboration_state_segments
        WHERE group_id=? AND state_code='negative_silence'
        ORDER BY id DESC LIMIT 1
        """,
        (ctx["group_id"],),
    )
    assert segment["id"] == created["segment_id"]
    assert segment["last_observed_at"] > created["last_observed_at"]
    assert segment["intervention_disposition"] == "PUBLISHED"
    assert segment["intervention_run_id"] is not None
    assert _latest_student_sequence(db, ctx["group_id"])["sequence"] == latest["sequence"]
    return {
        "result": result,
        "segment_id": segment["id"],
        "trigger_sequence": latest["sequence"],
    }


def _run_state_strategy(ctx, *, expected_state: str, trigger_type: str):
    import db as db_module
    from services.discussion_pipeline_v2.monitoring_service import MonitoringService
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

    room = db_module.query_one(
        """
        SELECT active_intervention_run_id, lock_token
        FROM groups
        WHERE id=?
        """,
        (ctx["group_id"],),
    )
    if (
        room
        and room["lock_token"]
        and int(room["active_intervention_run_id"] or 0) < 0
    ):
        RoomLeaseService.release(ctx["group_id"], room["lock_token"])

    result = MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type=trigger_type,
        allow_state_llm=True,
        persist_state_segment=True,
        schedule_strategy=True,
    )
    assert result["fused_state"] == expected_state
    assert result["strategy_review_enqueue_result"]["enqueued"] is True
    return result


def _finalization_gateway_for_tail(latest_sequence):
    from services.llm_gateway import LlmResult

    class Gateway:
        def __init__(self):
            self.calls = []

        def call(self, profile_name, payload, response_type="json"):
            self.calls.append({
                "profile_name": profile_name,
                "payload": payload,
                "response_type": response_type,
            })
            return LlmResult(
                success=True,
                output={
                    "state_segments": [
                        {
                            "state": "positive_collaboration",
                            "start_message_id": latest_sequence,
                            "end_message_id": latest_sequence,
                            "evidence_message_ids": [latest_sequence],
                            "confidence": 0.9,
                        }
                    ],
                    "should_intervene": False,
                    "intervention_message": "",
                },
                profile_name=profile_name,
            )

    return Gateway()


def test_39_message_v2_pipeline_teacher_review_and_growth_controls(
    db_and_app,
    monkeypatch,
):
    db, app_module, _client = db_and_app
    ctx = seed_running_session(db, session_no=701, member_count=4, limit_minutes=50)
    _enable_strategy_runtime(db, ctx)
    _start_group_discussion(db, ctx)
    patch_state = _patch_batch7_pipeline(monkeypatch)
    member_sessions = _login_student_clients(app_module, ctx)

    silence_flow = None
    for index, text in enumerate(SCRIPT_39, start=1):
        _post_script_item(member_sessions, ctx, index, text)
        if index == 13:
            _run_state_strategy(
                ctx,
                expected_state="task_detached",
                trigger_type="batch7_task_detached",
            )
            silence_flow = _run_silence_from_latest_student(db, ctx, monkeypatch)
        if index == 27:
            _run_state_strategy(
                ctx,
                expected_state="conflict_tension",
                trigger_type="batch7_conflict",
            )

    assert silence_flow is not None
    closed_silence = db.query_one(
        """
        SELECT * FROM collaboration_state_segments
        WHERE id=?
        """,
        (silence_flow["segment_id"],),
    )
    assert closed_silence["is_active"] == 0
    assert closed_silence["resolution_reason"] == "student_message_resumed"
    assert closed_silence["end_sequence"] > silence_flow["trigger_sequence"]
    assert closed_silence["intervention_disposition"] == "PUBLISHED"

    discussion_id = db.query_one(
        """
        SELECT id FROM group_session_discussions
        WHERE group_id=? AND session_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (ctx["group_id"], ctx["session_id"]),
    )["id"]
    latest_student = _latest_student_sequence(db, ctx["group_id"])
    task_detached_start = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND content=?
        """,
        (ctx["group_id"], SCRIPT_39[11]),
    )["sequence"]
    task_detached_end = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND content=?
        """,
        (ctx["group_id"], SCRIPT_39[13]),
    )["sequence"]
    frustration_start = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND content=?
        """,
        (ctx["group_id"], SCRIPT_39[14]),
    )["sequence"]
    frustration_end = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND client_message_id=?
        """,
        (ctx["group_id"], "batch7-help-24"),
    )["sequence"]
    conflict_start = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND content=?
        """,
        (ctx["group_id"], SCRIPT_39[24]),
    )["sequence"]
    conflict_end = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND content=?
        """,
        (ctx["group_id"], SCRIPT_39[26]),
    )["sequence"]
    recovery_start = db.query_one(
        """
        SELECT sequence FROM messages
        WHERE group_id=? AND role='student' AND content=?
        """,
        (ctx["group_id"], SCRIPT_39[27]),
    )["sequence"]
    agent_count_before_recovery_batch = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"]
    import services.state_assessment_scheduler as assessment_scheduler

    monkeypatch.setattr(assessment_scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 1)
    monkeypatch.setattr(assessment_scheduler, "STATE_LLM_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        assessment_scheduler,
        "STATE_LLM_MAX_CANDIDATE_MESSAGES",
        50,
    )
    recovery_queued = []
    monkeypatch.setattr(
        assessment_scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: recovery_queued.append(
            (int(batch_id), int(delay or 0))
        ),
    )

    def fixed_recovery_batch(**kwargs):
        end_sequence = int(kwargs["fixed_candidate_end_sequence"])
        payload = {
            "segments": [
                {
                    "state": "task_detached",
                    "start_sequence": task_detached_start,
                    "end_sequence": task_detached_end,
                    "confidence": 0.93,
                    "evidence_sequences": [
                        task_detached_start,
                        task_detached_end,
                    ],
                },
                {
                    "state": "blocked_frustration",
                    "start_sequence": frustration_start,
                    "end_sequence": frustration_end,
                    "confidence": 0.95,
                    "evidence_sequences": [
                        frustration_start,
                        frustration_end,
                    ],
                },
                {
                    "state": "conflict_tension",
                    "start_sequence": conflict_start,
                    "end_sequence": conflict_end,
                    "confidence": 0.94,
                    "evidence_sequences": [conflict_start, conflict_end],
                },
                {
                    "state": "positive_collaboration",
                    "start_sequence": recovery_start,
                    "end_sequence": end_sequence,
                    "confidence": 0.96,
                    "evidence_sequences": [recovery_start, end_sequence],
                },
            ],
            "active_segment_index": 3,
            "intervention": {
                "needed": False,
                "target_segment_index": None,
                "reason_code": "student_self_recovered",
                "message": None,
            },
            "primary_state": "positive_collaboration",
            "state_code": "positive_collaboration",
            "confidence": 0.96,
            "evidence_message_ids": [],
        }
        return {
            "monitor_run_id": None,
            "state_llm_result": payload,
            "state_llm_meta": {
                "success": True,
                "analysis_failed": False,
                "analysis_skipped": False,
                "model_name": "fixed-batch7-recovery-model",
                "prompt_version": "learning-assistant-plan-batch7",
                "raw_response": json.dumps(payload, ensure_ascii=False),
            },
        }

    import services.discussion_pipeline_v2.monitoring_service as monitoring_module

    with monkeypatch.context() as recovery_patch:
        recovery_patch.setattr(
            monitoring_module.MonitoringService,
            "run_detection",
            staticmethod(fixed_recovery_batch),
        )
        recovery_request = assessment_scheduler.request_state_assessment(
            group_id=ctx["group_id"],
            session_id=ctx["session_id"],
            discussion_id=discussion_id,
            trigger_type="post_intervention_observation",
            trigger_sequence=latest_student["sequence"],
        )
        assert recovery_request["created"] is True
        assert recovery_queued == [
            (recovery_request["assessment_batch_id"], 0)
        ]
        recovery_outcome = assessment_scheduler.execute_state_assessment_batch(
            recovery_request["assessment_batch_id"]
        )

    assert recovery_outcome["succeeded"] is True
    assert recovery_outcome["intervention"]["published"] is False
    assert recovery_outcome["intervention"]["reason"] == "intervention_not_needed"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"] == agent_count_before_recovery_batch

    from services.help_request_coverage_service import HelpRequestCoverageService

    conflict_guard = HelpRequestCoverageService.evaluate(
        ctx["group_id"],
        ctx["session_id"],
        "conflict_tension",
        None,
        conflict_start,
        conflict_end,
    )
    assert conflict_guard["blocked"] is False
    assert conflict_guard["reason_code"] == "different_state_new_issue"
    assert conflict_guard["handled_state_code"] == "blocked_frustration"

    strategy_cooldown_before_emotion = db.query_one(
        "SELECT last_intervention_at FROM groups WHERE id=?",
        (ctx["group_id"],),
    )["last_intervention_at"]
    from services.emotion_agent.emotion_reflection_service import (
        EmotionReflectionService,
    )
    emotion_now = datetime.now().replace(microsecond=0)
    emotion_result = EmotionReflectionService.execute_once(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        discussion_id=discussion_id,
        task_id=ctx["task_id"],
        scheduled_at=emotion_now.strftime("%Y-%m-%d %H:%M:%S"),
        tick_index=1,
        slot_id=7701,
        window_start=(emotion_now - timedelta(minutes=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        window_end=emotion_now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert emotion_result["status"] == "skipped"
    assert emotion_result["reason"] == "emotion_agent_disabled"
    assert db.query_one(
        "SELECT last_intervention_at FROM groups WHERE id=?",
        (ctx["group_id"],),
    )["last_intervention_at"] == strategy_cooldown_before_emotion

    flow_counts = _counts(db, ctx["group_id"])
    student_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='student'",
        (ctx["group_id"],),
    )["c"]
    agent_count = db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"]
    assert student_count == 39
    assert agent_count == 4
    assert flow_counts["help_requests"] == 1
    published_frustration = db.query_one(
        """
        SELECT COUNT(*) AS c
          FROM intervention_runs
         WHERE group_id=?
           AND status IN ('PUBLISHED','FALLBACK')
           AND detected_state='blocked_frustration'
        """,
        (ctx["group_id"],),
    )["c"]
    assert published_frustration == 1
    assert db.query_one(
        """
        SELECT COUNT(*) AS c
          FROM intervention_runs
         WHERE group_id=?
           AND status IN ('PUBLISHED','FALLBACK')
           AND trigger_type='silence_rule'
           AND detected_state='negative_silence'
        """,
        (ctx["group_id"],),
    )["c"] == 1
    assert db.query_one(
        """
        SELECT COUNT(*) AS c
          FROM intervention_runs
         WHERE group_id=?
           AND status IN ('PUBLISHED','FALLBACK')
           AND trigger_type='auto_state'
           AND detected_state='conflict_tension'
        """,
        (ctx["group_id"],),
    )["c"] >= 1
    agent_audit_rows = db.query_all(
        """
        SELECT m.id AS message_id,
               m.group_id AS message_group_id,
               m.session_no AS message_session_no,
               m.session_id AS message_session_id,
               m.task_id AS message_task_id,
               m.intervention_run_id,
               ir.id AS run_id,
               ir.group_id AS run_group_id,
               ir.session_id AS run_session_id,
               ir.task_id AS run_task_id,
                ir.agent_type,
                ir.trigger_type,
                ir.detected_state,
                ir.dominant_state
          FROM messages AS m
          LEFT JOIN intervention_runs AS ir ON ir.id=m.intervention_run_id
         WHERE m.group_id=? AND m.role='agent'
         ORDER BY m.sequence
        """,
        (ctx["group_id"],),
    )
    assert len(agent_audit_rows) == 4
    assert len({row["intervention_run_id"] for row in agent_audit_rows}) == 4
    for row in agent_audit_rows:
        assert row["message_group_id"] == row["run_group_id"] == ctx["group_id"]
        assert row["message_session_no"] == ctx["session_no"]
        assert row["message_session_id"] == row["run_session_id"] == ctx["session_id"]
        assert row["message_task_id"] == row["run_task_id"] == ctx["task_id"]
        assert row["intervention_run_id"] == row["run_id"]
    assert {
        (
            row["agent_type"],
            row["trigger_type"],
            row["detected_state"] or row["dominant_state"],
        )
        for row in agent_audit_rows
    } >= {
        ("strategy", "auto_state", "task_detached"),
        ("strategy", "silence_rule", "negative_silence"),
        ("strategy", "student_help_request", "blocked_frustration"),
        ("strategy", "auto_state", "conflict_tension"),
    }

    raw_segment_states = {
        row["state_code"]
        for row in db.query_all(
            "SELECT state_code FROM collaboration_state_segments WHERE group_id=?",
            (ctx["group_id"],),
        )
    }
    assert {
        "positive_collaboration",
        "task_detached",
        "blocked_frustration",
        "conflict_tension",
        "negative_silence",
    }.issubset(raw_segment_states)

    teacher_client, teacher_headers = _login_teacher_client(db, app_module)

    response = teacher_client.get(
        f"/api/teacher/group/{ctx['group_id']}/emotion-review"
        f"?session_id={ctx['session_id']}&window_minutes=1",
        headers=teacher_headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    review = response.get_json()
    assert review["summary"]["student_message_count"] == 39
    assert review["summary"]["agent_message_count"] == 4
    assert review["summary"]["message_count"] == 43
    assert review["summary"]["observing_student_message_count"] < 39
    assert review["summary"]["intervention_count"] == len(review["interventions"]) == 4
    assert review["summary"]["silence_segment_count"] >= 1
    assert any(
        item["id"] == silence_flow["segment_id"]
        and item["is_active"] is False
        and item["resolution_reason"] == "student_message_resumed"
        for item in review["silence_segments"]
    )

    display_states = {item["state_code"] for item in review["state_segments"]}
    silence_states = {item["state_code"] for item in review["silence_segments"]}
    assert display_states == {"unclassified"}
    assert "negative_silence" in silence_states
    assert "negative_silence" not in review["distribution"]
    assert review["distribution"]["unclassified"]["message_count"] >= 1
    assert {
        "positive_collaboration",
        "task_detached",
        "blocked_frustration",
        "conflict_tension",
    }.issubset(review["coarse_distribution"])

    labels = {
        message.get("agent_display_label")
        for message in review["messages"]
        if message.get("role") == "agent"
    }
    assert "策略智能体 · 学生求助" in labels
    assert "策略智能体 · 自动介入" in labels
    assert sum(1 for item in review["interventions"] if item["display_label"] == "策略智能体 · 自动介入") == 3
    assert sum(1 for item in review["interventions"] if item["display_label"] == "策略智能体 · 学生求助") == 1

    assert db.query_one(
        """
        SELECT COUNT(*) AS c FROM intervention_runs
        WHERE assessment_batch_id=?
        """,
        (recovery_request["assessment_batch_id"],),
    )["c"] == 0

    from services.discussion_pipeline_v2.monitoring_service import MonitoringService
    from services.intervention_pipeline_v2.intervention_service import InterventionService
    from agent.help_tasks import _execute_help_flow

    latest = _latest_student_sequence(db, ctx["group_id"])
    duplicate_monitor = MonitoringService.run_detection(
        ctx["group_id"],
        trigger_type="student_message",
        fixed_candidate_end_sequence=latest["sequence"],
        allow_state_llm=False,
        persist_state_segment=False,
        schedule_strategy=False,
    )
    assert duplicate_monitor["skipped"] is True
    assert duplicate_monitor["reason"] == "duplicate_cutoff"

    batch_segment = db.query_one(
        """
        SELECT assessment_batch_id
          FROM collaboration_state_segments
         WHERE group_id=? AND source='llm' AND assessment_status='confirmed'
         ORDER BY id LIMIT 1
        """,
        (ctx["group_id"],),
    )
    from services.state_assessment_batch_service import StateAssessmentBatchService

    duplicate_save = StateAssessmentBatchService.save_successful_segments(
        batch_segment["assessment_batch_id"],
        [],
    )
    assert duplicate_save["saved"] is False
    published_auto = db.query_one(
        """
        SELECT monitor_run_id, state_assessment_id, group_id, session_id, task_id, cutoff_sequence
          FROM intervention_runs
         WHERE group_id=? AND status='PUBLISHED' AND trigger_type='auto_state'
         ORDER BY id LIMIT 1
        """,
        (ctx["group_id"],),
    )
    InterventionService.execute(
        published_auto["monitor_run_id"],
        state_assessment_id=published_auto["state_assessment_id"],
        group_id=published_auto["group_id"],
        session_id=published_auto["session_id"],
        task_id=published_auto["task_id"],
        cutoff_sequence=published_auto["cutoff_sequence"],
        trigger_source="auto_state",
    )
    help_request_id = db.query_one(
        "SELECT id FROM help_requests WHERE group_id=?",
        (ctx["group_id"],),
    )["id"]
    _execute_help_flow(help_request_id)

    retry_counts = _counts(db, ctx["group_id"])
    assert retry_counts == flow_counts

    for _ in range(5):
        assert teacher_client.get("/api/teacher/groups?all=1", headers=teacher_headers).status_code == 200
        assert teacher_client.get(f"/api/group/{ctx['group_id']}/state", headers=teacher_headers).status_code == 200
        assert teacher_client.get(
            f"/api/teacher/group/{ctx['group_id']}/emotion-review"
            f"?session_id={ctx['session_id']}&window_minutes=1",
            headers=teacher_headers,
        ).status_code == 200
    refresh_counts = _counts(db, ctx["group_id"])
    assert refresh_counts == flow_counts

    db.execute(
        "UPDATE experiment_sessions SET status='ended', end_time=? WHERE id=?",
        (db.now_str(), ctx["session_id"]),
    )
    latest_student_sequence = _latest_student_sequence(db, ctx["group_id"])["sequence"]
    gateway = _finalization_gateway_for_tail(latest_student_sequence)

    from services.collaboration_state_finalization_service import finalize_collaboration_states

    first_finalization = finalize_collaboration_states(
        ctx["group_id"],
        ctx["session_id"],
        "session_end",
        gateway=gateway,
    )
    second_finalization = finalize_collaboration_states(
        ctx["group_id"],
        ctx["session_id"],
        "teacher_close",
        gateway=gateway,
    )
    third_finalization = finalize_collaboration_states(
        ctx["group_id"],
        ctx["session_id"],
        "room_freeze",
        gateway=gateway,
    )
    assert first_finalization["ok"] is True
    assert second_finalization["skipped"] is True
    assert third_finalization["skipped"] is True
    assert len(gateway.calls) == 1

    final_counts = _counts(db, ctx["group_id"])
    allowed_growth = {
        "collaboration_state_segments": 1,
        "collaboration_state_finalizations": 1,
    }
    for table, before in refresh_counts.items():
        assert final_counts[table] == before + allowed_growth.get(table, 0), table

    assert patch_state["detector_calls"]
    assert patch_state["review_counts"].get("task_detached", 0) >= 1
    assert patch_state["review_counts"].get("negative_silence", 0) == 1
    assert any(call["state"] == "conflict_tension" for call in patch_state["detector_calls"])
    assert patch_state["review_counts"].get("conflict_tension", 0) >= 1
    assert patch_state["review_counts"].get("positive_collaboration", 0) == 0


def _seed_completed_monitor_for_intervention(db, *, session_no: int, state_code: str = "conflict_tension"):
    ctx = seed_running_session(db, session_no=session_no, member_count=2, limit_minutes=30)
    _enable_strategy_runtime(db, ctx)
    student_1 = ctx["students"][0][0]
    student_2 = ctx["students"][1][0]
    msg1 = db.create_message(ctx["group_id"], student_1, "We need a clear comparison criterion.", role="student")
    msg2 = db.create_message(ctx["group_id"], student_2, "This proposal has no evidence yet.", role="student")
    cutoff = msg2["sequence"]
    rule_result = {
        "winning_state_code": state_code,
        "winning_score": 0.86,
        "assessment_status": "state_detected",
        "evidence_sequences": [msg1["sequence"], msg2["sequence"]],
    }
    monitor_run_id = db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, rule_result_json,
            final_state, confidence, status, analyzer_version,
            session_id, task_id, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            cutoff,
            "student_message",
            json.dumps(rule_result, ensure_ascii=False),
            state_code,
            0.86,
            "completed",
            "discussion_pipeline_v2_alpha",
            ctx["session_id"],
            ctx["task_id"],
            db.now_str(),
            db.now_str(),
        ),
    )
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no,
            fused_state_code, assessment_status, confidence, should_intervene,
            evidence_summary, fusion_json, rule_assessment_json, context_json,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            ctx["session_id"],
            ctx["task_id"],
            ctx["session_no"],
            state_code,
            "confirmed",
            0.86,
            1,
            "seeded intervention assessment",
            json.dumps({"decision_source": "test", "evidence_message_ids": [msg1["sequence"], msg2["sequence"]]}, ensure_ascii=False),
            json.dumps(rule_result, ensure_ascii=False),
            json.dumps({"evidence_message_ids": [msg1["sequence"], msg2["sequence"]]}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    rule_result["monitor_audit"] = {"state_assessment_id": assessment_id}
    db.execute(
        "UPDATE monitor_runs SET rule_result_json=?, state_assessment_id=? WHERE id=?",
        (json.dumps(rule_result, ensure_ascii=False), assessment_id, monitor_run_id),
    )
    return ctx, monitor_run_id, assessment_id, cutoff


def _intervene_review_result(context):
    state = (context.get("state_assessment") or {}).get("detected_state") or "conflict_tension"
    return {
        "ok": True,
        "action": "INTERVENE",
        "decision": "INTERVENE",
        "strategy": "v2_conflict_evidence" if state == "conflict_tension" else "v2_offtask_refocus",
        "strategy_id": "v2_conflict_evidence" if state == "conflict_tension" else "v2_offtask_refocus",
        "student_message": "Please compare the evidence first, then merge the parts that can be merged.",
        "message": "Please compare the evidence first, then merge the parts that can be merged.",
        "teacher_reason": "mock intervene",
        "reason": "mock intervene",
        "state_assessment": context.get("state_assessment") or {},
        "confirmed_state": state,
        "confirmed_confidence": 0.86,
        "evidence_sequences": (context.get("state_assessment") or {}).get("evidence_message_ids") or [],
        "state_segments": [],
        "profile": "strategy_review_and_generation",
        "prompt_version": "strategy_review_decision_v3",
        "payload": {"messages": []},
        "llm_result": {"success": True},
        "validation": {"valid": True},
    }


def test_intervention_lock_failure_timeout_publish_and_late_gate_paths_release_room(
    db_and_app,
    monkeypatch,
):
    db, _app_module, _client = db_and_app
    import services.intervention_pipeline_v2.intervention_service as service_module
    from services.intervention_pipeline_v2.intervention_service import InterventionService
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))

    ctx, monitor_id, assessment_id, cutoff = _seed_completed_monitor_for_intervention(
        db,
        session_no=730,
    )
    queued_help_id = db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            status, request_text, created_at
        ) VALUES(?,?,?,?,?,'QUEUED',?,?)
        """,
        (
            ctx["group_id"],
            ctx["students"][0][0],
            ctx["task_id"],
            ctx["session_no"],
            ctx["session_id"],
            "help is pending",
            db.now_str(),
        ),
    )
    monkeypatch.setattr(service_module, "review_strategy_context", _intervene_review_result)
    pending_help = InterventionService.execute(
        monitor_id,
        state_assessment_id=assessment_id,
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        task_id=ctx["task_id"],
        cutoff_sequence=cutoff,
        trigger_source="auto_state",
    )
    assert pending_help.get("skipped") is True
    assert pending_help["reason"] == "help_request_race_grace"
    help_check = pending_help["validation"]["help_request_check"]
    assert help_check["guard_evaluated"] is True
    assert help_check["guard_blocked"] is True
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"] == 0
    db.execute("UPDATE help_requests SET status='FAILED' WHERE id=?", (queued_help_id,))

    ctx, monitor_id, assessment_id, cutoff = _seed_completed_monitor_for_intervention(
        db,
        session_no=731,
    )
    external_token = RoomLeaseService.acquire(ctx["group_id"], 99999)
    locked_result = InterventionService.execute(
        monitor_id,
        state_assessment_id=assessment_id,
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        task_id=ctx["task_id"],
        cutoff_sequence=cutoff,
        trigger_source="auto_state",
    )
    assert locked_result.get("skipped") is True
    assert "AI_INTERVENING" in locked_result["reason"]
    assert db.query_one(
        "SELECT state, lock_token FROM groups WHERE id=?",
        (ctx["group_id"],),
    )["lock_token"] == external_token
    RoomLeaseService.release(ctx["group_id"], external_token)

    ctx, monitor_id, assessment_id, cutoff = _seed_completed_monitor_for_intervention(
        db,
        session_no=732,
    )

    def timeout_review(_context):
        raise TimeoutError("mock strategy timeout")

    monkeypatch.setattr(service_module, "review_strategy_context", timeout_review)
    timeout_result = InterventionService.execute(
        monitor_id,
        state_assessment_id=assessment_id,
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        task_id=ctx["task_id"],
        cutoff_sequence=cutoff,
        trigger_source="auto_state",
    )
    assert timeout_result["reason"] == "intervention_execute_exception"
    assert db.query_one("SELECT status FROM intervention_runs WHERE monitor_run_id=?", (monitor_id,))["status"] == "FAILED"
    assert db.query_one("SELECT state, lock_token FROM groups WHERE id=?", (ctx["group_id"],))["state"] == "OPEN"

    ctx, monitor_id, assessment_id, cutoff = _seed_completed_monitor_for_intervention(
        db,
        session_no=733,
    )
    original_publish = InterventionService._publish
    monkeypatch.setattr(service_module, "review_strategy_context", _intervene_review_result)
    monkeypatch.setattr(
        InterventionService,
        "_publish",
        staticmethod(lambda **kwargs: {"ok": False, "reason": "publish_failed"}),
    )
    publish_failed = InterventionService.execute(
        monitor_id,
        state_assessment_id=assessment_id,
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        task_id=ctx["task_id"],
        cutoff_sequence=cutoff,
        trigger_source="auto_state",
    )
    assert publish_failed["reason"] == "publish_failed"
    assert db.query_one("SELECT status FROM intervention_runs WHERE monitor_run_id=?", (monitor_id,))["status"] == "FAILED"
    assert db.query_one("SELECT state, lock_token FROM groups WHERE id=?", (ctx["group_id"],))["state"] == "OPEN"

    monkeypatch.setattr(InterventionService, "_publish", staticmethod(original_publish))

    ctx, monitor_id, assessment_id, cutoff = _seed_completed_monitor_for_intervention(
        db,
        session_no=734,
    )

    def end_session_review(context):
        db.execute("UPDATE experiment_sessions SET status='ended' WHERE id=?", (ctx["session_id"],))
        return _intervene_review_result(context)

    monkeypatch.setattr(service_module, "review_strategy_context", end_session_review)
    ended_result = InterventionService.execute(
        monitor_id,
        state_assessment_id=assessment_id,
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        task_id=ctx["task_id"],
        cutoff_sequence=cutoff,
        trigger_source="auto_state",
    )
    assert ended_result["published"] is False
    assert ended_result["reason"] == "session_not_active"
    assert db.query_one("SELECT status, skip_reason FROM intervention_runs WHERE monitor_run_id=?", (monitor_id,))["status"] == "SKIPPED"
    assert db.query_one("SELECT state, lock_token FROM groups WHERE id=?", (ctx["group_id"],))["state"] == "OPEN"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (ctx["group_id"],),
    )["c"] == 0

    ctx, monitor_id, assessment_id, cutoff = _seed_completed_monitor_for_intervention(
        db,
        session_no=735,
    )

    def freeze_doc_review(context):
        db.execute(
            """
            INSERT INTO collaborative_documents(
                group_id, task_id, session_no, session_id, title, status, created_by,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                ctx["group_id"],
                ctx["task_id"],
                ctx["session_no"],
                ctx["session_id"],
                "Frozen",
                "locked",
                ctx["students"][0][0],
                db.now_str(),
                db.now_str(),
            ),
        )
        return _intervene_review_result(context)

    monkeypatch.setattr(service_module, "review_strategy_context", freeze_doc_review)
    frozen_result = InterventionService.execute(
        monitor_id,
        state_assessment_id=assessment_id,
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        task_id=ctx["task_id"],
        cutoff_sequence=cutoff,
        trigger_source="auto_state",
    )
    assert frozen_result["published"] is False
    assert frozen_result["reason"] == "document_locked"
    assert db.query_one("SELECT status, skip_reason FROM intervention_runs WHERE monitor_run_id=?", (monitor_id,))["status"] == "SKIPPED"
    assert db.query_one("SELECT state, lock_token FROM groups WHERE id=?", (ctx["group_id"],))["state"] == "OPEN"
