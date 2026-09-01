# -*- coding: utf-8 -*-
"""Batch 6 final regression coverage for the single-call strategy agent."""

import inspect
import json
import time

from tests.helpers import create_group, create_student, seed_running_session


SCRIPTED_MESSAGES = [
    "大家先把第一周任务拆一下：我们要给校园共享学习空间做优化方案，我先记录问题和证据。",
    "我负责整理图书馆自习区的拥挤、插座和预约数据，先看证据再提方案。",
    "我补充学生访谈角度，比如小组讨论区不够和安静区被打扰的问题。",
    "我来做方案比较表：空间布局、预约规则、成本和风险，最后一起选优先方案。",
    "预约制可以统计使用率，但我们也要比较开放讨论区。",
    "先把证据来源分成观察、访谈和已有数据三类吧。",
    "我补一个评价标准：是否同时支持个人学习和小组协作。",
    "如果大家同意，我先列提纲：问题、证据、方案、预期效果。",
    "我觉得还要把成本和可维护性加进比较表。",
    "我们先不下结论，等证据列完再选主方案。",
    "这个任务有点重复，但先继续把材料归类。",
    "先停一下，我有点无聊了，感觉这个共享空间方案怎么写都差不多。",
    "我也不太想继续讨论，刚才看到食堂新窗口了，等会儿吃什么？",
    "要不先聊会儿游戏放松一下，反正最后能交上去就行。",
    "我拉回来一点，刚才说的预约制还需要学生访谈支持。",
    "我们可以先把跑题的内容放一边，继续看共享空间问题。",
    "我现在只确定一个问题：预约制会不会让临时自习的人更不方便？",
    "小组讨论区确实经常被占满，这个可以当观察证据。",
    "我还没想好访谈材料怎么用，只能先说小组讨论区确实经常被占满。",
    "访谈材料怎么用，我觉得可以先按安静自习和小组讨论两类需求整理。",
    "我也有点不确定，但比较标准可以从证据、成本、可执行性开始。",
    "我也有点不确定，我们是不是需要先把比较标准列出来？",
    "我还是卡住，不知道怎么把观察到的问题和最终优化方案连起来。",
    "谁负责证据，谁负责成本？现在没有下一步，我有点不知道该写哪部分。",
    "我们好像越整理越乱，空间布局、预约规则和费用都混在一起。",
    "我不知道该先写哪一段，感觉结论也写不出来。",
    "要不先问 SERA 帮我们理一下下一步？",
    "如果有人能提示一下，我们可能知道先拆哪部分。",
    "我先把两个方案写在表格左边，等下补证据。",
    "不对，你这个全部改成预约制根本没考虑临时自习的人，方案不合理。",
    "你也别一直否定，我觉得开放讨论区才是重点，你的方案太片面。",
    "别乱说，你没有看清任务要求，成本也完全没算。",
    "强行推讨论区肯定不行，你的依据也不够。",
    "先别争，我们把预约制和开放讨论区两个方案按证据、成本、可执行性分别打分。",
    "可以，我来记录评分，M2补预约数据，M3补讨论区访谈，我整理风险。",
    "我负责预约方案的证据：高峰时段座位紧张、插座不足，以及预约能缓解排队。",
    "我负责讨论区方案的证据：访谈里多人提到小组讨论没地方，而且容易影响安静区。",
    "阶段总结：我们保留两个方案，主方案是分区优化，预约制作为高峰时段辅助规则。",
    "我把比较表整理成三列：问题证据、优化动作、可能风险，最后给出优先级。",
    "最终结论可以写：先调整分区和预约规则，再根据试点数据决定是否扩大实施。",
    "我们还需要最后检查成果要求是不是都覆盖了。",
    "如果模型生成到这里被截断，就不能发布给学生。",
]


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


def _seed_scripted_group(db, *, upto=42, session_no=61):
    seeded = seed_running_session(db, session_no=session_no, member_count=4)
    _enable_strategy_session(db, seeded["session_id"])
    db.execute(
        """
        UPDATE learning_tasks
        SET title=?,
            description=?,
            question=?,
            task_goal=?,
            output_requirement=?,
            key_concepts_json=?,
            expected_dimensions_json=?,
            task_payload_json=?,
            agent_intervention_enabled=1
        WHERE id=?
        """,
        (
            "校园共享学习空间优化",
            "比较预约制、分区优化和开放讨论区等方案。",
            "怎样改进校园共享学习空间以兼顾个人学习和小组协作？",
            "形成一个有证据支持、可执行的优化建议。",
            "提交包含问题证据、优化动作、风险和优先级的方案比较表。",
            json.dumps(["证据比较", "空间优化"], ensure_ascii=False),
            json.dumps(["证据", "成本", "可执行性"], ensure_ascii=False),
            json.dumps({"evaluation_criteria": ["证据清楚", "比较完整"]}, ensure_ascii=False),
            seeded["task_id"],
        ),
    )
    for index, text in enumerate(SCRIPTED_MESSAGES[:upto], start=1):
        student_id = seeded["students"][(index - 1) % len(seeded["students"])][0]
        message = db.create_message(
            seeded["group_id"],
            student_id,
            text,
            role="student",
            client_message_id=f"batch6-{session_no}-{index}",
        )
        assert message["sequence"] == index
    return seeded


def _monitor_run(db, group_id, cutoff, *, state, score=0.86, trigger_type="new_message", evidence=None):
    session = db.query_one(
        "SELECT id, session_no, task_id FROM experiment_sessions WHERE status='running' ORDER BY id DESC LIMIT 1"
    )
    session_id = session["id"] if session else None
    session_no = session["session_no"] if session else None
    task_id = session["task_id"] if session else None
    evidence = evidence or [cutoff]
    rule_result = {
        "winning_state_code": state,
        "winning_score": score,
        "assessment_status": "state_detected",
        "signals": {"batch6_fixed_script": True},
        "trigger_sequence": cutoff,
        "evidence_sequences": evidence,
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
            cutoff,
            trigger_type,
            json.dumps(rule_result, ensure_ascii=False),
            state,
            score,
            "completed",
            "batch6_regression",
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
            session_id,
            task_id,
            session_no,
            state,
            "confirmed",
            score,
            1 if state not in {"positive_collaboration", "unknown"} else 0,
            "batch6 fixed assessment",
            json.dumps({"decision_source": "state_llm"}, ensure_ascii=False),
            json.dumps({"evidence_sequences": evidence}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    rule_result["monitor_audit"] = {"state_assessment_id": assessment_id}
    db.execute(
        "UPDATE monitor_runs SET rule_result_json=? WHERE id=?",
        (json.dumps(rule_result, ensure_ascii=False), monitor_run_id),
    )
    return monitor_run_id


def _strategy_context(db, seeded, cutoff, *, state, evidence=None):
    monitor_run_id = _monitor_run(
        db,
        seeded["group_id"],
        cutoff,
        state=state,
        evidence=evidence or [cutoff],
    )
    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    return ContextBuilder.build_strategy_review_context(
        seeded["group_id"],
        seeded["session_id"],
        monitor_run_id,
        cutoff,
    )


def _patch_review(monkeypatch, review_result):
    import services.intervention_pipeline_v2.intervention_service as service_module

    calls = []

    def fake_review(context):
        calls.append(context)
        return dict(review_result)

    monkeypatch.setattr(service_module, "review_strategy_context", fake_review)
    return calls


def _group_lock_row(db, group_id):
    return db.query_one(
        "SELECT state, lock_token, lock_expires_at, active_intervention_run_id, last_intervention_at FROM groups WHERE id=?",
        (group_id,),
    )


def test_batch6_fixed_script_review_windows_and_local_decisions(db_and_app):
    db, _app, _client = db_and_app
    seeded = _seed_scripted_group(db, upto=39)
    group_id = seeded["group_id"]

    from services.discussion_pipeline_v2.trigger_policy import TriggerPolicy
    from services.intervention_pipeline_v2.strategy_review_service import validate_strategy_review_output

    should_call, reason = TriggerPolicy.should_call_llm(
        group_id,
        10,
        {"winning_state_code": "positive_collaboration", "winning_score": 0.91},
    )
    assert should_call is False
    assert reason == "positive_collaboration_no_strategy_review_needed"

    off_task_context = _strategy_context(db, seeded, 13, state="task_detached", evidence=[12, 13])
    assert {12, 13}.issubset(set(off_task_context["input_message_sequences"]))
    assert off_task_context["task"]["title"] == "校园共享学习空间优化"

    for cutoff, evidence in ((20, [20]), (22, [22])):
        context = _strategy_context(db, seeded, cutoff, state="task_detached", evidence=evidence)
        result = validate_strategy_review_output(
            {
                "decision": "PASS",
                "strategy": None,
                "student_message": "",
                "teacher_reason": "任务内不确定但在推进。",
            },
            context,
        )
        assert result["valid"] is True

    conflict_context = _strategy_context(db, seeded, 33, state="conflict_tension", evidence=[30, 31, 33])
    intervene = validate_strategy_review_output(
        {
            "decision": "INTERVENE",
            "strategy": "v2_conflict_evidence",
            "student_message": "先不判断谁对谁错，请把两个方案各写出一个依据再比较。",
            "teacher_reason": "持续相互否定。",
        },
        conflict_context,
    )
    assert intervene["valid"] is True

    recovery_context = _strategy_context(db, seeded, 39, state="conflict_tension", evidence=[34, 39])
    recovery_pass = validate_strategy_review_output(
        {
            "decision": "PASS",
            "strategy": None,
            "student_message": "",
            "teacher_reason": "已按任务比较推进。",
        },
        recovery_context,
    )
    assert recovery_pass["valid"] is True


def test_batch6_pass_does_not_publish_or_reset_context_boundary(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = _seed_scripted_group(db, upto=22, session_no=62)
    group_id = seeded["group_id"]
    monitor_run_id = _monitor_run(db, group_id, 22, state="task_detached", evidence=[22])
    calls = _patch_review(
        monkeypatch,
        {
            "ok": True,
            "action": "PASS",
            "decision": "PASS",
            "strategy": None,
            "student_message": "",
            "teacher_reason": "先列比较标准属于任务推进。",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "latency_ms": 14},
            "validation": {"valid": True},
        },
    )
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    result = InterventionService.execute(monitor_run_id)

    assert result["published"] is False
    assert result["steps"]["pass_recorded"] is True
    assert len(calls) == 1
    assert calls[0]["context_boundary"]["from_sequence"] == 1

    run = db.query_one(
        "SELECT status, decision, skip_reason, generated_message, evidence_sequences_json FROM intervention_runs WHERE monitor_run_id=?",
        (monitor_run_id,),
    )
    assert run["status"] == "PASS"
    assert run["decision"] == "PASS"
    assert run["skip_reason"] == "pass_no_intervention"
    assert run["generated_message"] is None
    assert json.loads(run["evidence_sequences_json"]) == [22]
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (group_id,))["c"] == 0
    assert _group_lock_row(db, group_id)["last_intervention_at"] is None

    student_id = seeded["students"][0][0]
    followup = db.create_message(group_id, student_id, "PASS 后继续补一个任务内证据。", role="student")
    next_context = _strategy_context(db, seeded, followup["sequence"], state="task_detached", evidence=[followup["sequence"]])
    assert next_context["previous_strategy_intervention"] is None
    assert next_context["context_boundary"]["from_sequence"] == 1


def test_batch6_intervene_publishes_once_and_resets_boundary(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = _seed_scripted_group(db, upto=33, session_no=63)
    group_id = seeded["group_id"]
    monitor_run_id = _monitor_run(db, group_id, 33, state="conflict_tension", evidence=[30, 31, 33])
    message = "先不判断谁对谁错，请把两个方案各写出一个依据再比较。"
    calls = _patch_review(
        monkeypatch,
        {
            "ok": True,
            "action": "INTERVENE",
            "decision": "INTERVENE",
            "strategy": "v2_conflict_evidence",
            "student_message": message,
            "teacher_reason": "持续相互否定。",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "latency_ms": 18},
            "validation": {"valid": True},
        },
    )
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    result = InterventionService.execute(monitor_run_id)

    assert result["published"] is True
    assert len(calls) == 1

    run = db.query_one(
        """
        SELECT id, status, generated_message, selected_strategy_id,
               input_message_sequences_json, evidence_sequences_json
        FROM intervention_runs WHERE monitor_run_id=?
        """,
        (monitor_run_id,),
    )
    assert run["status"] == "PUBLISHED"
    assert run["generated_message"] == message
    assert run["selected_strategy_id"] == "v2_conflict_evidence"
    assert set(json.loads(run["evidence_sequences_json"])).issubset(
        set(json.loads(run["input_message_sequences_json"]))
    )

    agent_message = db.query_one(
        "SELECT id, sequence, content, agent_type, strategy_id FROM messages WHERE intervention_run_id=?",
        (run["id"],),
    )
    assert agent_message["content"] == message
    assert agent_message["agent_type"] == "strategy"
    assert agent_message["strategy_id"] == "v2_conflict_evidence"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE intervention_run_id=?", (run["id"],))["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM intervention_runs WHERE monitor_run_id=?", (monitor_run_id,))["c"] == 1
    assert _group_lock_row(db, group_id)["last_intervention_at"] is not None

    student_id = seeded["students"][0][0]
    next_student = db.create_message(group_id, student_id, "收到，我们回到证据比较。", role="student")
    next_context = _strategy_context(db, seeded, next_student["sequence"], state="conflict_tension", evidence=[next_student["sequence"]])
    assert next_context["previous_strategy_intervention"]["sequence"] == agent_message["sequence"]
    assert next_context["context_boundary"]["from_sequence"] == agent_message["sequence"] + 1
    assert next_context["input_message_sequences"] == [next_student["sequence"]]


def test_batch6_student_help_priority_blocks_auto_review(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = _seed_scripted_group(db, upto=28, session_no=64)
    group_id = seeded["group_id"]
    requester_id = seeded["students"][0][0]
    monitor_run_id = _monitor_run(db, group_id, 28, state="blocked_frustration", evidence=[23, 28])

    db.execute(
        """
        INSERT INTO help_requests(group_id, requester_id, task_id, session_no, session_id, status, request_text, created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            requester_id,
            seeded["task_id"],
            seeded["session_no"],
            seeded["session_id"],
            "QUEUED",
            "我们需要下一步提示。",
            db.now_str(),
        ),
    )
    calls = _patch_review(
        monkeypatch,
        {
            "ok": True,
            "action": "INTERVENE",
            "decision": "INTERVENE",
            "strategy": "v2_frustration_identify",
            "student_message": "先把当前最卡的一点写出来，再决定下一步。",
            "teacher_reason": "卡住。",
        },
    )
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["rejected"] is True
    assert result["validation"]["reason"] == "help_request_race_grace"
    help_check = result["validation"]["help_request_check"]
    assert help_check["guard_evaluated"] is True
    assert help_check["guard_blocked"] is True
    assert help_check["reason_code"] == "help_request_race_grace"
    assert calls == []
    skipped = db.query_one("SELECT status, skip_reason FROM intervention_runs WHERE group_id=?", (group_id,))
    assert skipped["status"] == "SKIPPED"
    assert skipped["skip_reason"] == "help_request_race_grace"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (group_id,))["c"] == 0


def test_batch6_incomplete_review_is_audited_without_publish(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = _seed_scripted_group(db, upto=42, session_no=65)
    group_id = seeded["group_id"]
    monitor_run_id = _monitor_run(db, group_id, 42, state="conflict_tension", evidence=[42])
    calls = _patch_review(
        monkeypatch,
        {
            "ok": False,
            "action": "fail_without_student_message",
            "reason": "incomplete_message",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "finish_reason": "length"},
            "validation": {"valid": False, "reason": "incomplete_message"},
        },
    )
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))
    result = InterventionService.execute(monitor_run_id)

    assert result["steps"]["failed_without_student_message"] is True
    assert result["reason"] == "incomplete_message"
    assert len(calls) == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (group_id,))["c"] == 0
    run = db.query_one(
        "SELECT status, failure_reason, generated_message FROM intervention_runs WHERE monitor_run_id=?",
        (monitor_run_id,),
    )
    assert run["status"] == "FAILED"
    assert run["failure_reason"] == "incomplete_message"
    assert run["generated_message"] is None
    monitor = db.query_one("SELECT review_error FROM monitor_runs WHERE id=?", (monitor_run_id,))
    assert monitor["review_error"] == "incomplete_message"
    lock = _group_lock_row(db, group_id)
    assert lock["state"] == "OPEN"
    assert lock["lock_token"] is None
    assert lock["active_intervention_run_id"] is None


def test_batch6_local_performance_and_cleanup_guards(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    seeded = _seed_scripted_group(db, upto=40, session_no=66)
    group_id = seeded["group_id"]
    monitor_run_id = _monitor_run(db, group_id, 40, state="task_detached", evidence=[40])

    from services.intervention_pipeline_v2.context_builder import ContextBuilder
    from services.intervention_pipeline_v2.intervention_service import InterventionService

    start = time.perf_counter()
    context = ContextBuilder.build_strategy_review_context(
        group_id,
        seeded["session_id"],
        monitor_run_id,
        40,
    )
    context_ms = (time.perf_counter() - start) * 1000

    calls = _patch_review(
        monkeypatch,
        {
            "ok": True,
            "action": "PASS",
            "decision": "PASS",
            "strategy": None,
            "student_message": "",
            "teacher_reason": "任务内收尾检查。",
            "profile": "strategy_review_and_generation",
            "prompt_version": "strategy_review_decision_v3",
            "payload": {"messages": []},
            "llm_result": {"success": True, "latency_ms": 16},
            "validation": {"valid": True},
        },
    )
    monkeypatch.setattr(InterventionService, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(InterventionService, "is_dry_run", staticmethod(lambda: False))

    execute_start = time.perf_counter()
    result = InterventionService.execute(monitor_run_id)
    execute_ms = (time.perf_counter() - execute_start) * 1000

    assert context["input_message_sequences"][-1] == 40
    assert context_ms < 200
    assert execute_ms < 1000
    assert result["published"] is False
    assert len(calls) == 1
    assert _group_lock_row(db, group_id)["lock_token"] is None

    group_two = create_group(db, name="Batch6 Parallel Group", code="B6-PAR")
    student_two, _key = create_student(db, group_two)
    other_message = db.create_message(group_two, student_two, "另一个小组继续讨论，不应被前一组锁阻塞。", role="student")
    other_monitor = _monitor_run(db, group_two, other_message["sequence"], state="task_detached", evidence=[other_message["sequence"]])
    other_context = ContextBuilder.build_strategy_review_context(
        group_two,
        seeded["session_id"],
        other_monitor,
        other_message["sequence"],
    )
    assert other_context["group_id"] == group_two
    assert other_context["input_message_sequences"] == [other_message["sequence"]]

    service_source = inspect.getsource(InterventionService)
    assert "intervention_generator" not in service_source
    assert not hasattr(InterventionService, "_retry_llm_shorten")
    assert not hasattr(InterventionService, "_call_llm")
