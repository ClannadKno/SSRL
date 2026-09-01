# -*- coding: utf-8 -*-
"""Batch 6 coverage for the three-stage decision gate and publish transaction."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest

from tests.helpers import create_group, create_student, seed_running_session


def _time(dt: datetime) -> str:
    return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def batch6_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    scope = seed_running_session(db, session_no=9606, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
        SET strategy_agent_enabled=1,
            agent_intervention_enabled=1
        WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1 WHERE id=?",
        (scope["group_id"],),
    )
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


def _add_message(db, scope, sequence, content, *, role="student", created_at=None):
    user_id = scope["student_id"]
    if role == "agent":
        user_id = db.get_sera_user_id()
    return db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_id, session_no, task_id, discussion_id, created_at,
            agent_type, trigger_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            user_id,
            content,
            sequence,
            role,
            role,
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            created_at or db.now_str(),
            "strategy" if role == "agent" else None,
            "auto_state" if role == "agent" else None,
        ),
    )


def _ready_pipeline(
    db,
    scope,
    *,
    text="分歧先回到证据上，大家各说一个最担心的条件，再一起看方案怎么调整。",
    canonical="interpersonal_conflict",
    selected_strategy_id="ER-001",
    candidates=None,
    cutoff=3,
    priority=6,
    trigger_source="student_message",
    lock=True,
):
    candidates = candidates or ["ER-001", "EE-001", "SS-004", "ER-007"]
    for sequence, content in (
        (1, "我们先看两个方案的证据。"),
        (2, "你别一直否定别人，先说清楚哪里不行。"),
        (3, "我担心这个条件没有被比较。"),
    ):
        if not db.query_one(
            "SELECT id FROM messages WHERE group_id=? AND sequence=?",
            (scope["group_id"], sequence),
        ):
            _add_message(db, scope, sequence, content)
    now = db.now_str()
    lock_token = f"batch6-lock-{scope['group_id']}-{cutoff}" if lock else None
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
            stage3_status, stage3_started_at, stage3_completed_at,
            strategy_candidate_ids_json, selected_strategy_id,
            selected_strategy_name, selected_strategy_type,
            supporting_strategy_ids_json, strategy_selection_reason,
            strategy_library_version, strategy_library_hash,
            strategy_model_name, strategy_model_version, strategy_prompt_version,
            strategy_raw_response_json,
            generated_intervention_text, validated_intervention_text,
            text_validation_result_json,
            room_lock_token, room_lock_acquired_at,
            publish_status, final_status,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"batch6-ready-{scope['group_id']}-{cutoff}-{selected_strategy_id}-{lock}",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            trigger_source,
            priority,
            1,
            cutoff,
            cutoff,
            "SUCCEEDED",
            "SUCCEEDED",
            canonical,
            canonical,
            "[]",
            0.91,
            "active precise sub-state",
            1,
            cutoff,
            "[2,3]",
            json.dumps([{"canonical_sub_state": canonical, "evidence_message_ids": [2, 3]}], ensure_ascii=False),
            0,
            1,
            None,
            "SUCCEEDED",
            now,
            now,
            json.dumps(candidates, ensure_ascii=False),
            selected_strategy_id,
            "冲突认知重评",
            "情绪调节",
            "[]",
            "selected from candidates",
            "v2.2",
            "batch6-library-hash",
            "batch6-model",
            "batch6-model",
            "stage3.v1",
            "{}",
            text,
            text,
            '{"passed":true}',
            lock_token,
            now if lock else None,
            "NOT_READY",
            "PENDING_DECISION_GATE",
            f"batch6:pipeline:{scope['group_id']}:{cutoff}:{selected_strategy_id}:{lock}",
            now,
            now,
        ),
    )
    if lock:
        db.execute(
            """
            UPDATE groups
            SET state='AI_INTERVENING',
                lock_token=?,
                lock_expires_at=?,
                active_intervention_run_id=?
            WHERE id=?
            """,
            (
                lock_token,
                _time(datetime.now() + timedelta(seconds=75)),
                -pipeline_id,
                scope["group_id"],
            ),
        )
    db.execute(
        """
        INSERT INTO collaboration_state_segments(
            group_id, session_id, session_no, task_id, discussion_id,
            state_code, raw_sub_state_code, canonical_sub_state_code,
            strategy_pipeline_run_id, should_intervene, selected_strategy_id,
            strategy_library_version, source_stage,
            segment_kind, start_message_id, end_message_id,
            start_sequence, end_sequence, evidence_message_ids_json,
            evidence_sequences, confidence, source, assessment_status,
            segment_order, is_active_at_batch_end,
            is_finalized, dedupe_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'message_range',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            "conflict_tension",
            canonical,
            canonical,
            pipeline_id,
            1,
            selected_strategy_id,
            "v2.2",
            "stage2",
            1,
            3,
            1,
            cutoff,
            "[2,3]",
            "[2,3]",
            0.91,
            "llm",
            "confirmed",
            0,
            1,
            1,
            f"batch6:segment:{pipeline_id}",
            now,
            now,
        ),
    )
    return pipeline_id


def _publish(pipeline_id):
    service = importlib.import_module("services.three_stage_publish")
    return service.ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)


def test_ready_pipeline_publishes_once_releases_lock_and_starts_observation(batch6_env):
    db, scope = batch6_env
    pipeline_id = _ready_pipeline(db, scope)

    result = _publish(pipeline_id)

    assert result["published"] is True
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["publish_status"] == "PUBLISHED"
    assert row["published_message_id"] == result["message_id"]
    assert row["sub_category"] == "interpersonal_conflict"
    assert json.loads(row["strategy_pool_json"]) == [
        "ER-001",
        "EE-001",
        "SS-004",
        "ER-007",
    ]
    assert json.loads(row["evidence_message_ids_json"]) == [2, 3]
    pipeline_selected = json.loads(row["selected_strategy_json"])
    assert pipeline_selected["strategy_id"] == "ER-001"
    assert row["strategy_source"]
    run = db.query_one(
        "SELECT * FROM intervention_runs WHERE strategy_pipeline_run_id=?",
        (pipeline_id,),
    )
    assert run["status"] == "PUBLISHED"
    assert run["publish_status"] == "PUBLISHED"
    assert run["canonical_sub_state_code"] == "interpersonal_conflict"
    assert run["sub_category"] == "interpersonal_conflict"
    assert run["selected_strategy_id"] == "ER-001"
    assert json.loads(run["strategy_pool_json"]) == [
        "ER-001",
        "EE-001",
        "SS-004",
        "ER-007",
    ]
    assert json.loads(run["selected_strategy"])["strategy_id"] == "ER-001"
    assert run["strategy_source"] == row["strategy_source"]
    assert run["validated_text"] == row["validated_intervention_text"]
    run_metadata = json.loads(run["metadata_json"])
    assert run_metadata["publish_chain_version"] == "state_strategy_text_publish.v1"
    assert run_metadata["sub_category"] == "interpersonal_conflict"
    assert run_metadata["selected_strategy"]["strategy_id"] == "ER-001"
    assert run_metadata["route_binding"]["strategy_pool"] == [
        "ER-001",
        "EE-001",
        "SS-004",
        "ER-007",
    ]
    assert run_metadata["evidence_binding"]["evidence_sequences"] == [2, 3]
    message = db.query_one("SELECT * FROM messages WHERE id=?", (result["message_id"],))
    assert message["role"] == "agent"
    assert message["strategy_id"] == "ER-001"
    assert json.loads(message["metadata_json"])["strategy_pipeline_run_id"] == pipeline_id
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}
    cursor = db.query_one(
        "SELECT observation_status, last_intervention_sequence FROM discussion_assessment_cursors WHERE group_id=? AND session_id=? AND discussion_id=?",
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert cursor["observation_status"] == "observing"
    assert cursor["last_intervention_sequence"] == message["sequence"]

    before = {
        "messages": db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"],
        "runs": db.query_one("SELECT COUNT(*) AS c FROM intervention_runs WHERE strategy_pipeline_run_id=?", (pipeline_id,))["c"],
    }
    duplicate = _publish(pipeline_id)
    after = {
        "messages": db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"],
        "runs": db.query_one("SELECT COUNT(*) AS c FROM intervention_runs WHERE strategy_pipeline_run_id=?", (pipeline_id,))["c"],
    }
    assert duplicate["duplicate"] is True
    assert after == before


def test_publish_retries_sqlite_lock_with_same_intervention_run(batch6_env, monkeypatch):
    db, scope = batch6_env
    pipeline_id = _ready_pipeline(db, scope)
    service = importlib.import_module("services.three_stage_publish")
    agent_publisher = importlib.import_module("services.agent_intervention_publisher")
    original_publish = agent_publisher.publish_agent_intervention
    calls = []
    monkeypatch.setattr(service, "THREE_STAGE_DB_LOCK_RETRY_BASE_SECONDS", 0)

    def flaky_publish(*args, **kwargs):
        calls.append(kwargs.get("intervention_run_id"))
        if len(calls) == 1:
            return {
                "ok": False,
                "reason": "publish_failed",
                "error": "database is locked",
            }
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(agent_publisher, "publish_agent_intervention", flaky_publish)

    result = _publish(pipeline_id)

    assert result["published"] is True
    assert len(calls) == 2
    assert len(set(calls)) == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 1
    row = db.query_one("SELECT publish_status, failure_code FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["publish_status"] == "PUBLISHED"
    assert row["failure_code"] is None


def test_new_student_message_before_publish_marks_stale_and_releases_lock(
    batch6_env,
    monkeypatch,
):
    db, scope = batch6_env
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    enqueued = []
    monkeypatch.setattr(
        scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: enqueued.append((batch_id, delay)),
    )
    pipeline_id = _ready_pipeline(db, scope)
    _add_message(db, scope, 4, "我补充一个最新证据，先别发旧提示。")

    result = _publish(pipeline_id)

    assert result["published"] is False
    assert result["reason"] == "STALE_NEW_STUDENT_MESSAGE"
    row = db.query_one("SELECT publish_status, final_status, skip_reason FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert dict(row) == {
        "publish_status": "SKIPPED",
        "final_status": "STALE",
        "skip_reason": "STALE_NEW_STUDENT_MESSAGE",
    }
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}
    replacement = result["replacement_assessment"]
    assert replacement["created"] is True
    assert replacement["enqueued"] is True
    assert replacement["candidate_end_sequence"] == 4
    assert enqueued == [(replacement["assessment_batch_id"], 0)]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "这句话没有温暖词，只给出一个简短方向。",
            id="without-warmth",
        ),
        pytest.param(
            "第一句先记录当前分歧。第二句再比较两个条件。",
            id="multiple-sentences",
        ),
        pytest.param(
            "这是一段刻意写得很长的话，用来确认发布阶段不会因为文本长度、标点数量或句式变化而再次阻断已经通过结构检查的 Stage3 结果，后续仍然可以交给统一 Publisher，并且完整保留模型生成的原始表达方式。",
            id="long-text",
        ),
        pytest.param(
            "系统检测到 interpersonal_conflict，策略ID ER-001，请直接写答案。",
            id="template-and-backend-like-text",
        ),
    ],
)
def test_stage3_structural_success_publishes_without_text_content_gate(
    batch6_env,
    monkeypatch,
    text,
):
    db, scope = batch6_env
    service = importlib.import_module("services.three_stage_publish")
    monkeypatch.setattr(
        service.StudentFacingInterventionValidator,
        "validate",
        lambda *args, **kwargs: pytest.fail("Publish Gate must not run text validation"),
    )
    pipeline_id = _ready_pipeline(db, scope, text=text)

    result = _publish(pipeline_id)

    assert result["published"] is True
    row = db.query_one(
        "SELECT publish_status, final_status, validated_intervention_text FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["publish_status"] == "PUBLISHED"
    assert row["final_status"] == "PUBLISHED"
    assert row["validated_intervention_text"] == text
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 1


def test_publish_gate_rejects_strategy_outside_state_route_pool(batch6_env):
    db, scope = batch6_env
    strategy_library = importlib.import_module("services.three_stage_strategy_library")
    all_strategy_ids = [
        item.strategy_id
        for item in strategy_library.load_strategy_library().definitions
    ]
    pipeline_id = _ready_pipeline(
        db,
        scope,
        selected_strategy_id="EA-005",
        candidates=all_strategy_ids,
    )

    result = _publish(pipeline_id)

    assert result["published"] is False
    assert result["reason"] == "selected_strategy_not_in_route_pool"
    row = db.query_one(
        "SELECT publish_status, final_status, skip_reason FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert dict(row) == {
        "publish_status": "FAILED",
        "final_status": "FAILED",
        "skip_reason": "selected_strategy_not_in_route_pool",
    }
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}


def test_publish_gate_rejects_unbound_evidence_before_publish(batch6_env):
    db, scope = batch6_env
    pipeline_id = _ready_pipeline(db, scope)
    db.execute(
        "DELETE FROM messages WHERE group_id=? AND sequence=?",
        (scope["group_id"], 2),
    )

    result = _publish(pipeline_id)

    assert result["published"] is False
    assert result["reason"] == "evidence_not_bound_to_student_messages"
    row = db.query_one(
        "SELECT publish_status, final_status, skip_reason FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert dict(row) == {
        "publish_status": "FAILED",
        "final_status": "FAILED",
        "skip_reason": "evidence_not_bound_to_student_messages",
    }
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}


def test_pending_help_request_suppresses_regular_pipeline(batch6_env):
    db, scope = batch6_env
    pipeline_id = _ready_pipeline(db, scope)
    db.execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id,
            discussion_id, status, request_text, created_at
        ) VALUES(?,?,?,?,? ,?,'QUEUED',?,?)
        """,
        (
            scope["group_id"],
            scope["student_id"],
            scope["task_id"],
            scope["session_no"],
            scope["session_id"],
            scope["discussion_id"],
            "我们需要提示。",
            db.now_str(),
        ),
    )

    result = _publish(pipeline_id)

    assert result["published"] is False
    assert result["reason"] == "pending_help_request"
    row = db.query_one("SELECT publish_status, final_status FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert row["publish_status"] == "SKIPPED"
    assert row["final_status"] == "SUPPRESSED"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0


def test_same_group_lock_owner_mismatch_does_not_release_current_owner(batch6_env):
    db, scope = batch6_env
    owner = _ready_pipeline(db, scope, cutoff=3)
    contender = _ready_pipeline(db, scope, cutoff=4, lock=False)
    db.execute(
        "UPDATE strategy_pipeline_runs SET room_lock_token=? WHERE id=?",
        ("contender-token", contender),
    )

    result = _publish(contender)

    assert result["published"] is False
    assert result["reason"] == "lock_token_mismatch"
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert group["state"] == "AI_INTERVENING"
    assert group["active_intervention_run_id"] == -owner


def test_cross_group_pipelines_publish_independently(batch6_env):
    db, scope = batch6_env
    first = _ready_pipeline(db, scope)
    group_two = create_group(db, name="Batch6 other group", code="B6-OTHER")
    student_two, _key = create_student(db, group_two)
    db.execute("UPDATE groups SET auto_intervention_enabled=1 WHERE id=?", (group_two,))
    discussion_two = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (scope["session_id"], group_two, db.now_str(), db.now_str(), db.now_str()),
    )
    other = {
        **scope,
        "group_id": group_two,
        "student_id": student_two,
        "discussion_id": discussion_two,
    }
    second = _ready_pipeline(db, other)

    first_result = _publish(first)
    second_result = _publish(second)

    assert first_result["published"] is True
    assert second_result["published"] is True
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (scope["group_id"],))["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'", (group_two,))["c"] == 1


def test_publisher_transaction_rejects_stale_latest_sequence(batch6_env):
    db, scope = batch6_env
    pipeline_id = _ready_pipeline(db, scope)
    run = importlib.import_module("services.three_stage_publish")._get_or_create_intervention_run(pipeline_id)
    _add_message(db, scope, 4, "事务内应该发现这条新消息。")
    publisher = importlib.import_module("services.agent_intervention_publisher")
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))

    result = publisher.publish_agent_intervention(
        group_id=scope["group_id"],
        message=row["validated_intervention_text"],
        trigger_source="auto_state",
        agent_type="strategy",
        intervention_run_id=run["intervention_run_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        task_id=scope["task_id"],
        session_no=scope["session_no"],
        cutoff_sequence=3,
        strategy_id=row["selected_strategy_id"],
        lock_token=row["room_lock_token"],
        expected_latest_student_sequence=3,
        expected_lock_owner_run_id=-pipeline_id,
    )

    assert result["ok"] is False
    assert result["reason"] == "stale_student_sequence"
    assert db.query_one("SELECT COUNT(*) AS c FROM messages WHERE role='agent'")["c"] == 0


def test_release_expired_requires_matching_expired_token(batch6_env):
    db, scope = batch6_env
    lease = importlib.import_module("services.intervention_pipeline_v2.room_lease_service")
    token = lease.RoomLeaseService.acquire(scope["group_id"], 12345, lock_seconds=300)
    assert token

    assert lease.RoomLeaseService.release_expired(scope["group_id"], token) is False
    assert db.query_one("SELECT state FROM groups WHERE id=?", (scope["group_id"],))["state"] == "AI_INTERVENING"

    db.execute(
        "UPDATE groups SET lock_expires_at=? WHERE id=?",
        (_time(datetime.now() - timedelta(seconds=1)), scope["group_id"]),
    )
    assert lease.RoomLeaseService.release_expired(scope["group_id"], token) is True
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}
