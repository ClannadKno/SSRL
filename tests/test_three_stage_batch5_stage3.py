# -*- coding: utf-8 -*-
"""Batch 5 coverage for Stage 3 strategy selection and text generation."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest

from tests.helpers import seed_running_session


class _FakeLlmResult:
    def __init__(self, output, *, success=True, failure_type=None, finish_reason=None):
        self.success = success
        self.output = output
        self.raw_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        self.model_name = "batch5-stage3-model"
        self.profile_name = "strategy_review_and_generation"
        self.latency_ms = 3
        self.attempt_count = 1
        self.token_usage = {"completion_tokens": 80}
        self.failure_type = failure_type
        self.failure_message = failure_type
        self.finish_reason = finish_reason


class _FakeGateway:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def call(
        self,
        profile_name,
        payload,
        response_type="json",
        *,
        max_attempts_override=None,
    ):
        self.calls.append(
            {
                "profile_name": profile_name,
                "payload": payload,
                "response_type": response_type,
                "max_attempts_override": max_attempts_override,
            }
        )
        item = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        if isinstance(item, _FakeLlmResult):
            return item
        return _FakeLlmResult(item)


@pytest.fixture
def batch5_env(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=9505, member_count=1)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (scope["session_id"], scope["group_id"], db.now_str(), db.now_str(), db.now_str()),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    return db, scope


def _add_message(db, scope, sequence, content, *, role="student"):
    return db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_id, session_no, task_id, discussion_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["student_id"],
            content,
            sequence,
            role,
            role,
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            db.now_str(),
        ),
    )


def _output(strategy_id, text, *, supporting=None, reason="selected from candidates"):
    return {
        "schema_version": "stage3.v1",
        "selected_strategy_id": strategy_id,
        "selected_strategy_name": "model supplied name",
        "selected_strategy_type": "model supplied type",
        "supporting_strategy_ids": list(supporting or []),
        "matched_trigger_features": ["feature from evidence"],
        "inapplicable_candidate_ids": [],
        "strategy_application_plan": "choose the strategy first, then write one group-facing sentence",
        "selection_reason": reason,
        "intervention_text": text,
        "expected_student_action": "complete one small next action",
    }


def _core_output(strategy_id, text):
    return {
        "selected_strategy_id": strategy_id,
        "intervention_text": text,
    }


def _insert_stage2_pipeline(
    db,
    scope,
    *,
    canonical="interpersonal_conflict",
    candidates=None,
    should_intervene=1,
    inhibition_strategy_id=None,
    secondary_tags=None,
    stage3_status="PENDING",
    locked=False,
):
    strategy_library = importlib.import_module("services.three_stage_strategy_library")
    library = strategy_library.load_strategy_library()
    if candidates is None:
        candidates = ["ER-001", "EE-001", "SS-004", "ER-007"]
    secondary_tags = list(secondary_tags or [])
    msg1 = _add_message(db, scope, 1, "我们先看证据。")
    msg2 = _add_message(db, scope, 2, "你别总否定别人，方案哪里不行说清楚。")
    msg3 = _add_message(db, scope, 3, "我担心这个条件没有被比较。")
    lock_token = f"stage3-lock-{scope['group_id']}-{canonical}" if locked else None
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid,
            group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority,
            input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
            stage1_status, stage2_status,
            raw_sub_state_code, canonical_sub_state_code,
            secondary_sub_state_tags_json, sub_state_confidence, sub_state_reason,
            sub_state_start_sequence, sub_state_end_sequence,
            sub_state_evidence_message_ids_json, all_state_segments_json,
            detected_self_regulation, should_intervene, inhibition_strategy_id,
            stage3_status, strategy_candidate_ids_json,
            strategy_library_version, strategy_library_hash,
            room_lock_token, room_lock_acquired_at,
            publish_status, final_status,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"batch5-stage2-{scope['group_id']}-{canonical}-{locked}-{stage3_status}",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "message_count_periodic",
            200,
            1,
            3,
            3,
            "SUCCEEDED",
            "SUCCEEDED",
            canonical,
            canonical,
            dumps(secondary_tags),
            0.91,
            "active precise sub-state",
            1,
            3,
            "[2,3]",
            dumps([
                {
                    "canonical_sub_state": canonical,
                    "secondary_tags": secondary_tags,
                    "evidence_message_ids": [2, 3],
                }
            ]),
            0,
            should_intervene,
            inhibition_strategy_id,
            stage3_status,
            dumps(candidates),
            library.version,
            library.library_hash,
            lock_token,
            db.now_str() if locked else None,
            "NOT_READY",
            "PENDING_STAGE3" if should_intervene else "SUPPRESSED",
            f"batch5:pipeline:{scope['group_id']}:{canonical}:{locked}:{stage3_status}",
            db.now_str(),
            db.now_str(),
        ),
    )
    if locked:
        lock_expires_at = (datetime.now() + timedelta(seconds=75)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.execute(
            """
            UPDATE groups
            SET state='AI_INTERVENING', lock_token=?, lock_expires_at=?,
                active_intervention_run_id=?, auto_intervention_enabled=1
            WHERE id=?
            """,
            (lock_token, lock_expires_at, -pipeline_id, scope["group_id"]),
        )
        db.execute(
            """
            UPDATE experiment_sessions
               SET strategy_agent_enabled=1, agent_intervention_enabled=1
             WHERE id=?
            """,
            (scope["session_id"],),
        )
    db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, raw_sub_state_code, canonical_sub_state_code,
            strategy_pipeline_run_id, should_intervene, source_stage,
            segment_kind, start_message_id, end_message_id,
            start_sequence, end_sequence, evidence_message_ids_json,
            evidence_sequences, confidence, source, assessment_status,
            segment_order, is_active_at_batch_end,
            is_finalized, dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'message_range',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            "conflict_tension" if should_intervene else "positive_collaboration",
            canonical,
            canonical,
            pipeline_id,
            should_intervene,
            "stage2",
            msg1,
            msg3,
            1,
            3,
            "[2,3]",
            "[2,3]",
            0.91,
            "llm",
            "confirmed",
            0,
            1,
            1,
            f"batch5:segment:{pipeline_id}",
            db.now_str(),
            db.now_str(),
        ),
    )
    return pipeline_id


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_stage3_context_contains_fixed_state_task_messages_and_strategy_history(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")

    context = stage3.build_stage3_context(pipeline_id)
    payload = stage3.build_stage3_payload(context)
    payload_context = json.loads(payload["messages"][1]["content"])

    assert context["state_is_fixed"] is True
    assert context["should_intervene"] is True
    assert context["stage2_active_sub_state"]["canonical_sub_state"] == "interpersonal_conflict"
    assert context["task_info"]["task_name"] == "Task 9505"
    assert [row["sequence"] for row in context["evidence_messages"]] == [2, 3]
    assert context["allowed_strategy_ids"] == ["ER-001", "EE-001", "SS-004", "ER-007"]
    assert context["candidate_strategy_definitions"][0]["strategy_id"] == "ER-001"
    assert context["candidate_strategy_definitions"][0]["template_examples"]
    assert context["generation_requirements"]["must_reference_selected_strategy_templates"] is True
    assert context["generation_requirements"]["requires_light_emotional_warmth"] is True
    assert context["generation_requirements"]["emotional_warmth_components"] == [
        "理解感",
        "鼓励感",
        "陪伴感",
    ]
    assert context["prompt_version"] == "stage3_strategy_selection_v6"
    assert context["messages_since_last_strategy_intervention"]
    assert payload_context["state_is_fixed"] is True
    assert payload_context["should_intervene"] is True
    assert payload_context["evidence_message_ids"] == [2, 3]
    assert payload_context["task_context"]["task_name"] == "Task 9505"
    assert payload_context["message_context"]["evidence_messages"]
    assert "discussion_stage" in payload_context["discussion_context"]
    assert "remaining_seconds" in payload_context["discussion_context"]
    assert payload_context["recent_agent_intervention"] is None
    candidate = payload_context["candidate_strategy_definitions"][0]
    assert {
        "strategy_id",
        "strategy_name",
        "mechanism",
        "applicable_situation",
        "reference_utterances",
        "special_generation_guidance",
    }.issubset(candidate)
    assert candidate["mechanism"]
    assert candidate["applicable_situation"]
    assert candidate["reference_utterances"]
    assert candidate["special_generation_guidance"]
    assert payload_context["output_contract"]["only_fields"] == [
        "selected_strategy_id",
        "intervention_text",
    ]
    assert "你不能修改、替换或重新判断该子状态" in payload["messages"][0]["content"]
    assert "状态已经由上一阶段确定，不得重新判断" in payload["messages"][0]["content"]
    assert "当前必须从 allowed_strategy_ids 中选择一个策略" in payload["messages"][0]["content"]
    assert "不需要输出候选策略比较" in payload["messages"][0]["content"]
    assert "轻微情感温度" in payload["messages"][0]["content"]
    assert payload["max_tokens"] == 600
    assert payload["temperature"] == 0.2


def test_stage3_profile_uses_compact_low_temperature_budget():
    gateway_module = importlib.import_module("services.llm_gateway")
    profile = gateway_module.BUILTIN_PROFILES["strategy_review_and_generation"]

    assert 300 <= profile["max_tokens"] <= 600
    assert profile["temperature"] <= 0.3
    assert profile["read_timeout"] <= 20
    assert profile["retries"] == 1


@pytest.mark.parametrize(
    ("canonical", "candidates"),
    [
        ("confusion", ["EA-001", "ER-005", "EE-006", "SS-003"]),
        ("frustration", ["ER-002", "EE-003", "SS-006", "EA-007"]),
        ("interpersonal_conflict", ["ER-001", "EE-001", "SS-004", "ER-007"]),
        ("off_topic_unregulated", ["ER-003"]),
        ("burnout", ["ER-008"]),
        ("individual_marginalization", ["EA-003", "EE-005", "SS-002"]),
    ],
)
def test_stage3_prompt_contract_is_fixed_for_required_substates(
    batch5_env, canonical, candidates
):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical=canonical,
        candidates=candidates,
    )
    _add_message(db, scope, 4, "我们刚才的介入先放在这里。", role="agent")
    stage3 = importlib.import_module("services.three_stage_stage3")

    context = stage3.build_stage3_context(pipeline_id)
    payload = stage3.build_stage3_payload(context)
    payload_context = json.loads(payload["messages"][1]["content"])

    assert payload_context["stage2_active_sub_state"]["canonical_sub_state"] == canonical
    assert payload_context["state_is_fixed"] is True
    assert payload_context["should_intervene"] is True
    assert payload_context["allowed_strategy_ids"] == candidates
    assert payload_context["task_context"]
    assert payload_context["message_context"]["evidence_messages"]
    assert payload_context["evidence_message_ids"] == [2, 3]
    assert payload_context["recent_agent_intervention"]["content"].startswith("我们刚才")
    assert {
        "strategy_id",
        "strategy_name",
        "mechanism",
        "applicable_situation",
        "reference_utterances",
        "special_generation_guidance",
    }.issubset(payload_context["candidate_strategy_definitions"][0])
    if canonical == "frustration":
        er002 = next(
            item
            for item in payload_context["candidate_strategy_definitions"]
            if item["strategy_id"] == "ER-002"
        )
        assert "发现约束" in er002["special_generation_guidance"]


def test_stage3_context_limits_full_persisted_candidates_to_state_route_pool(batch5_env):
    db, scope = batch5_env
    strategy_library = importlib.import_module("services.three_stage_strategy_library")
    all_strategy_ids = [
        item.strategy_id
        for item in strategy_library.load_strategy_library().definitions
    ]
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="burnout",
        candidates=all_strategy_ids,
    )
    stage3 = importlib.import_module("services.three_stage_stage3")

    context = stage3.build_stage3_context(pipeline_id)
    payload_context = json.loads(
        stage3.build_stage3_payload(context)["messages"][1]["content"]
    )

    assert len(all_strategy_ids) == 28
    assert context["strategy_route"]["strategy_pool"] == ["ER-008"]
    assert context["allowed_strategy_ids"] == ["ER-008"]
    assert [
        item["strategy_id"]
        for item in context["candidate_strategy_definitions"]
    ] == ["ER-008"]
    assert [
        item["strategy_id"]
        for item in context["strategy_usage_history"]["ranked_candidates"]
    ] == ["ER-008"]
    assert payload_context["allowed_strategy_ids"] == ["ER-008"]
    assert [
        item["strategy_id"]
        for item in payload_context["candidate_strategy_definitions"]
    ] == ["ER-008"]
    assert "ER-001" not in stage3.build_stage3_payload(context)["messages"][1]["content"]


def test_stage3_context_uses_overlay_route_pool_for_stage_achievement(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="execution_progress",
        secondary_tags=["stage_achievement"],
        candidates=["SS-007"],
    )
    stage3 = importlib.import_module("services.three_stage_stage3")

    context = stage3.build_stage3_context(pipeline_id)

    assert context["stage2_active_sub_state"]["canonical_sub_state"] == "execution_progress"
    assert context["stage2_active_sub_state"]["secondary_tags"] == ["stage_achievement"]
    assert context["strategy_route"]["route_overlay_tag"] == "stage_achievement"
    assert context["strategy_route"]["strategy_pool"] == ["SS-007"]
    assert context["allowed_strategy_ids"] == ["SS-007"]
    assert [
        item["strategy_id"]
        for item in context["strategy_usage_history"]["ranked_candidates"]
    ] == ["SS-007"]


def test_stage3_success_persists_strategy_and_text_without_publishing(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=True)
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _core_output(
                "ER-001",
                "分歧先回到证据上，大家各说一个最担心的条件，再一起看方案怎么调整。",
            )
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(pipeline_id, gateway=gateway)

    assert result["stage3_status"] == "SUCCEEDED"
    assert result["selected_strategy_id"] == "ER-001"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["max_attempts_override"] == 1
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["stage3_status"] == "SUCCEEDED"
    assert row["selected_strategy_id"] == "ER-001"
    assert row["selected_strategy_name"] == "冲突认知重评"
    assert json.loads(row["supporting_strategy_ids_json"]) == []
    assert json.loads(row["matched_trigger_features_json"]) == []
    assert json.loads(row["inapplicable_candidate_ids_json"]) == []
    assert row["strategy_selection_reason"] is None
    assert row["strategy_application_plan"] is None
    assert row["validated_intervention_text"].endswith("。")
    assert row["publish_status"] == "NOT_READY"
    assert row["final_status"] == "PENDING_DECISION_GATE"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 0
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM intervention_runs WHERE strategy_pipeline_run_id=?",
        (pipeline_id,),
    )["c"] == 0
    segment = db.query_one(
        "SELECT selected_strategy_id FROM collaboration_state_segments WHERE strategy_pipeline_run_id=?",
        (pipeline_id,),
    )
    assert segment["selected_strategy_id"] == "ER-001"
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert group["state"] == "AI_INTERVENING"
    assert group["lock_token"]
    assert group["active_intervention_run_id"] == -pipeline_id


def test_stage3_latency_telemetry_records_compact_attempt_metadata(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _core_output(
                "ER-001",
                "分歧先回到证据上，大家各说一个最担心的条件。",
            )
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "SUCCEEDED"
    rows = db.query_all(
        """
        SELECT event, elapsed_ms, details_json
        FROM strategy_pipeline_latency_events
        WHERE pipeline_run_id=? AND stage='stage3'
        ORDER BY id
        """,
        (pipeline_id,),
    )
    attempt_finished = next(
        row for row in rows if row["event"] == "stage3_llm_attempt_1_finished"
    )
    attempt_details = json.loads(attempt_finished["details_json"])
    assert attempt_details["prompt_version"] == "stage3_strategy_selection_v6"
    assert attempt_details["attempt_type"] == "initial"
    assert attempt_details["max_tokens"] == 600
    assert attempt_details["temperature"] == 0.2
    assert attempt_details["finish_reason"] is None
    assert attempt_details["response_chars"] > 0
    assert attempt_details["local_parse_success"] is True
    assert attempt_details["entered_repair"] is False
    assert attempt_details["prompt_chars"] > 0
    assert attempt_details["prompt_estimated_tokens"] > 0
    assert attempt_details["json_extractable"] is True
    assert attempt_details["core_json_extractable"] is True
    assert attempt_details["incomplete_response"] is False

    finished = [row for row in rows if row["event"] == "stage3_finished"][-1]
    finished_details = json.loads(finished["details_json"])
    assert finished_details["selected_strategy_id"] == "ER-001"
    assert finished_details["stage3_total_elapsed_ms"] == finished["elapsed_ms"]


def test_stage3_repairs_non_candidate_strategy_and_can_select_backup(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="frustration",
        candidates=["ER-002", "EE-003", "SS-006", "EA-007"],
    )
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _output("ER-999", "先随便试一下再说。"),
            _output(
                "EE-003",
                "现在卡住可以先停一下，大家各说一个能确认的小线索再继续。",
            ),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(pipeline_id, gateway=gateway)

    assert result["stage3_status"] == "SUCCEEDED"
    assert result["selected_strategy_id"] == "EE-003"
    assert len(gateway.calls) == 2
    assert [call["max_attempts_override"] for call in gateway.calls] == [1, 1]
    repair_payload = json.loads(gateway.calls[1]["payload"]["messages"][1]["content"])
    assert repair_payload["validation_error"] == "strategy_not_candidate"
    assert set(repair_payload) == {
        "allowed_strategy_ids",
        "validation_error",
        "previous_response",
        "output_contract",
    }
    assert repair_payload["allowed_strategy_ids"] == [
        "ER-002",
        "EE-003",
        "SS-006",
        "EA-007",
    ]
    assert repair_payload["output_contract"]["only_fields"] == [
        "selected_strategy_id",
        "intervention_text",
    ]
    assert "candidate_strategy_definitions" not in repair_payload
    assert "task_context" not in repair_payload
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["generated_intervention_text"] == "先随便试一下再说。"
    assert row["validated_intervention_text"].startswith("现在卡住")

def test_stage3_retries_truncated_gateway_result_with_strict_repair_contract(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _FakeLlmResult(
                "",
                success=False,
                failure_type="truncated_response",
                finish_reason="length",
            ),
            _output(
                "ER-001",
                "先把分歧放回共同标准，大家各说一条证据，再一起比较哪个方案更稳妥。",
            ),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "SUCCEEDED"
    assert len(gateway.calls) == 2
    assert [call["max_attempts_override"] for call in gateway.calls] == [1, 1]
    repair_prompt = gateway.calls[1]["payload"]["messages"][0]["content"]
    assert "selected_strategy_id" in repair_prompt
    assert "intervention_text" in repair_prompt
    repair_context = json.loads(
        gateway.calls[1]["payload"]["messages"][1]["content"]
    )
    assert repair_context["validation_error"] == "truncated_response"


def test_stage3_local_json_extraction_does_not_consume_repair_budget(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    text = "看起来分歧有点难对齐，大家可以一起先说一条证据。"
    wrapped = (
        "结果如下：\n```json\n"
        + json.dumps(_core_output("ER-001", text), ensure_ascii=False)
        + "\n```\n"
    )
    gateway = _FakeGateway([wrapped])

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "SUCCEEDED"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["max_attempts_override"] == 1


def test_stage3_repair_failure_stops_at_two_external_calls(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(["{\"selected_strategy_id\":", "still not json"])

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "FAILED"
    assert result["failure_code"] == "invalid_json"
    assert len(gateway.calls) == 2
    assert [call["max_attempts_override"] for call in gateway.calls] == [1, 1]
    repair_context = json.loads(gateway.calls[1]["payload"]["messages"][1]["content"])
    assert repair_context["validation_error"] == "invalid_json"
    assert set(repair_context) == {
        "allowed_strategy_ids",
        "validation_error",
        "previous_response",
        "output_contract",
    }


def test_stage3_accepts_duplicate_recent_agent_text_without_content_validation(batch5_env):
    db, scope = batch5_env
    duplicate_text = "分歧先回到证据上，大家各说一个最担心的条件，再一起看方案怎么调整。"
    _add_message(db, scope, 0, duplicate_text, role="agent")
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _output("ER-001", duplicate_text, supporting=["SS-004"]),
            _output(
                "SS-004",
                "先保护每个人的表达空间，大家各补一句自己依据的证据再继续比较。",
            ),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(pipeline_id, gateway=gateway)

    assert result["stage3_status"] == "SUCCEEDED"
    assert result["validated_intervention_text"] == duplicate_text
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["max_attempts_override"] == 1


def test_stage3_accepts_cold_text_without_content_validation(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _output("ER-001", "请把分歧拆开并各说一条证据。"),
            _output(
                "ER-001",
                "看起来分歧确实有点难对齐，大家先各说一条证据再一起比较。",
            ),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(pipeline_id, gateway=gateway)

    assert result["stage3_status"] == "SUCCEEDED"
    assert result["validated_intervention_text"] == "请把分歧拆开并各说一条证据。"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["max_attempts_override"] == 1
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["generated_intervention_text"] == "请把分歧拆开并各说一条证据。"
    assert row["validated_intervention_text"] == "请把分歧拆开并各说一条证据。"


def test_stage3_accepts_over_personified_or_judgment_leaking_text(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    context = stage3.build_stage3_context(pipeline_id)

    over_personified = stage3.parse_stage3_output(
        _output(
            "ER-001",
            "我一直陪着你们，大家先把分歧拆开各说一条证据。",
        ),
        context,
    )
    judgment_leakage = stage3.parse_stage3_output(
        _output(
            "ER-001",
            "系统判断你们卡在分歧里，大家先各说一条证据。",
        ),
        context,
    )

    assert over_personified["valid"] is True
    assert judgment_leakage["valid"] is True


def test_stage3_does_not_enter_for_oi_or_non_intervention_state(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="deep_thinking",
        candidates=["OI-002"],
        should_intervene=0,
        inhibition_strategy_id="OI-002",
        stage3_status="SKIPPED",
    )
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway([_output("OI-002", "请继续。")])

    result = stage3.Stage3PipelineService.execute_for_pipeline(pipeline_id, gateway=gateway)

    assert result["updated"] is False
    assert result["reason"] == "not_stage3_eligible"
    assert len(gateway.calls) == 0
    row = db.query_one("SELECT selected_strategy_id, generated_intervention_text FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["selected_strategy_id"] is None
    assert row["generated_intervention_text"] is None


def test_stage3_ignores_legacy_extra_fields_and_keeps_stage2_state(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope, locked=True)
    stage3 = importlib.import_module("services.three_stage_stage3")
    bad = _core_output(
        "ER-001",
        "分歧先回到证据上，大家各说一个最担心的条件，再一起看方案怎么调整。",
    )
    bad["canonical_sub_state"] = "standard"
    gateway = _FakeGateway([bad])

    result = stage3.Stage3PipelineService.execute_for_pipeline(pipeline_id, gateway=gateway)

    assert result["stage3_status"] == "SUCCEEDED"
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["canonical_sub_state_code"] == "interpersonal_conflict"
    assert row["stage3_status"] == "SUCCEEDED"
    assert row["selected_strategy_id"] == "ER-001"
    assert row["room_lock_released_at"] is None


def test_stage3_accepts_two_field_json_and_simple_wrappers(batch5_env):
    db, scope = batch5_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope)
    context = stage3.build_stage3_context(pipeline_id)
    text = "看起来分歧有点难对齐，大家可以一起先说一条证据。"

    cases = [
        _core_output("ER-001", text),
        "```json\n" + json.dumps(_core_output("ER-001", text), ensure_ascii=False) + "\n```",
        "这里是结果：\n"
        + json.dumps(_core_output("ER-001", text), ensure_ascii=False)
        + "\n以上。",
        {
            **_core_output("ER-001", text),
            "schema_version": "stage3.v1",
            "selection_reason": "ignored legacy field",
        },
    ]

    for output in cases:
        parsed = stage3.parse_stage3_output(output, context)
        assert parsed["valid"] is True
        assert parsed["output"] == {
            "selected_strategy_id": "ER-001",
            "intervention_text": text,
        }

    invalid_cases = [
        ({**_core_output("ER-999", text)}, "strategy_not_candidate"),
        ({**_core_output("ER-001", "")}, "missing_intervention_text"),
        ({"intervention_text": text}, "missing_selected_strategy_id"),
        ({"selected_strategy_id": "ER-001"}, "missing_intervention_text"),
    ]
    for output, reason in invalid_cases:
        parsed = stage3.parse_stage3_output(output, context)
        assert parsed["valid"] is False
        assert parsed["reason"] == reason


def test_stage3_payload_disables_provider_thinking_and_keeps_two_call_budget(batch5_env):
    db, scope = batch5_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope)
    context = stage3.build_stage3_context(pipeline_id)

    initial = stage3.build_stage3_payload(context)
    repair = stage3.build_stage3_payload(
        context,
        repair={"previous_output": "{", "validation_error": "invalid_json"},
    )

    assert initial["thinking"] == {"type": "disabled"}
    assert repair["thinking"] == {"type": "disabled"}
    assert initial["max_tokens"] == 600
    assert repair["max_tokens"] == 400


def test_complete_json_is_not_rejected_only_for_length_finish_reason(batch5_env):
    db, scope = batch5_env
    stage3 = importlib.import_module("services.three_stage_stage3")
    pipeline_id = _insert_stage2_pipeline(db, scope)
    context = stage3.build_stage3_context(pipeline_id)

    parsed = stage3.parse_stage3_output(
        _core_output("ER-001", "大家先把分歧放回证据上再一起比较。"),
        context,
        finish_reason="length",
    )

    assert parsed["valid"] is True


def test_stage3_accepts_content_that_publish_may_still_reject(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    context = stage3.build_stage3_context(pipeline_id)
    accepted_texts = [
        "请把分歧拆开并各说一条证据",
        "大家先说一条证据。然后再比较哪个条件更关键。",
        "先看证据，并把分歧放在一起比较。",
        "刚才卡住确实不容易，先选一个能验证的小步骤。",
        "大家先把分歧放回证据上，" * 8,
        "这句没有照搬参考模板，大家先说一条自己的证据",
    ]

    for text in accepted_texts:
        parsed = stage3.parse_stage3_output(
            _core_output("ER-001", text),
            context,
        )
        assert parsed["valid"] is True
        assert parsed["output"]["intervention_text"] == text
        assert parsed["text_validation"] == {
            "passed": True,
            "failure_code": None,
            "validation_scope": "structure_only",
        }


def test_stage3_structural_validation_rejects_only_invalid_core_fields(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(db, scope)
    stage3 = importlib.import_module("services.three_stage_stage3")
    context = stage3.build_stage3_context(pipeline_id)

    invalid_cases = [
        ({"selected_strategy_id": "ER-999", "intervention_text": "任意非空话术"}, "strategy_not_candidate"),
        ({"selected_strategy_id": "ER-001", "intervention_text": ""}, "missing_intervention_text"),
        ({"selected_strategy_id": "ER-001", "intervention_text": "   "}, "missing_intervention_text"),
        ({"selected_strategy_id": "ER-001", "intervention_text": 123}, "missing_intervention_text"),
        ({"intervention_text": "任意非空话术"}, "missing_selected_strategy_id"),
        ({"selected_strategy_id": "ER-001"}, "missing_intervention_text"),
    ]

    for output, reason in invalid_cases:
        parsed = stage3.parse_stage3_output(output, context)
        assert parsed["valid"] is False
        assert parsed["reason"] == reason


def test_er002_content_requirements_are_prompt_only(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="frustration",
        candidates=["ER-002", "EE-003", "SS-006", "EA-007"],
    )
    stage3 = importlib.import_module("services.three_stage_stage3")
    context = stage3.build_stage3_context(pipeline_id)

    invalid = stage3.parse_stage3_output(
        _output("ER-002", "请继续推进任务并整理方案。"),
        context,
    )
    valid = stage3.parse_stage3_output(
        _output("ER-002", "大家卡住很正常，先把最难的一点说出来，再选一个能验证的小步骤。"),
        context,
    )
    valid_reframe_without_emotion_keyword = stage3.parse_stage3_output(
        _output(
            "ER-002",
            "大家反复尝试发现约束本身就是方案优化的关键，先按必要性排序再调整一个冲突点。",
        ),
        context,
    )

    assert invalid["valid"] is True
    assert valid["valid"] is True
    assert valid_reframe_without_emotion_keyword["valid"] is True


def test_stage3_preserves_text_without_local_content_normalization(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="burnout",
        candidates=["ER-008"],
    )
    stage3 = importlib.import_module("services.three_stage_stage3")
    original_text = (
        "大家觉得没劲了。先别想学校用不用，就问一个具体的："
        "这个空间最想用来做什么？就从那个画面开始聊。"
    )
    gateway = _FakeGateway([_output("ER-008", original_text)])

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "SUCCEEDED"
    row = db.query_one(
        "SELECT generated_intervention_text, validated_intervention_text, "
        "text_validation_result_json FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["generated_intervention_text"] == original_text
    assert row["validated_intervention_text"] == original_text
    validation = json.loads(row["text_validation_result_json"])
    assert validation["passed"] is True
    assert validation["validation_scope"] == "structure_only"
    assert "normalization" not in validation


def test_stage3_accepts_semicolon_without_repair(batch5_env):
    db, scope = batch5_env
    pipeline_id = _insert_stage2_pipeline(
        db,
        scope,
        canonical="frustration",
        candidates=["ER-002", "EE-003", "SS-006", "EA-007"],
    )
    stage3 = importlib.import_module("services.three_stage_stage3")
    gateway = _FakeGateway(
        [
            _output(
                "ER-002",
                "刚才反复失败是在帮你们发现关键约束；现在先聚焦一个安全约束再核对预算。",
            ),
            _output(
                "ER-002",
                "大家反复失败是在发现关键约束，现在先只检查一个安全约束对预算的影响。",
            ),
        ]
    )

    result = stage3.Stage3PipelineService.execute_for_pipeline(
        pipeline_id,
        gateway=gateway,
    )

    assert result["stage3_status"] == "SUCCEEDED"
    assert "；" in result["validated_intervention_text"]
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["max_attempts_override"] == 1
