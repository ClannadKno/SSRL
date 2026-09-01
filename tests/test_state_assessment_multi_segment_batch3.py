# -*- coding: utf-8 -*-
"""Batch 3 coverage for precise Stage 2 sub-state assessment output."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest

from tests.helpers import seed_running_session


class _FakeLlmResult:
    def __init__(self, output, *, success=True, failure_type=None):
        self.success = success
        self.output = output
        self.raw_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        self.model_name = "batch3-model"
        self.latency_ms = 2
        self.attempt_count = 1
        self.token_usage = {"completion_tokens": 80}
        self.failure_type = failure_type
        self.failure_message = failure_type


def _route(canonical, secondary=None):
    from services.three_stage_schema import route_for_state_with_overlays

    return route_for_state_with_overlays(canonical, secondary or [])


def _inhibition(canonical, secondary=None, reason="protect spontaneous regulation"):
    route = _route(canonical, secondary)
    if route["inhibition_strategy_id"]:
        return {
            "is_inhibited": True,
            "strategy_id": route["inhibition_strategy_id"],
            "reason": reason,
        }
    return {"is_inhibited": False, "strategy_id": None, "reason": None}


def _segment(
    canonical,
    start,
    end,
    evidence,
    confidence=0.8,
    *,
    raw=None,
    active=False,
    secondary=None,
    self_regulated=False,
):
    return {
        "raw_sub_state": raw or canonical,
        "canonical_sub_state": canonical,
        "secondary_tags": list(secondary or []),
        "start_sequence": start,
        "end_sequence": end,
        "confidence": confidence,
        "evidence_message_ids": evidence,
        "reason": f"{canonical} evidence",
        "is_active_at_window_end": active,
        "detected_self_regulation": bool(self_regulated),
    }


def _payload(segments, *, active_index=None, should_intervene=None, candidates=None):
    if active_index is None:
        active_index = len(segments) - 1
    active = segments[active_index]
    route = _route(active["canonical_sub_state"], active["secondary_tags"])
    if should_intervene is None:
        should_intervene = route["should_intervene"]
    if candidates is None:
        candidates = (
            list(route["candidate_strategy_ids"])
            if should_intervene
            else []
        )
    return {
        "schema_version": "stage2.v1",
        "analysis_scope": {
            "candidate_start_sequence": min(item["start_sequence"] for item in segments),
            "candidate_end_sequence": max(item["end_sequence"] for item in segments),
            "input_cutoff_student_sequence": max(item["end_sequence"] for item in segments),
        },
        "segments": segments,
        "active_segment_index": active_index,
        "active_sub_state": {
            "raw_sub_state": active["raw_sub_state"],
            "canonical_sub_state": active["canonical_sub_state"],
            "secondary_tags": active["secondary_tags"],
            "confidence": active["confidence"],
            "start_sequence": active["start_sequence"],
            "end_sequence": active["end_sequence"],
            "evidence_message_ids": active["evidence_message_ids"],
            "detected_self_regulation": active["detected_self_regulation"],
        },
        "should_intervene": bool(should_intervene),
        "inhibition": _inhibition(active["canonical_sub_state"], active["secondary_tags"]),
        "candidate_strategy_ids": candidates,
        "decision_reason": "validated precise sub-state",
    }


def _detector_context():
    return {
        "group_id": 7,
        "session_id": 8,
        "discussion_id": 9,
        "assessment_batch_id": 10,
        "candidate_start_sequence": 12,
        "candidate_end_sequence": 16,
        "state_detector_candidate_sequences": [12, 13, 14, 15, 16],
        "state_detector_messages": [
            {
                "id": sequence + 100,
                "sequence": sequence,
                "role": "student",
                "user_id": sequence % 2 + 1,
                "content": f"message {sequence}",
                "created_at": f"2026-07-23 05:00:{sequence:02d}",
                "is_new_since_last_assessment": sequence >= 12,
            }
            for sequence in range(10, 17)
        ],
        "current_task": {
            "title": "Evidence task",
            "question": "Which claim is best supported?",
            "phase": "discussion",
            "requirements": "Compare evidence and agree on a conclusion.",
        },
        "stage1_result": {
            "coarse_state_code": "POSSIBLE_CONFLICT",
            "coarse_decision": "ESCALATE",
        },
        "recent_intervention": {"message": "请按证据逐项比较。", "sequence": 9},
    }


def _run_detector(monkeypatch, outputs):
    detector = importlib.import_module("services.discussion_pipeline_v2.llm_state_detector")
    calls = []

    class FakeGateway:
        def call(self, profile, payload, response_type):
            calls.append((profile, payload, response_type))
            return _FakeLlmResult(outputs[min(len(calls) - 1, len(outputs) - 1)])

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)
    return detector.LLMStateDetector.detect(_detector_context()), calls


def _contains_key(payload, key):
    if isinstance(payload, dict):
        return key in payload or any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(value, key) for value in payload)
    return False


def test_detector_sends_stage2_context_and_never_requests_student_visible_text(monkeypatch):
    output = _payload(
        [
            _segment("constructive_conflict", 12, 13, [12, 13], 0.81),
            _segment("interpersonal_conflict", 15, 16, [15, 16], 0.94, active=True),
        ]
    )

    envelope, calls = _run_detector(monkeypatch, [output])

    assert len(calls) == 1
    user_payload = json.loads(calls[0][1]["messages"][1]["content"])
    assert user_payload["schema_version"] == "stage2.v1"
    assert user_payload["stage1_result"]["coarse_decision"] == "ESCALATE"
    assert user_payload["output_contract"]["no_student_visible_text"] is True
    assert [row["sequence"] for row in user_payload["context_only"]] == [10, 11]
    assert [row["sequence"] for row in user_payload["candidate_messages"]] == [12, 13, 14, 15, 16]
    assert not _contains_key(envelope["result"], "message")
    assert "intervention" not in envelope["result"]
    assert envelope["result"]["schema_version"] == "stage2.v1"
    assert envelope["result"]["active_sub_state"]["canonical_sub_state"] == "interpersonal_conflict"
    assert envelope["result"]["primary_state"] == "conflict_tension"
    assert envelope["result"]["evidence_message_ids"] == [115, 116]
    assert envelope["meta"]["prompt_version"].endswith("stage2_state_only_v10")
    assert calls[0][1]["max_tokens"] == 1800
    assert calls[0][1]["response_format"] == {"type": "json_object"}
    assert user_payload["output_contract"]["required_top_level_fields"] == [
        "sub_category",
        "canonical_state",
        "confidence",
        "evidence_message_ids",
    ]
    assert "sub_state_strategy_mapping" not in user_payload
    assert "candidate_strategy_ids" in user_payload["output_contract"][
        "forbidden_top_level_fields"
    ]
    assert user_payload["output_contract"]["burnout_must_remain_burnout"] is True


@pytest.mark.parametrize(
    ("canonical", "legacy_state", "should_intervene", "candidates", "oi_strategy"),
    [
        ("standard", "positive_collaboration", False, [], None),
        ("constructive_conflict", "positive_collaboration", False, [], "OI-001"),
        ("interpersonal_conflict", "conflict_tension", True, ["ER-001", "EE-001", "SS-004", "ER-007"], None),
        ("deep_thinking", "positive_collaboration", False, [], "OI-002"),
        ("individual_marginalization", "task_detached", True, ["EA-003", "EE-005", "SS-002"], None),
        ("confusion", "blocked_frustration", True, ["EA-001", "ER-005", "EE-006", "SS-003"], None),
        ("frustration", "blocked_frustration", True, ["ER-002", "EE-003", "SS-006", "EA-007"], None),
        ("burnout", "blocked_frustration", True, ["ER-008"], None),
        ("off_topic_self_regulated", "task_detached", False, [], "OI-003"),
        ("off_topic_unregulated", "task_detached", True, ["ER-003"], None),
        ("execution_progress", "positive_collaboration", False, [], "OI-004"),
        ("perfunctory_detachment", "task_detached", True, ["ER-006", "SS-005"], None),
    ],
)
def test_parser_distinguishes_precise_sub_states_and_routes(
    canonical, legacy_state, should_intervene, candidates, oi_strategy
):
    detector = importlib.import_module("services.discussion_pipeline_v2.llm_state_detector")
    segment = _segment(
        canonical,
        12,
        16,
        [12, 14, 16],
        active=True,
        self_regulated=canonical.endswith("self_regulated"),
    )
    output = _payload([segment])

    parsed = detector.parse_llm_json_content(output, [12, 13, 14, 15, 16])

    assert parsed["valid"] is True
    data = parsed["data"]
    assert data["segments"][0]["canonical_sub_state"] == canonical
    assert data["segments"][0]["state_code"] == legacy_state
    assert data["should_intervene"] is should_intervene
    assert data["candidate_strategy_ids"] == candidates
    assert data["inhibition"]["strategy_id"] == oi_strategy
    assert data["inhibition"]["is_inhibited"] is bool(oi_strategy)


def test_parser_accepts_multi_state_window_and_recovered_active_state():
    detector = importlib.import_module("services.discussion_pipeline_v2.llm_state_detector")
    output = _payload(
        [
            _segment("frustration", 12, 13, [12, 13], 0.91),
            _segment("constructive_conflict", 15, 16, [15, 16], 0.86, active=True),
        ]
    )

    parsed = detector.parse_llm_json_content(output, [12, 13, 14, 15, 16])

    assert parsed["valid"] is True
    assert parsed["data"]["active_segment_index"] == 1
    assert parsed["data"]["active_sub_state"]["canonical_sub_state"] == "constructive_conflict"
    assert parsed["data"]["should_intervene"] is False
    assert parsed["data"]["candidate_strategy_ids"] == []


def test_parser_routes_high_intensity_overlay_strategy_group():
    detector = importlib.import_module("services.discussion_pipeline_v2.llm_state_detector")
    output = _payload(
        [
            _segment(
                "standard",
                12,
                16,
                [12, 14, 16],
                active=True,
                secondary=["high_intensity_overload"],
            )
        ]
    )

    parsed = detector.parse_llm_json_content(output, [12, 13, 14, 15, 16])

    assert parsed["valid"] is True
    assert parsed["data"]["active_sub_state"]["canonical_sub_state"] == "standard"
    assert parsed["data"]["active_sub_state"]["secondary_tags"] == ["high_intensity_overload"]
    assert parsed["data"]["should_intervene"] is True
    assert parsed["data"]["candidate_strategy_ids"] == ["EA-005", "ER-004"]


def test_parser_allows_optional_stage_achievement_support():
    detector = importlib.import_module("services.discussion_pipeline_v2.llm_state_detector")
    output = _payload(
        [
            _segment(
                "execution_progress",
                12,
                16,
                [12, 14, 16],
                active=True,
                secondary=["stage_achievement"],
            )
        ],
        should_intervene=True,
    )

    parsed = detector.parse_llm_json_content(output, [12, 13, 14, 15, 16])

    assert parsed["valid"] is True
    assert parsed["data"]["active_sub_state"]["canonical_sub_state"] == "execution_progress"
    assert parsed["data"]["active_sub_state"]["secondary_tags"] == ["stage_achievement"]
    assert parsed["data"]["should_intervene"] is True
    assert parsed["data"]["candidate_strategy_ids"] == ["SS-007"]


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (
            lambda output: output["segments"][0].update(start_sequence=11),
            "sequence_outside_candidate",
        ),
        (
            lambda output: output["segments"].append(
                _segment("interpersonal_conflict", 13, 16, [16], active=True)
            ),
            "overlapping_or_unsorted_segments",
        ),
        (
            lambda output: output["segments"][0].update(
                canonical_sub_state="passive_silence"
            ),
            "invalid_canonical_sub_state",
        ),
        (
            lambda output: output["segments"][0].update(evidence_message_ids=[10]),
            "evidence_outside_candidate",
        ),
        (
            lambda output: output["analysis_scope"].update(candidate_end_sequence=15),
            "analysis_scope_mismatch",
        ),
        (
            lambda output: output.update(candidate_strategy_ids=["SS-001"]),
            "invalid_candidate_strategy_id",
        ),
        (
            lambda output: output.update(
                should_intervene=False,
                candidate_strategy_ids=[],
            ),
            "should_intervene_route_mismatch",
        ),
        (
            lambda output: output.update(
                inhibition={"is_inhibited": True, "strategy_id": "OI-001", "reason": "bad"}
            ),
            "unexpected_inhibition",
        ),
    ],
)
def test_parser_rejects_critical_stage2_schema_errors(mutator, error):
    detector = importlib.import_module("services.discussion_pipeline_v2.llm_state_detector")
    output = _payload(
        [_segment("interpersonal_conflict", 12, 16, [12, 14, 16], active=True)]
    )
    mutator(output)

    parsed = detector.parse_llm_json_content(output, [12, 13, 14, 15, 16])

    assert parsed["valid"] is False
    assert parsed["error_type"] == error


def test_truncated_or_invalid_json_gets_only_one_schema_repair(monkeypatch):
    valid = _payload([_segment("off_topic_unregulated", 12, 16, [12, 14, 16], active=True)])
    envelope, calls = _run_detector(monkeypatch, ["{\"segments\":[", valid])
    assert len(calls) == 2
    assert envelope["meta"]["retry_count"] == 1
    assert envelope["meta"]["success"] is True

    failed, failed_calls = _run_detector(monkeypatch, ["not-json", "still-not-json"])
    assert len(failed_calls) == 2
    assert failed["meta"]["success"] is False
    assert failed["meta"]["failure_type"] == "json_parse_error"
    assert failed["result"]["detector_error"] is True


@pytest.fixture
def batch3_env(test_env, monkeypatch):
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
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_BACKOFF_SECONDS", 1)
    scope = seed_running_session(db, session_no=930, member_count=1)
    db.execute(
        """
        UPDATE experiment_sessions
           SET strategy_agent_enabled=1, agent_intervention_enabled=1
         WHERE id=?
        """,
        (scope["session_id"],),
    )
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (scope["task_id"],),
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
    return db, scheduler, batch_service.StateAssessmentBatchService, scope, queued


def _add_candidate_messages(db, scope, sequences):
    for sequence in sequences:
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_id, session_no, task_id, discussion_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope["group_id"],
                scope["student_id"],
                f"candidate {sequence}",
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


def _request_batch(scheduler, scope, end_sequence):
    return scheduler.request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type="message_count_periodic",
        trigger_sequence=end_sequence,
    )


def _seed_stage1_pipeline(db, scope, *, start_sequence, end_sequence, locked=False):
    lock_token = f"stage1-lock-{scope['group_id']}-{end_sequence}" if locked else None
    run_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid,
            group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority,
            input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
            stage1_status, stage1_started_at, stage1_completed_at,
            coarse_decision, coarse_state_code, coarse_should_escalate,
            stage2_status, publish_status, final_status,
            room_lock_token, room_lock_acquired_at,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"test-stage1-{scope['group_id']}-{end_sequence}",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "message_count_periodic",
            200,
            start_sequence,
            end_sequence,
            end_sequence,
            "SUCCEEDED",
            db.now_str(),
            db.now_str(),
            "ESCALATE" if locked else "NO_STRONG_INTERVENTION",
            "POSSIBLE_CONFLICT" if locked else "POSSIBLE_POSITIVE",
            1 if locked else 0,
            "PENDING",
            "NOT_READY",
            "LOCKED" if locked else "PENDING_STAGE2",
            lock_token,
            db.now_str() if locked else None,
            f"stage1:test:{scope['group_id']}:{scope['discussion_id']}:{end_sequence}",
            db.now_str(),
            db.now_str(),
        ),
    )
    if locked:
        db.execute(
            """
            UPDATE groups
            SET state='AI_INTERVENING',
                lock_token=?,
                active_intervention_run_id=?,
                lock_expires_at=?
            WHERE id=?
            """,
            (
                lock_token,
                -run_id,
                (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S"),
                scope["group_id"],
            ),
        )
    return run_id


def _fake_monitoring(monkeypatch, payload, *, success=True, failure_type=None):
    monitoring = importlib.import_module("services.discussion_pipeline_v2.monitoring_service")
    calls = []

    def fake_detection(**kwargs):
        calls.append(kwargs)
        return {
            "monitor_run_id": 77,
            "state_llm_result": payload,
            "state_llm_meta": {
                "success": success,
                "analysis_failed": not success,
                "analysis_skipped": False,
                "failure_type": failure_type,
                "failure_message": failure_type,
                "model_name": "batch3-model",
                "prompt_version": "batch3-stage2",
                "raw_response": json.dumps(payload, ensure_ascii=False),
            },
        }

    monkeypatch.setattr(monitoring.MonitoringService, "run_detection", staticmethod(fake_detection))
    return calls


def test_worker_persists_precise_segments_and_defers_risk_to_stage3(batch3_env, monkeypatch):
    db, scheduler, service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [12, 13, 14, 15, 16])
    requested = _request_batch(scheduler, scope, 16)
    pipeline_id = _seed_stage1_pipeline(
        db, scope, start_sequence=12, end_sequence=16, locked=True
    )
    payload = _payload(
        [
            _segment("constructive_conflict", 12, 12, [12], 0.8),
            _segment("frustration", 14, 14, [14], 0.9),
            _segment("interpersonal_conflict", 15, 16, [15, 16], 0.94, active=True),
        ]
    )
    call_kwargs = _fake_monitoring(monkeypatch, payload)

    outcome = scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])

    assert outcome["succeeded"] is True
    assert call_kwargs[0]["persist_state_segment"] is False
    assert call_kwargs[0]["schedule_strategy"] == "legacy_only"
    assert outcome["intervention"]["reason"] == "stage2_deferred_to_stage3"
    rows = db.query_all(
        "SELECT * FROM collaboration_state_segments WHERE assessment_batch_id=? ORDER BY segment_order",
        (requested["assessment_batch_id"],),
    )
    assert [row["canonical_sub_state_code"] for row in rows] == [
        "constructive_conflict",
        "frustration",
        "interpersonal_conflict",
    ]
    assert [row["state_code"] for row in rows] == [
        "positive_collaboration",
        "blocked_frustration",
        "conflict_tension",
    ]
    assert [row["is_active_at_batch_end"] for row in rows] == [0, 0, 1]
    assert {row["strategy_pipeline_run_id"] for row in rows} == {pipeline_id}
    assert all(row["source_stage"] == "stage2" for row in rows)
    assert rows[2]["should_intervene"] == 1
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    ) == 16
    batch = db.query_one(
        "SELECT * FROM state_assessment_batches WHERE id=?",
        (requested["assessment_batch_id"],),
    )
    stored = json.loads(batch["parsed_response"])
    assert stored["schema_version"] == "stage2.v1"
    assert "intervention" not in stored
    pipeline = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert pipeline["stage2_status"] == "SUCCEEDED"
    assert pipeline["canonical_sub_state_code"] == "interpersonal_conflict"
    assert pipeline["should_intervene"] == 1
    assert pipeline["stage3_status"] == "PENDING"
    assert pipeline["publish_status"] == "NOT_READY"
    assert pipeline["final_status"] == "PENDING_STAGE3"
    assert json.loads(pipeline["strategy_candidate_ids_json"]) == ["ER-001", "EE-001", "SS-004", "ER-007"]
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 0
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM intervention_runs WHERE group_id=?",
        (scope["group_id"],),
    )["c"] == 0


def test_worker_hands_preliminary_lock_to_authoritative_batch_cutoff(
    batch3_env,
    monkeypatch,
):
    db, scheduler, _service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [31, 32, 33, 34, 35, 36])
    requested = _request_batch(scheduler, scope, 36)
    owner_id = _seed_stage1_pipeline(
        db,
        scope,
        start_sequence=31,
        end_sequence=34,
        locked=True,
    )
    target_id = _seed_stage1_pipeline(
        db,
        scope,
        start_sequence=31,
        end_sequence=36,
        locked=False,
    )
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET coarse_decision='ESCALATE',
               coarse_state_code='POSSIBLE_BLOCKED',
               coarse_should_escalate=1,
               final_status='WAITING_FOR_LOCK',
               skip_reason='ROOM_LOCK_UNAVAILABLE'
         WHERE id=?
        """,
        (target_id,),
    )
    payload = _payload(
        [
            _segment(
                "confusion",
                31,
                36,
                [31, 34, 36],
                0.9,
                active=True,
            )
        ]
    )
    _fake_monitoring(monkeypatch, payload)

    outcome = scheduler.execute_state_assessment_batch(
        requested["assessment_batch_id"]
    )

    assert outcome["succeeded"] is True
    assert outcome["stage2_preparation"]["pipeline_run_id"] == target_id
    assert outcome["stage2_initial_lease"]["acquired"] is True
    assert (
        outcome["stage2_initial_lease"]["transferred_from_pipeline_id"]
        == owner_id
    )
    owner = db.query_one(
        """
        SELECT final_status, publish_status, skip_reason,
               superseded_by_run_id
          FROM strategy_pipeline_runs
         WHERE id=?
        """,
        (owner_id,),
    )
    assert dict(owner) == {
        "final_status": "SUPERSEDED",
        "publish_status": "SKIPPED",
        "skip_reason": "SUPERSEDED_BY_STATE_BATCH",
        "superseded_by_run_id": target_id,
    }
    target = db.query_one(
        """
        SELECT stage2_status, canonical_sub_state_code,
               room_lock_token, final_status
          FROM strategy_pipeline_runs
         WHERE id=?
        """,
        (target_id,),
    )
    assert target["stage2_status"] == "SUCCEEDED"
    assert target["canonical_sub_state_code"] == "confusion"
    assert target["room_lock_token"]
    assert target["final_status"] == "PENDING_STAGE3"
    room = db.query_one(
        """
        SELECT state, lock_token, active_intervention_run_id
          FROM groups
         WHERE id=?
        """,
        (scope["group_id"],),
    )
    assert room["state"] == "AI_INTERVENING"
    assert room["lock_token"] == target["room_lock_token"]
    assert room["active_intervention_run_id"] == -target_id


def test_worker_suppresses_oi_state_and_releases_stage1_lock(batch3_env, monkeypatch):
    db, scheduler, _service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [21, 22, 23])
    requested = _request_batch(scheduler, scope, 23)
    pipeline_id = _seed_stage1_pipeline(
        db, scope, start_sequence=21, end_sequence=23, locked=True
    )
    payload = _payload(
        [_segment("deep_thinking", 21, 23, [21, 23], 0.88, active=True)]
    )
    _fake_monitoring(monkeypatch, payload)

    outcome = scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])

    assert outcome["succeeded"] is True
    assert outcome["stage2_pipeline"]["should_enter_stage3"] is False
    assert outcome["stage2_pipeline"]["lock_released"] is True
    pipeline = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert pipeline["stage2_status"] == "SUCCEEDED"
    assert pipeline["canonical_sub_state_code"] == "deep_thinking"
    assert pipeline["should_intervene"] == 0
    assert pipeline["inhibition_strategy_id"] == "OI-002"
    assert pipeline["stage3_status"] == "SKIPPED"
    assert pipeline["publish_status"] == "SUPPRESSED"
    assert pipeline["final_status"] == "SUPPRESSED"
    assert pipeline["skip_reason"] == "OI_SUPPRESSED:OI-002"
    assert pipeline["room_lock_released_at"]
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 0


def test_worker_marks_stale_when_new_student_message_arrives_after_window(batch3_env, monkeypatch):
    db, scheduler, _service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [31, 32])
    requested = _request_batch(scheduler, scope, 32)
    pipeline_id = _seed_stage1_pipeline(
        db, scope, start_sequence=31, end_sequence=32, locked=True
    )
    _add_candidate_messages(db, scope, [33])
    payload = _payload(
        [_segment("interpersonal_conflict", 31, 32, [31, 32], 0.9, active=True)]
    )
    _fake_monitoring(monkeypatch, payload)

    outcome = scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])

    assert outcome["succeeded"] is True
    assert outcome["stage2_pipeline"]["stale"] is True
    pipeline = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert pipeline["final_status"] == "STALE"
    assert pipeline["stage3_status"] == "SKIPPED"
    assert pipeline["skip_reason"] == "STALE_NEW_STUDENT_MESSAGE"
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["active_intervention_run_id"] is None


def test_failed_structured_output_persists_unclassified_and_releases_lock(
    batch3_env, monkeypatch
):
    db, scheduler, service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [41, 42])
    requested = _request_batch(scheduler, scope, 42)
    pipeline_id = _seed_stage1_pipeline(
        db, scope, start_sequence=41, end_sequence=42, locked=True
    )
    payload = {
        "detector_error": True,
        "schema_version": "stage2.v1",
        "segments": [],
    }
    call_kwargs = _fake_monitoring(
        monkeypatch,
        payload,
        success=False,
        failure_type="schema_validation_error",
    )

    outcome = scheduler.execute_state_assessment_batch(requested["assessment_batch_id"])

    assert outcome["succeeded"] is False
    assert outcome["reason"] == "schema_validation_error"
    assert call_kwargs[0]["schedule_strategy"] == "legacy_only"
    assert outcome["retry"]["retried"] is False
    assert outcome["retry"]["reason"] == "structured_output_retry_exhausted"
    pipeline = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    assert pipeline["stage2_status"] == "FAILED"
    assert pipeline["stage3_status"] == "SKIPPED"
    assert pipeline["final_status"] == "FAILED"
    assert pipeline["skip_reason"] == "STAGE2_FAILED"
    assert pipeline["room_lock_released_at"]
    fallback = db.query_one(
        """
        SELECT assessment_status, canonical_sub_state_code, fallback_reason
        FROM collaboration_state_segments
        WHERE assessment_batch_id=?
        """,
        (requested["assessment_batch_id"],),
    )
    assert dict(fallback) == {
        "assessment_status": "unclassified",
        "canonical_sub_state_code": None,
        "fallback_reason": "batch_unclassified",
    }
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM intervention_runs WHERE assessment_batch_id=?",
        (requested["assessment_batch_id"],),
    )["c"] == 0
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    ) == 0
    cursor = db.query_one(
        """
        SELECT last_scheduled_student_sequence
        FROM discussion_assessment_cursors
        WHERE group_id=? AND session_id=? AND discussion_id=?
        """,
        (scope["group_id"], scope["session_id"], scope["discussion_id"]),
    )
    assert cursor["last_scheduled_student_sequence"] == 42
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert group["state"] == "OPEN"
    assert group["lock_token"] is None
    assert group["active_intervention_run_id"] is None


def test_persistence_rejects_non_candidate_sequence_without_advancing_cursor(batch3_env):
    db, scheduler, service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [51, 52])
    requested = _request_batch(scheduler, scope, 52)
    service.claim_batch(requested["assessment_batch_id"])

    with pytest.raises(ValueError):
        service.save_successful_segments(
            requested["assessment_batch_id"],
            [_segment("interpersonal_conflict", 50, 52, [52], active=True)],
        )

    assert db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_segments WHERE assessment_batch_id=?",
        (requested["assessment_batch_id"],),
    )["c"] == 0
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    ) == 0


def test_persistence_rejects_invalid_canonical_sub_state_without_advancing_cursor(batch3_env):
    db, scheduler, service, scope, _queued = batch3_env
    _add_candidate_messages(db, scope, [61, 62])
    requested = _request_batch(scheduler, scope, 62)
    service.claim_batch(requested["assessment_batch_id"])
    segment = _segment("interpersonal_conflict", 61, 62, [61, 62], active=True)
    segment["canonical_sub_state"] = "passive_silence"

    with pytest.raises(ValueError, match="invalid_canonical_sub_state"):
        service.save_successful_segments(requested["assessment_batch_id"], [segment])

    assert db.query_one(
        "SELECT COUNT(*) AS c FROM collaboration_state_segments WHERE assessment_batch_id=?",
        (requested["assessment_batch_id"],),
    )["c"] == 0
    assert service.get_last_finalized_student_sequence(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    ) == 0
