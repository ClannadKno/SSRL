# -*- coding: utf-8 -*-
"""Strategy regression using the unchanged four-person discussion script."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from tests.helpers import seed_running_session


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "load-test" / "config" / "trigger-states-4p.js"


def _load_four_person_messages():
    command = (
        "const scenario=require('./load-test/config/trigger-states-4p');"
        "process.stdout.write(JSON.stringify(scenario.scriptedDiscussion.messages));"
    )
    result = subprocess.run(
        ["node", "-e", command],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)
    assert len(messages) == 29
    assert {row["studentId"].rsplit("M", 1)[-1] for row in messages} == {
        "1",
        "2",
        "3",
        "4",
    }
    return messages


def _enable_strategy_agent(db, scope):
    db.execute(
        """
        UPDATE experiment_sessions
           SET agent_mode='strategy',
               strategy_agent_enabled=1,
               emotion_agent_enabled=0,
               agent_intervention_enabled=1
         WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (scope["task_id"],),
    )
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1, state='OPEN' WHERE id=?",
        (scope["group_id"],),
    )


def _enter_four_members(db, scope, now):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    discussion = None
    for user_id, _login_key in scope["students"]:
        discussion = enter_group_discussion_stage(
            scope["session_id"], scope["group_id"], user_id
        )
    assert discussion and discussion["status"] == "running"
    # Keep the discussion origin deterministic for state-window assertions.
    origin = now - timedelta(seconds=295)
    db.execute(
        """
        UPDATE group_session_discussions
           SET started_at=?, deadline=?, status='running', updated_at=?
         WHERE id=?
        """,
        (
            origin.strftime("%Y-%m-%d %H:%M:%S"),
            (now + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%Y-%m-%d %H:%M:%S"),
            discussion["id"],
        ),
    )
    return {
        **scope,
        "discussion_id": discussion["id"],
        "all_members_entered_at": origin.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _append_script_messages(db, scope, scripted_messages):
    created = []
    for index, item in scripted_messages:
        member_index = int(item["studentId"].rsplit("M", 1)[-1]) - 1
        user_id = scope["students"][member_index][0]
        created.append(
            db.create_message(
                scope["group_id"],
                user_id,
                item["text"],
                role="student",
                session_no=scope["session_no"],
                task_id=scope["task_id"],
                client_message_id=f"batch7-original-script-{index}",
            )
        )
    return created


def _assessment_payload(start_sequence, end_sequence):
    if start_sequence == 1:
        assert end_sequence == 20
        segments = [
            {
                "state": "positive_collaboration",
                "canonical_sub_state": "standard",
                "start_sequence": 1,
                "end_sequence": 4,
                "confidence": 0.91,
                "evidence_sequences": [1, 2, 4],
            },
            {
                "state": "task_detached",
                "canonical_sub_state": "off_topic_unregulated",
                "start_sequence": 9,
                "end_sequence": 11,
                "confidence": 0.93,
                "evidence_sequences": [9, 10, 11],
            },
            {
                "state": "frustration_stuck",
                "canonical_sub_state": "frustration",
                "start_sequence": 15,
                "end_sequence": 17,
                "confidence": 0.94,
                "evidence_sequences": [15, 16, 17],
            },
            {
                "state": "conflict_tension",
                "canonical_sub_state": "interpersonal_conflict",
                "start_sequence": 18,
                "end_sequence": 20,
                "confidence": 0.96,
                "evidence_sequences": [18, 19, 20],
            },
        ]
        active_index = 3
        intervention = {
            "needed": True,
            "target_segment_index": 3,
            "reason_code": "active_conflict",
            "message": "先暂停争论，按证据、成本和可执行性逐项比较两个方案。",
        }
        primary_state = "conflict_tension"
    else:
        assert (start_sequence, end_sequence) == (22, 30)
        segments = [
            {
                "state": "positive_collaboration",
                "canonical_sub_state": "execution_progress",
                "start_sequence": start_sequence,
                "end_sequence": end_sequence,
                "confidence": 0.95,
                "evidence_sequences": [start_sequence, start_sequence + 3, end_sequence],
            }
        ]
        active_index = 0
        intervention = {
            "needed": False,
            "target_segment_index": None,
            "reason_code": "constructive_progress",
            "message": None,
        }
        primary_state = "positive_collaboration"
    return {
        "segments": segments,
        "active_segment_index": active_index,
        "intervention": intervention,
        "primary_state": primary_state,
        "state_code": primary_state,
        "confidence": segments[active_index]["confidence"],
        "evidence_message_ids": [],
    }


def _request(scheduler, scope, trigger_type, trigger_sequence):
    return scheduler.request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type=trigger_type,
        trigger_sequence=trigger_sequence,
    )


def test_original_four_person_script_preserves_strategy_pipeline_boundaries(
    db_and_app,
    monkeypatch,
):
    db, _app_module, _client = db_and_app
    original_script_bytes = SCRIPT_PATH.read_bytes()
    scripted_messages = _load_four_person_messages()
    now = datetime.now().replace(microsecond=0)
    seeded = seed_running_session(db, session_no=770, member_count=4, limit_minutes=60)
    _enable_strategy_agent(db, seeded)
    scope = _enter_four_members(db, seeded, now)

    import config
    import services.discussion_pipeline_v2.monitoring_service as monitoring_module
    import services.state_assessment_scheduler as scheduler
    from services.emotion_slot_service import EmotionSlotService
    from services.state_assessment_batch_service import StateAssessmentBatchService
    from services.teacher_emotion_review_service import get_emotion_review

    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_DRY_RUN", False)
    monkeypatch.setattr(config, "LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED", True)
    monkeypatch.setattr(scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 1)
    monkeypatch.setattr(scheduler, "STATE_LLM_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 20)
    queued = []
    monkeypatch.setattr(
        scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: queued.append((int(batch_id), int(delay or 0))),
    )
    detector_calls = []

    def fixed_detection(**kwargs):
        start = int(kwargs["fixed_candidate_start_sequence"])
        end = int(kwargs["fixed_candidate_end_sequence"])
        detector_calls.append((start, end))
        payload = _assessment_payload(start, end)
        return {
            "monitor_run_id": None,
            "state_llm_result": payload,
            "state_llm_meta": {
                "success": True,
                "analysis_failed": False,
                "analysis_skipped": False,
                "model_name": "fixed-batch7-model",
                "prompt_version": "batch7-original-script-v1",
                "raw_response": json.dumps(payload, ensure_ascii=False),
            },
        }

    monkeypatch.setattr(
        monitoring_module.MonitoringService,
        "run_detection",
        staticmethod(fixed_detection),
    )

    first_messages = _append_script_messages(
        db, scope, list(enumerate(scripted_messages[:20], start=1))
    )
    assert [row["content"] for row in first_messages] == [
        row["text"] for row in scripted_messages[:20]
    ]

    message_trigger = _request(scheduler, scope, "message_count_periodic", 20)
    time_trigger = _request(scheduler, scope, "time_periodic", 20)
    rule_trigger = _request(scheduler, scope, "rule_high_risk", 20)
    assert message_trigger["created"] is True
    assert time_trigger["assessment_batch_id"] == message_trigger["assessment_batch_id"]
    assert rule_trigger["assessment_batch_id"] == message_trigger["assessment_batch_id"]
    assert time_trigger["rerun_requested"] is True
    assert rule_trigger["rerun_requested"] is True
    assert len(queued) == 1

    first_outcome = scheduler.execute_state_assessment_batch(
        message_trigger["assessment_batch_id"]
    )
    assert first_outcome["succeeded"] is True
    assert first_outcome["intervention"]["published"] is True
    assert detector_calls == [(1, 20)]
    first_segment_states = [row["state_code"] for row in first_outcome["segments"]]
    assert first_segment_states == [
        "positive_collaboration",
        "task_detached",
        "blocked_frustration",
        "conflict_tension",
    ]
    intervention_run = db.query_one(
        "SELECT * FROM intervention_runs WHERE assessment_batch_id=?",
        (message_trigger["assessment_batch_id"],),
    )
    intervention_message = db.query_one(
        "SELECT * FROM messages WHERE id=?", (intervention_run["message_id"],)
    )
    assert intervention_run["target_segment_id"] == first_outcome["segments"][3]["id"]
    assert intervention_message["intervention_run_id"] == intervention_run["id"]

    recovery_messages = _append_script_messages(
        db, scope, list(enumerate(scripted_messages[20:], start=21))
    )
    assert recovery_messages[0]["sequence"] == intervention_message["sequence"] + 1
    observing = StateAssessmentBatchService.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=recovery_messages[0]["sequence"],
    )
    assert observing["assessment_status"] == "observing"

    recovery_trigger = _request(
        scheduler,
        scope,
        "post_intervention_observation",
        recovery_messages[-1]["sequence"],
    )
    assert (
        recovery_trigger["candidate_start_sequence"],
        recovery_trigger["candidate_end_sequence"],
    ) == (22, 30)
    recovery_outcome = scheduler.execute_state_assessment_batch(
        recovery_trigger["assessment_batch_id"]
    )
    assert recovery_outcome["succeeded"] is True
    assert recovery_outcome["intervention"]["reason"] == "intervention_not_needed"
    assert detector_calls == [(1, 20), (22, 30)]
    confirmed = StateAssessmentBatchService.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=recovery_messages[0]["sequence"],
    )
    assert confirmed["assessment_status"] == "confirmed"
    # The low-level compatibility accessor intentionally exposes the coarse
    # state; teacher/research surfaces below expose the canonical sub-state.
    assert confirmed["semantic_state"] == "positive_collaboration"

    review = get_emotion_review(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
    )
    messages_by_sequence = {row["sequence"]: row for row in review["messages"]}
    assert messages_by_sequence[9]["semantic_state"] == "off_topic_unregulated"
    assert messages_by_sequence[15]["semantic_state"] == "frustration"
    assert messages_by_sequence[18]["semantic_state"] == "interpersonal_conflict"
    assert messages_by_sequence[intervention_message["sequence"]]["assessment_status"] is None
    assert messages_by_sequence[30]["semantic_state"] == "execution_progress"
    assert review["current_state"]["semantic_state"] == "execution_progress"
    assert review["current_state"]["assessment_status"] == "confirmed"

    counts_before_replay = {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in (
            "state_assessment_batches",
            "collaboration_state_segments",
            "intervention_runs",
            "messages",
        )
    }
    replay_request = _request(scheduler, scope, "time_periodic", 30)
    replay_worker = scheduler.execute_state_assessment_batch(
        recovery_trigger["assessment_batch_id"]
    )
    assert replay_request["reason"] == "no_new_student_messages"
    assert replay_worker["claimed"] is False
    assert {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in counts_before_replay
    } == counts_before_replay

    slot_now = now + timedelta(seconds=10)
    slot = EmotionSlotService.ensure_latest_due_slot(scope, now=slot_now)
    assert slot.get("reason") == "emotion_agent_disabled"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE agent_type='emotion'"
    )["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM emotion_reflection_slots")["c"] == 0

    stable_after_close = {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in (
            "state_assessment_batches",
            "collaboration_state_segments",
            "intervention_runs",
            "emotion_reflection_slots",
            "messages",
        )
    }
    db.execute(
        "UPDATE group_session_discussions SET status='closed' WHERE id=?",
        (scope["discussion_id"],),
    )
    assert EmotionSlotService.scan_due(
        now=slot_now + timedelta(seconds=300), enqueue=False
    )["scanned"] == 0
    assert _request(scheduler, scope, "time_periodic", 30)["reason"] == (
        "discussion_not_running"
    )
    assert {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in stable_after_close
    } == stable_after_close

    assert db.query_one(
        """
        SELECT COUNT(*) AS c
          FROM collaboration_state_segments AS s
          LEFT JOIN state_assessment_batches AS b ON b.id=s.assessment_batch_id
         WHERE s.group_id=? AND (s.assessment_batch_id IS NULL OR b.id IS NULL)
        """,
        (scope["group_id"],),
    )["c"] == 0
    assert SCRIPT_PATH.read_bytes() == original_script_bytes
