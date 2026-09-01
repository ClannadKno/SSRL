# -*- coding: utf-8 -*-
"""Batch 13 regression coverage for immutable explicit-trigger windows."""

from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def batch13_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    batch_service = importlib.import_module("services.state_assessment_batch_service")
    queued = []

    monkeypatch.setattr(
        scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: queued.append((int(batch_id), int(delay or 0))),
    )
    monkeypatch.setattr(scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 1)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 8)
    monkeypatch.setattr(scheduler, "STATE_LLM_CONTEXT_MESSAGES", 3)

    scope = seed_running_session(db, session_no=1313, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy',
               strategy_agent_enabled=1,
               emotion_agent_enabled=0,
               agent_intervention_enabled=1,
               research_state_monitoring_enabled=0
         WHERE id=?
        """,
        (scope["session_id"],),
    )
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    scope.update(
        {
            "discussion_id": discussion_id,
            "student_id": scope["students"][0][0],
        }
    )
    return db, scheduler, batch_service.StateAssessmentBatchService, scope, queued


def _add_messages(db, scope, messages):
    ids = {}
    for sequence, content in messages:
        ids[sequence] = db.execute(
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
                "student",
                "student",
                scope["session_id"],
                scope["session_no"],
                scope["task_id"],
                scope["discussion_id"],
                db.now_str(),
            ),
        )
    return ids


def _request(scheduler, scope, trigger, sequence=None, **kwargs):
    return scheduler.request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type=trigger,
        trigger_sequence=sequence,
        **kwargs,
    )


def _stage2_detection(canonical, start, end):
    should_intervene = canonical == "individual_marginalization"
    segment = {
        "raw_sub_state": canonical,
        "canonical_sub_state": canonical,
        "secondary_tags": [],
        "start_sequence": start,
        "end_sequence": end,
        "confidence": 0.93,
        "evidence_message_ids": [end],
        "reason": f"{canonical} remains active at sequence {end}",
        "is_active_at_window_end": True,
        "detected_self_regulation": False,
    }
    return {
        "monitor_run_id": end + 1300,
        "state_llm_result": {
            "schema_version": "stage2.v1",
            "analysis_scope": {
                "candidate_start_sequence": start,
                "candidate_end_sequence": end,
                "input_cutoff_student_sequence": end,
            },
            "segments": [segment],
            "active_segment_index": 0,
            "active_sub_state": dict(segment),
            "should_intervene": should_intervene,
            "inhibition": {
                "is_inhibited": not should_intervene,
                "strategy_id": "OI-004" if canonical == "execution_progress" else None,
                "reason": "recovery is proceeding" if not should_intervene else None,
            },
            "candidate_strategy_ids": ["EA-003"] if should_intervene else [],
            "decision_reason": f"deterministic batch13 {canonical}",
        },
        "state_llm_meta": {
            "success": True,
            "analysis_failed": False,
            "analysis_skipped": False,
            "model_name": "batch13-deterministic",
            "prompt_version": "batch13-trigger-window",
        },
    }


def _patch_worker(monkeypatch, scheduler, state_by_end):
    monitoring = importlib.import_module(
        "services.discussion_pipeline_v2.monitoring_service"
    )
    stage2 = importlib.import_module("services.three_stage_stage2")
    calls = []

    def run_detection(**kwargs):
        start = int(kwargs["fixed_candidate_start_sequence"])
        end = int(kwargs["fixed_candidate_end_sequence"])
        calls.append((start, end))
        return _stage2_detection(state_by_end[end], start, end)

    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(run_detection),
    )
    monkeypatch.setattr(
        stage2.Stage2PipelineService,
        "prepare_for_batch",
        staticmethod(
            lambda batch, pipeline_mode=None: {
                "prepared": True,
                "pipeline_run_id": int(batch["id"]) + 13000,
                "coarse_should_escalate": False,
                "room_lock_token": None,
            }
        ),
    )
    monkeypatch.setattr(
        stage2.Stage2PipelineService,
        "mark_started",
        staticmethod(
            lambda **kwargs: {
                "started": True,
                "pipeline_run_id": int(kwargs["pipeline_run_id"]),
            }
        ),
    )
    monkeypatch.setattr(
        stage2.Stage2PipelineService,
        "persist_success",
        staticmethod(
            lambda **kwargs: {
                "updated": True,
                "pipeline_run_id": int(kwargs["batch"]["id"]) + 13000,
                "should_enter_stage3": False,
                "skip_reason": "batch13_test_stage3_not_required",
                "stale": False,
            }
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "record_observation_assessment",
        lambda **_kwargs: {"recorded": False, "reason": "batch13_test"},
    )
    return calls


def _execute_all_pending(db, scheduler):
    outcomes = []
    for _attempt in range(10):
        row = db.query_one(
            """
            SELECT id FROM state_assessment_batches
            WHERE status='pending'
            ORDER BY id
            LIMIT 1
            """
        )
        if not row:
            return outcomes
        outcomes.append(scheduler.execute_state_assessment_batch(row["id"]))
    raise AssertionError("state assessment queue did not drain within 10 executions")


def test_g4d_trigger_window_excludes_later_recovery_and_advances_latest_state(
    batch13_env, monkeypatch
):
    db, scheduler, service, scope, _queued = batch13_env
    _add_messages(
        db,
        scope,
        [
            (1, "我们又跳过了小林的意见。"),
            (2, "先别管小林，继续按我们的版本。"),
            (3, "这已经是第三次没有让小林参与决定。"),
            (4, "你说得对，现在把小林的方案加入并重新分工。"),
        ],
    )
    calls = _patch_worker(
        monkeypatch,
        scheduler,
        {3: "individual_marginalization", 4: "execution_progress"},
    )

    trigger = _request(scheduler, scope, "rule_high_risk", 3)

    assert (
        trigger["candidate_start_sequence"],
        trigger["candidate_end_sequence"],
    ) == (1, 3)
    assert json.loads(trigger["batch"]["student_sequences_json"]) == [1, 2, 3]
    assert trigger["batch"]["rerun_requested"] == 1

    outcomes = _execute_all_pending(db, scheduler)
    batches = db.query_all(
        """
        SELECT candidate_start_sequence, candidate_end_sequence, trigger_type,
               trigger_sequence
        FROM state_assessment_batches
        ORDER BY id
        """
    )
    states = db.query_all(
        """
        SELECT canonical_sub_state_code, assessment_batch_id
        FROM collaboration_state_segments
        ORDER BY assessment_batch_id
        """
    )

    assert len(outcomes) == 2
    assert [tuple(call) for call in calls] == [(1, 3), (4, 4)]
    assert [
        (
            row["candidate_start_sequence"],
            row["candidate_end_sequence"],
            row["trigger_type"],
            row["trigger_sequence"],
        )
        for row in batches
    ] == [
        (1, 3, "rule_high_risk", 3),
        (4, 4, "message_count_periodic", None),
    ]
    assert [row["canonical_sub_state_code"] for row in states] == [
        "individual_marginalization",
        "execution_progress",
    ]
    assert (
        service.get_last_finalized_student_sequence(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            discussion_id=scope["discussion_id"],
        )
        == 4
    )

    duplicate = _request(scheduler, scope, "rule_high_risk", 3)
    assert duplicate["reason"] == "no_new_student_messages"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM state_assessment_batches"
    )["c"] == 2


def test_active_batch_preserves_earliest_explicit_cutoff_across_concurrent_reruns(
    batch13_env, monkeypatch
):
    db, scheduler, service, scope, queued = batch13_env
    _add_messages(db, scope, [(1, "准备材料。"), (2, "整理数据。")])
    first = _request(scheduler, scope, "message_count_periodic", 2)
    _add_messages(
        db,
        scope,
        [
            (3, "小林还是没有获得发言机会。"),
            (4, "现在补上小林的意见并调整分工。"),
        ],
    )

    def request_risk(_index):
        return _request(scheduler, scope, "rule_high_risk", 3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        risk_results = list(pool.map(request_risk, range(2)))
    recovery = _request(scheduler, scope, "message_count_periodic", 4)
    active = db.query_one(
        "SELECT * FROM state_assessment_batches WHERE id=?",
        (first["assessment_batch_id"],),
    )

    assert all(
        item["assessment_batch_id"] == first["assessment_batch_id"]
        for item in risk_results
    )
    assert recovery["assessment_batch_id"] == first["assessment_batch_id"]
    assert (active["candidate_start_sequence"], active["candidate_end_sequence"]) == (
        1,
        2,
    )
    assert active["continuation_trigger_type"] == "rule_high_risk"
    assert active["continuation_trigger_sequence"] == 3

    calls = _patch_worker(
        monkeypatch,
        scheduler,
        {
            2: "standard",
            3: "individual_marginalization",
            4: "execution_progress",
        },
    )
    outcomes = _execute_all_pending(db, scheduler)
    batches = db.query_all(
        """
        SELECT candidate_start_sequence, candidate_end_sequence, trigger_type
        FROM state_assessment_batches
        ORDER BY id
        """
    )

    assert len(outcomes) == 3
    assert calls == [(1, 2), (3, 3), (4, 4)]
    assert [
        (
            row["candidate_start_sequence"],
            row["candidate_end_sequence"],
            row["trigger_type"],
        )
        for row in batches
    ] == [
        (1, 2, "message_count_periodic"),
        (3, 3, "rule_high_risk"),
        (4, 4, "message_count_periodic"),
    ]
    assert len(
        {
            (row["candidate_start_sequence"], row["candidate_end_sequence"])
            for row in batches
        }
    ) == 3
    assert len(queued) == 3
    assert (
        service.get_last_finalized_student_sequence(
            group_id=scope["group_id"],
            session_id=scope["session_id"],
            discussion_id=scope["discussion_id"],
        )
        == 4
    )


def test_delayed_trigger_inside_active_periodic_window_gets_fixed_replay(
    batch13_env, monkeypatch
):
    db, scheduler, _service, scope, _queued = batch13_env
    _add_messages(
        db,
        scope,
        [
            (1, "继续讨论。"),
            (2, "仍然忽略小林。"),
            (3, "第三次跳过小林的贡献。"),
            (4, "已经承认问题并开始修正。"),
        ],
    )
    first = _request(scheduler, scope, "message_count_periodic", 4)
    delayed = _request(scheduler, scope, "rule_high_risk", 3)
    active = delayed["batch"]

    assert (
        active["continuation_candidate_start_sequence"],
        active["continuation_candidate_end_sequence"],
    ) == (1, 3)
    calls = _patch_worker(
        monkeypatch,
        scheduler,
        {4: "execution_progress", 3: "individual_marginalization"},
    )

    outcomes = _execute_all_pending(db, scheduler)
    windows = db.query_all(
        """
        SELECT candidate_start_sequence, candidate_end_sequence, trigger_type
        FROM state_assessment_batches
        ORDER BY id
        """
    )

    assert first["candidate_end_sequence"] == 4
    assert len(outcomes) == 2
    assert calls == [(1, 4), (1, 3)]
    assert [
        (
            row["candidate_start_sequence"],
            row["candidate_end_sequence"],
            row["trigger_type"],
        )
        for row in windows
    ] == [
        (1, 4, "message_count_periodic"),
        (1, 3, "rule_high_risk"),
    ]


def test_replacement_metadata_survives_active_batch_and_keeps_recovery_separate(
    batch13_env, monkeypatch
):
    db, scheduler, _service, scope, _queued = batch13_env
    message_ids = _add_messages(db, scope, [(1, "旧窗口。"), (2, "旧窗口末尾。")])
    first = _request(scheduler, scope, "message_count_periodic", 2)
    message_ids.update(
        _add_messages(
            db,
            scope,
            [(3, "替换窗口触发。"), (4, "替换后的恢复消息。")],
        )
    )

    replacement = _request(
        scheduler,
        scope,
        "message_count_periodic",
        3,
        continuation=True,
        replacement_of_pipeline_run_id=9913,
        replacement_reason="STALE_NEW_STUDENT_MESSAGE",
        replacement_trigger_message_id=message_ids[3],
        replacement_cutoff_sequence=3,
    )
    _request(scheduler, scope, "message_count_periodic", 4)
    active = replacement["batch"]

    assert active["continuation_replacement_of_pipeline_run_id"] == 9913
    assert active["continuation_replacement_trigger_message_id"] == message_ids[3]
    assert active["continuation_replacement_cutoff_sequence"] == 3

    _patch_worker(
        monkeypatch,
        scheduler,
        {2: "standard", 3: "individual_marginalization", 4: "execution_progress"},
    )
    _execute_all_pending(db, scheduler)
    batches = db.query_all(
        """
        SELECT candidate_start_sequence, candidate_end_sequence,
               replacement_of_pipeline_run_id, replacement_reason,
               replacement_trigger_message_id, replacement_cutoff_sequence
        FROM state_assessment_batches
        ORDER BY id
        """
    )

    assert first["candidate_end_sequence"] == 2
    assert [
        (row["candidate_start_sequence"], row["candidate_end_sequence"])
        for row in batches
    ] == [(1, 2), (3, 3), (4, 4)]
    assert batches[0]["replacement_of_pipeline_run_id"] is None
    assert batches[1]["replacement_of_pipeline_run_id"] == 9913
    assert batches[1]["replacement_reason"] == "STALE_NEW_STUDENT_MESSAGE"
    assert batches[1]["replacement_trigger_message_id"] == message_ids[3]
    assert batches[1]["replacement_cutoff_sequence"] == 3
    assert batches[2]["replacement_of_pipeline_run_id"] is None
