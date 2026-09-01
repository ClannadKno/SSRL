# -*- coding: utf-8 -*-
"""Plan batch 3: terminal LLM windows must not block later assessments."""

from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.helpers import seed_running_session


@pytest.fixture
def batch3_env(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scheduler = importlib.import_module("services.state_assessment_scheduler")
    batch_module = importlib.import_module("services.state_assessment_batch_service")
    queued = []

    monkeypatch.setattr(
        scheduler,
        "_enqueue_batch",
        lambda batch_id, delay=0: queued.append((int(batch_id), int(delay or 0))),
    )
    monkeypatch.setattr(scheduler, "STATE_LLM_MESSAGE_THRESHOLD", 1)
    monkeypatch.setattr(scheduler, "STATE_LLM_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 10)
    monkeypatch.setattr(scheduler, "STATE_LLM_CONTEXT_MESSAGES", 3)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_BACKOFF_SECONDS", 1)

    def make_scope(session_no: int):
        scope = seed_running_session(db, session_no=session_no, member_count=1)
        db.execute(
            """
            UPDATE experiment_sessions
               SET agent_mode='none',
                   strategy_agent_enabled=0,
                   emotion_agent_enabled=0,
                   agent_intervention_enabled=0,
                   research_state_monitoring_enabled=1
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
        return scope

    return db, scheduler, batch_module.StateAssessmentBatchService, make_scope, queued


def _add_messages(db, scope, sequences):
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
                f"student message {sequence}",
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


def _request(scheduler, scope, *, continuation=False):
    return scheduler.request_state_assessment(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        trigger_type="rule_high_risk",
        trigger_sequence=None,
        continuation=continuation,
    )


def _save_empty_success(service, request):
    claimed = service.claim_batch(request["assessment_batch_id"])
    assert claimed["claimed"] is True
    return service.save_successful_segments(
        request["assessment_batch_id"],
        [],
        parsed_response={"segments": [], "active_segment_index": None},
    )


def _timeout_detection():
    return {
        "monitor_run_id": 301,
        "state_llm_result": {
            "segments": [],
            "primary_state": "unknown",
        },
        "state_llm_meta": {
            "success": False,
            "analysis_failed": True,
            "analysis_skipped": False,
            "failure_type": "read_timeout",
            "failure_message": "mocked state detector read timeout",
        },
    }


def _stage2_payload(
    canonical: str,
    *,
    should_intervene: bool = None,
    inhibition_id: str = None,
    candidate_strategy_ids: list[str] = None,
) -> dict:
    if should_intervene is None:
        should_intervene = canonical in {
            "interpersonal_conflict",
            "confusion",
            "frustration",
            "burnout",
            "off_topic_unregulated",
            "perfunctory_detachment",
            "individual_marginalization",
        }
    return {
        "schema_version": "stage2.v1",
        "analysis_scope": {
            "candidate_start_sequence": 1,
            "candidate_end_sequence": 2,
            "input_cutoff_student_sequence": 2,
        },
        "segments": [
            {
                "raw_sub_state": canonical,
                "canonical_sub_state": canonical,
                "secondary_tags": [],
                "start_sequence": 1,
                "end_sequence": 2,
                "confidence": 0.82,
                "evidence_message_ids": [1, 2],
                "reason": "precise state evidence",
                "is_active_at_window_end": True,
                "detected_self_regulation": False,
            }
        ],
        "active_sub_state": {
            "raw_sub_state": canonical,
            "canonical_sub_state": canonical,
            "secondary_tags": [],
            "confidence": 0.82,
            "start_sequence": 1,
            "end_sequence": 2,
            "evidence_message_ids": [1, 2],
            "detected_self_regulation": False,
        },
        "should_intervene": should_intervene,
        "inhibition": {
            "is_inhibited": bool(inhibition_id),
            "strategy_id": inhibition_id,
            "reason": "protect self regulation" if inhibition_id else None,
        },
        "candidate_strategy_ids": list(candidate_strategy_ids or []),
        "decision_reason": "validated precise sub-state",
    }


def test_stage2_contract_rejects_bad_json_and_bad_inhibition():
    detector = importlib.import_module(
        "services.discussion_pipeline_v2.llm_state_detector"
    )

    truncated = detector.parse_llm_json_content('{"schema_version":"stage2.v1"', [1, 2])
    assert truncated["valid"] is False
    assert truncated["error_type"] == "truncated_response"

    invalid_json = detector.parse_llm_json_content("{not valid json}", [1, 2])
    assert invalid_json["valid"] is False
    assert invalid_json["error_type"] == "json_parse_error"

    invalid_inhibition = _stage2_payload("execution_progress", inhibition_id="OI-004")
    invalid_inhibition["inhibition"] = "OI-004"
    parsed = detector.parse_llm_json_content(invalid_inhibition, [1, 2])
    assert parsed["valid"] is False
    assert parsed["error_type"] == "invalid_inhibition"

    non_oi_with_inhibition = _stage2_payload(
        "interpersonal_conflict",
        should_intervene=True,
        inhibition_id="OI-001",
        candidate_strategy_ids=["ER-001"],
    )
    parsed = detector.parse_llm_json_content(non_oi_with_inhibition, [1, 2])
    assert parsed["valid"] is False
    assert parsed["error_type"] == "unexpected_inhibition"

    oi_missing_inhibition = _stage2_payload("execution_progress")
    parsed = detector.parse_llm_json_content(oi_missing_inhibition, [1, 2])
    assert parsed["valid"] is False
    assert parsed["error_type"] == "oi_inhibition_required"


def test_oi_inhibition_does_not_require_public_candidate_strategy():
    detector = importlib.import_module(
        "services.discussion_pipeline_v2.llm_state_detector"
    )

    parsed = detector.parse_llm_json_content(
        _stage2_payload("execution_progress", inhibition_id="OI-004"),
        [1, 2],
    )

    assert parsed["valid"] is True
    assert parsed["data"]["should_intervene"] is False
    assert parsed["data"]["inhibition"]["strategy_id"] == "OI-004"
    assert parsed["data"]["candidate_strategy_ids"] == []


def test_truncated_response_gets_one_schema_retry_and_succeeds(monkeypatch):
    detector = importlib.import_module(
        "services.discussion_pipeline_v2.llm_state_detector"
    )
    calls = []

    class FakeResult:
        def __init__(self, output, *, finish_reason):
            self.success = True
            self.output = output
            self.raw_text = output if isinstance(output, str) else json.dumps(output)
            self.model_name = "batch3-detector"
            self.latency_ms = 7
            self.token_usage = {"completion_tokens": 42}
            self.finish_reason = finish_reason
            self.failure_type = None
            self.failure_message = None

    class FakeGateway:
        def call(self, _profile, payload, _response_type):
            calls.append(payload)
            if len(calls) == 1:
                return FakeResult('{"schema_version":"stage2.v1"', finish_reason="length")
            return FakeResult(
                _stage2_payload("execution_progress", inhibition_id="OI-004"),
                finish_reason="stop",
            )

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "STATE_LLM_SCHEMA_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(detector, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)

    result = detector.LLMStateDetector.detect(
        {
            "group_id": 1,
            "session_id": 1,
            "discussion_id": 1,
            "assessment_batch_id": 9,
            "recent_student_messages": [
                {
                    "id": 1,
                    "sequence": 1,
                    "role": "student",
                    "user_id": 1,
                    "content": "我们继续整理方案。",
                    "created_at": "2026-07-24 02:00:00",
                },
                {
                    "id": 2,
                    "sequence": 2,
                    "role": "student",
                    "user_id": 1,
                    "content": "预算表已经完成。",
                    "created_at": "2026-07-24 02:00:10",
                },
            ],
        }
    )

    assert result["meta"]["success"] is True
    assert result["meta"]["retry_used"] is True
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert result["meta"]["validation_attempts"][0]["schema_error"] == "truncated_response"
    assert result["meta"]["finish_reason"] == "stop"
    assert result["result"]["inhibition"]["strategy_id"] == "OI-004"


def test_exhausted_window_advances_scheduling_cursor_and_processes_later_messages(
    batch3_env, monkeypatch
):
    db, scheduler, service, make_scope, _queued = batch3_env
    scope = make_scope(930)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 4)
    _add_messages(db, scope, range(1, 9))

    first = _request(scheduler, scope)
    _save_empty_success(service, first)
    second = _request(scheduler, scope, continuation=True)
    _save_empty_success(service, second)

    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 10)
    _add_messages(db, scope, range(9, 23))
    failed_window = _request(scheduler, scope)
    monitoring = importlib.import_module(
        "services.discussion_pipeline_v2.monitoring_service"
    )
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(lambda **_kwargs: _timeout_detection()),
    )

    first_failure = scheduler.execute_state_assessment_batch(
        failed_window["assessment_batch_id"]
    )
    assert first_failure["error_code"] == "read_timeout"
    assert first_failure["retry"]["retried"] is True
    db.execute(
        "UPDATE state_assessment_batches SET next_retry_at=? WHERE id=?",
        ("2000-01-01 00:00:00", failed_window["assessment_batch_id"]),
    )
    exhausted = scheduler.execute_state_assessment_batch(
        failed_window["assessment_batch_id"]
    )

    terminal = exhausted["retry"]["terminal"]
    assert terminal["terminalized"] is True
    assert terminal["batch"]["terminal_status"] == "quarantined"
    assert terminal["batch"]["error_code"] == "read_timeout"
    assert terminal["batch"]["fallback_action"] == "unclassified"
    assert terminal["batch"]["fallback_segment_count"] == 1
    assert json.loads(terminal["batch"]["student_sequences_json"]) == list(
        range(9, 19)
    )
    fallback_segment = db.query_one(
        """
        SELECT * FROM collaboration_state_segments
        WHERE assessment_batch_id=? AND fallback_reason='batch_unclassified'
        """,
        (failed_window["assessment_batch_id"],),
    )
    assert fallback_segment["assessment_status"] == "unclassified"
    assert fallback_segment["source"] == "llm"
    assert fallback_segment["should_intervene"] == 0
    assert fallback_segment["selected_strategy_id"] is None
    assert (fallback_segment["start_sequence"], fallback_segment["end_sequence"]) == (9, 18)
    assert db.query_one(
        "SELECT COUNT(*) AS count FROM intervention_runs WHERE group_id=?",
        (scope["group_id"],),
    )["count"] == 0
    assert exhausted["continuation"]["candidate_start_sequence"] == 19
    assert exhausted["continuation"]["candidate_end_sequence"] == 22

    cursor = service.get_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert cursor["last_finalized_student_sequence"] == 8
    assert cursor["last_scheduled_student_sequence"] == 18
    unclassified = service.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=9,
    )
    assert unclassified["semantic_state"] is None
    assert unclassified["assessment_status"] == "unclassified"
    assert unclassified["segment_id"] == fallback_segment["id"]
    assert unclassified["fallback_reason"] == "batch_unclassified"
    rows = db.query_all(
        """
        SELECT candidate_start_sequence, candidate_end_sequence, status,
               terminal_status
        FROM state_assessment_batches
        WHERE discussion_id=?
        ORDER BY id
        """,
        (scope["discussion_id"],),
    )
    assert [
        (row["candidate_start_sequence"], row["candidate_end_sequence"])
        for row in rows
    ] == [(1, 4), (5, 8), (9, 18), (19, 22)]


def test_terminal_window_uses_explicit_confirmed_rule_evidence(batch3_env, monkeypatch):
    db, scheduler, service, make_scope, _queued = batch3_env
    scope = make_scope(931)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 1)
    _add_messages(db, scope, range(9, 19))
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, session_no, task_id, discussion_id,
            rule_state_code, fused_state_code, assessment_status,
            confidence, rule_assessment_json, context_json, created_at
        ) VALUES(?,?,?,?,?,?,'task_detached','confirmed',?,?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            "task_detached",
            0.83,
            json.dumps({"evidence_sequences": [11, 12]}, ensure_ascii=False),
            json.dumps({}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    requested = _request(scheduler, scope)
    monitoring = importlib.import_module(
        "services.discussion_pipeline_v2.monitoring_service"
    )
    monkeypatch.setattr(
        monitoring.MonitoringService,
        "run_detection",
        staticmethod(lambda **_kwargs: _timeout_detection()),
    )

    result = scheduler.execute_state_assessment_batch(
        requested["assessment_batch_id"]
    )

    terminal = result["retry"]["terminal"]
    assert terminal["batch"]["terminal_status"] == "degraded"
    assert terminal["batch"]["fallback_action"] == "degraded_rule_segments"
    assert terminal["batch"]["fallback_segment_count"] == 3
    segment = db.query_one(
        """
        SELECT * FROM collaboration_state_segments
        WHERE assessment_batch_id=? AND fallback_reason='batch_retry_exhausted'
        """,
        (requested["assessment_batch_id"],),
    )
    assert segment["source"] == "rule"
    assert segment["assessment_id"] == assessment_id
    assert (segment["start_sequence"], segment["end_sequence"]) == (11, 12)
    assert json.loads(segment["evidence_sequences"]) == [11, 12]
    unclassified = db.query_all(
        """
        SELECT start_sequence, end_sequence, assessment_status, should_intervene
        FROM collaboration_state_segments
        WHERE assessment_batch_id=? AND fallback_reason='batch_unclassified'
        ORDER BY start_sequence
        """,
        (requested["assessment_batch_id"],),
    )
    assert [
        (row["start_sequence"], row["end_sequence"], row["assessment_status"])
        for row in unclassified
    ] == [(9, 10, "unclassified"), (13, 18, "unclassified")]
    assert all(row["should_intervene"] == 0 for row in unclassified)
    classification = service.get_message_classification(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        sequence=11,
    )
    assert classification["semantic_state"] == "task_detached"
    assert classification["fallback_reason"] == "batch_retry_exhausted"
    trend_module = importlib.import_module("services.teacher_emotion_trend_service")
    trend = trend_module.get_emotion_trend(
        scope["group_id"],
        session_id=scope["session_id"],
    )
    degraded = [
        item
        for item in trend["state_segments"]
        if item.get("assessment_batch_id") == requested["assessment_batch_id"]
    ]
    degraded_rule = [
        item for item in degraded if item.get("fallback_reason") == "batch_retry_exhausted"
    ]
    batch_unclassified = [
        item for item in degraded if item.get("fallback_reason") == "batch_unclassified"
    ]
    assert degraded_rule[0]["segment_source"] == "degraded_rule"
    assert degraded_rule[0]["batch_terminal_status"] == "degraded"
    assert [item["segment_source"] for item in batch_unclassified] == [
        "batch_unclassified",
        "batch_unclassified",
    ]
    assert all(item["assessment_status"] == "unclassified" for item in batch_unclassified)


def test_terminalization_is_concurrent_and_idempotent(batch3_env, monkeypatch):
    db, scheduler, service, make_scope, _queued = batch3_env
    scope = make_scope(932)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 1)
    _add_messages(db, scope, range(1, 5))
    requested = _request(scheduler, scope)
    assert service.claim_batch(requested["assessment_batch_id"])["claimed"] is True
    service.mark_batch_failed(
        requested["assessment_batch_id"],
        error_code="read_timeout",
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _index: service.terminalize_exhausted_batch(
                    requested["assessment_batch_id"]
                ),
                range(4),
            )
        )

    assert sum(1 for result in results if result["terminalized"]) == 1
    batch = service.get_batch(requested["assessment_batch_id"])
    assert batch["terminal_status"] == "quarantined"
    assert batch["fallback_segment_count"] == 1
    assert db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM collaboration_state_segments
        WHERE assessment_batch_id=? AND fallback_reason='batch_unclassified'
        """,
        (requested["assessment_batch_id"],),
    )["count"] == 1
    cursor = service.get_cursor(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
    )
    assert cursor["last_scheduled_student_sequence"] == 4


def test_state_only_replay_replaces_unclassified_fallback_with_canonical_segment(
    batch3_env, monkeypatch
):
    db, scheduler, service, make_scope, _queued = batch3_env
    scope = make_scope(936)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 4)
    _add_messages(db, scope, range(1, 5))
    requested = _request(scheduler, scope)
    assert service.claim_batch(requested["assessment_batch_id"])["claimed"] is True
    service.mark_batch_failed(requested["assessment_batch_id"], error_code="read_timeout")
    terminal = service.terminalize_exhausted_batch(requested["assessment_batch_id"])
    assert terminal["batch"]["fallback_segment_count"] == 1

    prepared = service.prepare_scope_reprocessing(
        group_id=scope["group_id"],
        session_id=scope["session_id"],
        discussion_id=scope["discussion_id"],
        apply=True,
        error_codes=("read_timeout",),
    )
    assert prepared["prepared"] is True
    replay = _request(scheduler, scope, continuation=True)
    assert replay["assessment_batch_id"] == requested["assessment_batch_id"]
    assert service.claim_batch(replay["assessment_batch_id"])["claimed"] is True
    service.save_successful_segments(
        replay["assessment_batch_id"],
        [
            {
                "canonical_sub_state_code": "execution_progress",
                "raw_sub_state_code": "execution_progress",
                "start_sequence": 1,
                "end_sequence": 4,
                "confidence": 0.86,
                "evidence_sequences": [1, 2, 4],
                "segment_order": 0,
                "source": "llm",
                "assessment_status": "confirmed",
                "is_active_at_batch_end": True,
                "trigger_type": "rule_high_risk",
            }
        ],
        parsed_response={
            "schema_version": "stage2.v1",
            "segments": [],
            "active_segment_index": 0,
        },
    )

    assert db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM collaboration_state_segments
        WHERE assessment_batch_id=? AND fallback_reason='batch_unclassified'
        """,
        (requested["assessment_batch_id"],),
    )["count"] == 0
    canonical = db.query_one(
        """
        SELECT canonical_sub_state_code, assessment_status, selected_strategy_id
        FROM collaboration_state_segments
        WHERE assessment_batch_id=?
        """,
        (requested["assessment_batch_id"],),
    )
    assert canonical["canonical_sub_state_code"] == "execution_progress"
    assert canonical["assessment_status"] == "confirmed"
    assert canonical["selected_strategy_id"] is None
    trend_module = importlib.import_module("services.teacher_emotion_trend_service")
    trend = trend_module.get_emotion_trend(scope["group_id"], session_id=scope["session_id"])
    segment = next(
        item
        for item in trend["state_segments"]
        if item["assessment_batch_id"] == requested["assessment_batch_id"]
    )
    assert segment["final_sub_state_code"] == "execution_progress"
    assert segment["assignment_source"] == "model_segment"


def test_failed_worker_crash_is_recovered_without_rescanning_window(
    batch3_env, monkeypatch
):
    db, scheduler, service, make_scope, _queued = batch3_env
    scope = make_scope(933)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(scheduler, "STATE_LLM_MAX_CANDIDATE_MESSAGES", 4)
    _add_messages(db, scope, range(1, 7))
    first = _request(scheduler, scope)
    assert service.claim_batch(first["assessment_batch_id"])["claimed"] is True
    service.mark_batch_failed(
        first["assessment_batch_id"],
        error_code="read_timeout",
    )

    recovered_request = _request(scheduler, scope, continuation=True)

    original = service.get_batch(first["assessment_batch_id"])
    assert original["terminal_status"] == "quarantined"
    assert recovered_request["candidate_start_sequence"] == 5
    assert recovered_request["candidate_end_sequence"] == 6
    assert recovered_request["recovered_terminal_batches"] == [
        first["assessment_batch_id"]
    ]


def test_terminal_failure_is_isolated_per_session(batch3_env, monkeypatch):
    _db, scheduler, service, make_scope, _queued = batch3_env
    first_scope = make_scope(934)
    monkeypatch.setattr(scheduler, "STATE_LLM_FAILURE_MAX_ATTEMPTS", 1)
    _add_messages(_db, first_scope, range(1, 5))
    first = _request(scheduler, first_scope)
    assert service.claim_batch(first["assessment_batch_id"])["claimed"] is True
    service.mark_batch_failed(first["assessment_batch_id"], error_code="read_timeout")
    service.terminalize_exhausted_batch(first["assessment_batch_id"])

    second_scope = make_scope(935)
    _add_messages(_db, second_scope, range(1, 5))
    second = _request(scheduler, second_scope)

    assert (second["candidate_start_sequence"], second["candidate_end_sequence"]) == (
        1,
        4,
    )
    second_cursor = service.get_cursor(
        group_id=second_scope["group_id"],
        session_id=second_scope["session_id"],
        discussion_id=second_scope["discussion_id"],
    )
    assert second_cursor["last_scheduled_student_sequence"] == 0


def test_read_timeout_is_not_reclassified_as_schema_error(monkeypatch):
    detector = importlib.import_module(
        "services.discussion_pipeline_v2.llm_state_detector"
    )

    class TimeoutResult:
        success = False
        output = None
        raw_text = ""
        model_name = "mock-timeout-model"
        latency_ms = 10
        token_usage = None
        failure_type = "read_timeout"
        failure_message = "mocked timeout"

    class TimeoutGateway:
        def call(self, *_args, **_kwargs):
            return TimeoutResult()

    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: TimeoutGateway())
    monkeypatch.setattr(detector, "_write_llm_audit", lambda *_args, **_kwargs: None)
    context = {
        "group_id": 1,
        "recent_student_messages": [
            {
                "id": 1,
                "sequence": 1,
                "role": "student",
                "user_id": 1,
                "content": "我们继续分析。",
                "created_at": "2026-07-24 02:00:00",
            }
        ],
    }

    result = detector.LLMStateDetector.detect(context)

    assert result["meta"]["failure_type"] == "read_timeout"
    assert result["result"]["error_type"] == "read_timeout"


def test_batch3_schema_migration_is_idempotent(batch3_env):
    db, _scheduler, _service, _make_scope, _queued = batch3_env

    db.ensure_database_ready()
    db.ensure_database_ready()

    cursor_columns = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(discussion_assessment_cursors)")
    }
    batch_columns = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(state_assessment_batches)")
    }
    segment_columns = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(collaboration_state_segments)")
    }
    batch_indexes = {
        row["name"]
        for row in db.query_all("PRAGMA index_list(state_assessment_batches)")
    }
    assert {
        "last_scheduled_student_sequence",
        "last_scheduling_completed_at",
    } <= cursor_columns
    assert {
        "student_sequences_json",
        "terminal_status",
        "terminal_at",
        "fallback_action",
        "fallback_segment_count",
    } <= batch_columns
    assert "fallback_reason" in segment_columns
    assert "idx_state_assessment_batches_terminal" in batch_indexes


def test_legacy_failed_rows_upgrade_without_historical_terminalization(
    test_env, tmp_path
):
    migrations = importlib.import_module("migrations")
    legacy_path = tmp_path / "legacy-batch3.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.executescript(
        """
        CREATE TABLE state_assessment_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            discussion_id INTEGER NOT NULL,
            candidate_start_sequence INTEGER NOT NULL,
            candidate_end_sequence INTEGER NOT NULL,
            context_start_sequence INTEGER,
            context_end_sequence INTEGER,
            trigger_type TEXT NOT NULL,
            trigger_sequence INTEGER,
            window_key TEXT NOT NULL,
            status TEXT NOT NULL,
            rerun_requested INTEGER NOT NULL DEFAULT 0,
            request_priority INTEGER NOT NULL DEFAULT 0,
            last_trigger_sequence INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            next_retry_at TEXT,
            enqueued_at TEXT,
            model TEXT,
            prompt_version TEXT,
            raw_response TEXT,
            parsed_response TEXT,
            error_code TEXT,
            error_detail TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE discussion_assessment_cursors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            discussion_id INTEGER NOT NULL,
            last_finalized_student_sequence INTEGER NOT NULL DEFAULT 0,
            last_assessment_requested_at TEXT,
            last_assessment_completed_at TEXT,
            last_intervention_sequence INTEGER,
            observation_started_sequence INTEGER,
            observation_status TEXT NOT NULL DEFAULT 'inactive',
            updated_at TEXT NOT NULL,
            UNIQUE(group_id, session_id, discussion_id)
        );
        INSERT INTO state_assessment_batches(
            group_id, session_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            trigger_type, window_key, status, attempt_count, max_attempts,
            error_code, created_at, updated_at
        ) VALUES(1,1,1,9,18,'message_count_periodic','legacy-window',
                 'failed',2,2,'schema_validation_error',
                 '2026-07-23 22:00:00','2026-07-23 22:00:00');
        INSERT INTO discussion_assessment_cursors(
            group_id, session_id, discussion_id,
            last_finalized_student_sequence, updated_at
        ) VALUES(1,1,1,8,'2026-07-23 22:00:00');
        """
    )

    migrations._migration_incremental_state_assessment_tables(conn)
    migrations._migration_incremental_state_assessment_tables(conn)
    conn.commit()

    batch = conn.execute(
        """
        SELECT status, terminal_status, student_sequences_json
        FROM state_assessment_batches
        WHERE id=1
        """
    ).fetchone()
    cursor = conn.execute(
        """
        SELECT last_finalized_student_sequence, last_scheduled_student_sequence
        FROM discussion_assessment_cursors
        WHERE id=1
        """
    ).fetchone()
    conn.close()
    assert batch == ("failed", None, None)
    assert cursor == (8, 0)
