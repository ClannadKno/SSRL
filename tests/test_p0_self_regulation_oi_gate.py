# -*- coding: utf-8 -*-
"""P0 batch 5 coverage for fresh self-regulation and OI suppression."""

from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

import pytest

from tests.helpers import seed_running_session


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@pytest.fixture
def gate_env(test_env):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    scope = seed_running_session(db, session_no=9505, member_count=1)
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
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1 WHERE id=?",
        (scope["group_id"],),
    )
    scope["discussion_id"] = db.execute(
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
    scope["student_id"] = scope["students"][0][0]
    return db, scope


def _add_student_message(db, scope, sequence: int, content: str) -> int:
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
            "student",
            "student",
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            db.now_str(),
        ),
    )


def _prepare_authoritative_pipeline(
    db,
    scope,
    *,
    candidate_start: int = 1,
    candidate_end: int = 2,
) -> tuple[dict, int]:
    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, session_no, task_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            trigger_type, request_priority, window_key, status,
            started_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'running',?,?,?)
        """,
        (
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["task_id"],
            scope["discussion_id"],
            candidate_start,
            candidate_end,
            "rule_high_risk",
            400,
            f"batch5:{uuid.uuid4()}",
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, trigger_priority, assessment_batch_id,
            input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence,
            stage1_status, coarse_decision, coarse_state_code,
            coarse_should_escalate, stage2_status, publish_status, final_status,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "rule_high_risk",
            400,
            batch_id,
            candidate_start,
            candidate_end,
            candidate_end,
            "SUCCEEDED",
            "ESCALATE",
            "POSSIBLE_BLOCKED",
            1,
            "PENDING",
            "NOT_READY",
            "PENDING_STAGE2",
            f"batch5:pipeline:{uuid.uuid4()}",
            db.now_str(),
            db.now_str(),
        ),
    )
    from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
    from services.three_stage_stage2 import Stage2PipelineService

    claim = RoomLeaseService.claim_strategy_pipeline(pipeline_id)
    assert claim["acquired"] is True
    batch = dict(
        db.query_one("SELECT * FROM state_assessment_batches WHERE id=?", (batch_id,))
    )
    started = Stage2PipelineService.mark_started(
        batch=batch,
        pipeline_run_id=pipeline_id,
    )
    assert started["started"] is True
    return batch, pipeline_id


def _stage2_payload(
    canonical: str,
    *,
    evidence: list[int],
    detected_self_regulation: bool,
    should_intervene: bool,
    inhibition_strategy_id: str | None = None,
    start_sequence: int = 1,
    end_sequence: int = 2,
) -> dict:
    from services.three_stage_schema import route_for_sub_state

    route = route_for_sub_state(canonical)
    segment = {
        "raw_sub_state": canonical,
        "canonical_sub_state": canonical,
        "secondary_tags": [],
        "confidence": 0.94,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "evidence_message_ids": list(evidence),
        "reason": "fresh student evidence",
        "is_active_at_window_end": True,
        "detected_self_regulation": detected_self_regulation,
    }
    return {
        "schema_version": "stage2.v1",
        "analysis_scope": {
            "candidate_start_sequence": start_sequence,
            "candidate_end_sequence": end_sequence,
            "input_cutoff_student_sequence": end_sequence,
        },
        "segments": [segment],
        "active_sub_state": dict(segment),
        "active_segment_index": 0,
        "should_intervene": should_intervene,
        "inhibition": {
            "is_inhibited": bool(inhibition_strategy_id),
            "strategy_id": inhibition_strategy_id,
            "reason": "fresh OI evidence" if inhibition_strategy_id else None,
        },
        "candidate_strategy_ids": list(route["candidate_strategy_ids"]),
        "decision_reason": "batch 5 deterministic gate",
    }


def _save_active_segment(db, batch: dict, payload: dict) -> dict:
    from services.state_assessment_batch_service import StateAssessmentBatchService

    active = payload["active_sub_state"]
    saved = StateAssessmentBatchService.save_successful_segments(
        batch["id"],
        [
            {
                "state": "positive_collaboration",
                "raw_sub_state_code": active["raw_sub_state"],
                "canonical_sub_state_code": active["canonical_sub_state"],
                "secondary_tags": [],
                "start_sequence": active["start_sequence"],
                "end_sequence": active["end_sequence"],
                "confidence": active["confidence"],
                "evidence_sequences": active["evidence_message_ids"],
                "segment_order": 0,
                "source": "llm",
                "assessment_status": "confirmed",
                "is_active_at_batch_end": True,
                "trigger_type": batch["trigger_type"],
            }
        ],
        parsed_response=payload,
    )
    return saved


@pytest.mark.parametrize(
    ("canonical", "oi_strategy_id", "student_text"),
    [
        ("constructive_conflict", "OI-001", "我们先停止互相指责，按共同标准比较两个方案。"),
        ("deep_thinking", "OI-002", "我先独立核算预算，再回来说明比较结果。"),
        ("off_topic_self_regulated", "OI-003", "刚才跑题了，现在拉回项目组合和预算。"),
        ("execution_progress", "OI-004", "项目组合已定，我现在完成预算表。"),
    ],
)
def test_fresh_oi_forces_suppression_releases_lock_and_persists_audit(
    gate_env,
    canonical,
    oi_strategy_id,
    student_text,
):
    db, scope = gate_env
    _add_student_message(db, scope, 1, "先确认当前任务。")
    evidence_message_id = _add_student_message(db, scope, 2, student_text)
    batch, pipeline_id = _prepare_authoritative_pipeline(db, scope)
    payload = _stage2_payload(
        canonical,
        evidence=[2],
        detected_self_regulation=False,
        should_intervene=True,
        inhibition_strategy_id=oi_strategy_id,
    )
    saved = _save_active_segment(db, batch, payload)

    from services.three_stage_stage2 import Stage2PipelineService

    result = Stage2PipelineService.persist_success(
        batch=saved["batch"],
        stage2_result=payload,
        saved_segments=saved["segments"],
    )

    assert result["should_intervene"] is False
    assert result["should_enter_stage3"] is False
    assert result["lock_released"] is True
    assert result["suppression_type"] == "OI"
    assert result["suppression_strategy_id"] == oi_strategy_id
    row = db.query_one(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["stage3_status"] == "SKIPPED"
    assert row["final_status"] == "SUPPRESSED"
    assert row["skip_reason"] == f"OI_SUPPRESSED:{oi_strategy_id}"
    assert row["suppression_type"] == "OI"
    assert row["suppression_strategy_id"] == oi_strategy_id
    assert json.loads(row["suppression_evidence_message_ids_json"]) == [
        evidence_message_id
    ]
    assert row["suppression_source_batch_id"] == batch["id"]
    assert row["suppression_source_segment_id"] == saved["segments"][0]["id"]
    assert row["suppression_decision_at"]
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE role='agent'"
    )["c"] == 0


@pytest.mark.parametrize(
    "student_text",
    [
        "我们已经重新聚焦，先完成证据比较。",
        "我们先完成项目组合和预算，再检查风险。",
    ],
)
def test_fresh_self_regulation_overrides_confusion_route_and_never_enters_stage3(
    gate_env,
    student_text,
):
    db, scope = gate_env
    _add_student_message(db, scope, 1, "刚才思路有点乱。")
    evidence_message_id = _add_student_message(db, scope, 2, student_text)
    batch, pipeline_id = _prepare_authoritative_pipeline(db, scope)
    payload = _stage2_payload(
        "confusion",
        evidence=[2],
        detected_self_regulation=True,
        should_intervene=True,
    )
    saved = _save_active_segment(db, batch, payload)

    from services.three_stage_stage2 import Stage2PipelineService

    result = Stage2PipelineService.persist_success(
        batch=saved["batch"],
        stage2_result=payload,
        saved_segments=saved["segments"],
    )

    assert result["should_intervene"] is False
    assert result["should_enter_stage3"] is False
    assert result["fresh_detected_self_regulation"] is True
    assert result["suppression_type"] == "SELF_REGULATION"
    row = db.query_one(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["detected_self_regulation"] == 1
    assert row["fresh_detected_self_regulation"] == 1
    assert row["should_intervene"] == 0
    assert row["stage3_status"] == "SKIPPED"
    assert row["skip_reason"] == "SELF_REGULATION_SUPPRESSED"
    assert row["suppression_type"] == "SELF_REGULATION"
    assert row["suppression_strategy_id"] is None
    assert json.loads(row["suppression_evidence_message_ids_json"]) == [
        evidence_message_id
    ]
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE role='agent'"
    )["c"] == 0


@pytest.mark.parametrize(
    ("candidate_start", "evidence", "active_start", "expected_detail"),
    [
        (1, [], 1, "empty_evidence"),
        (2, [1], 1, "evidence_outside_current_candidate_window"),
    ],
)
def test_empty_or_stale_self_regulation_evidence_fails_closed_without_suppression(
    gate_env,
    candidate_start,
    evidence,
    active_start,
    expected_detail,
):
    db, scope = gate_env
    _add_student_message(db, scope, 1, "历史上曾经说过先自己整理。")
    _add_student_message(db, scope, 2, "当前仍然困惑，需要重新评估。")
    batch, pipeline_id = _prepare_authoritative_pipeline(
        db,
        scope,
        candidate_start=candidate_start,
    )
    payload = _stage2_payload(
        "confusion",
        evidence=evidence,
        detected_self_regulation=True,
        should_intervene=True,
        start_sequence=active_start,
    )

    from services.three_stage_stage2 import Stage2PipelineService

    result = Stage2PipelineService.persist_success(
        batch=batch,
        stage2_result=payload,
        saved_segments=[],
    )

    assert result["final_status"] == "FAILED"
    assert result["skip_reason"] == "INVALID_SUPPRESSION_EVIDENCE"
    assert result["suppression_type"] is None
    assert result["should_enter_stage3"] is False
    assert result["lock_released"] is True
    row = db.query_one(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert row["failure_code"] == "INVALID_SUPPRESSION_EVIDENCE"
    assert row["failure_detail"] == expected_detail
    assert row["suppression_type"] is None
    assert row["suppression_decision_at"] is None
    assert row["detected_self_regulation"] == 0


def test_parser_normalizes_self_regulation_before_static_confusion_route():
    detector = importlib.import_module(
        "services.discussion_pipeline_v2.llm_state_detector"
    )
    payload = _stage2_payload(
        "confusion",
        evidence=[2],
        detected_self_regulation=True,
        should_intervene=True,
    )

    parsed = detector.parse_llm_json_content(payload, [1, 2])

    assert parsed["valid"] is True
    assert parsed["data"]["fresh_detected_self_regulation"] is True
    assert parsed["data"]["should_intervene"] is False
    assert parsed["data"]["candidate_strategy_ids"] == []


def test_publish_gate_blocks_inconsistent_ready_row_marked_fresh_self_regulation(
    gate_env,
):
    db, scope = gate_env
    from tests.test_three_stage_batch6_decision_gate import _ready_pipeline

    pipeline_id = _ready_pipeline(db, scope)
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET fresh_detected_self_regulation=1,
               suppression_type='SELF_REGULATION',
               suppression_decision_reason='SELF_REGULATION_SUPPRESSED',
               suppression_decision_at=?
         WHERE id=?
        """,
        (db.now_str(), pipeline_id),
    )

    from services.three_stage_publish import ThreeStageInterventionPublisher

    result = ThreeStageInterventionPublisher.publish_ready_pipeline(pipeline_id)

    assert result["published"] is False
    assert result["reason"] == "SELF_REGULATION_SUPPRESSED"
    assert result["final_status"] == "SUPPRESSED"
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE role='agent'"
    )["c"] == 0
    group = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert dict(group) == {
        "state": "OPEN",
        "lock_token": None,
        "active_intervention_run_id": None,
    }


def test_batch5_acceptance_queries_have_zero_detected_publish_violations(gate_env):
    db, _scope = gate_env
    metrics = {
        "self_regulation_detected_and_published": db.query_one(
            """
            SELECT COUNT(*) AS c FROM strategy_pipeline_runs
            WHERE COALESCE(fresh_detected_self_regulation, 0)=1
              AND publish_status='PUBLISHED'
            """
        )["c"],
        "OI_detected_and_published": db.query_one(
            """
            SELECT COUNT(*) AS c FROM strategy_pipeline_runs
            WHERE suppression_strategy_id IN ('OI-001','OI-002','OI-003','OI-004')
              AND publish_status='PUBLISHED'
            """
        )["c"],
        "empty_evidence_self_reg_suppression": db.query_one(
            """
            SELECT COUNT(*) AS c FROM strategy_pipeline_runs
            WHERE suppression_type='SELF_REGULATION'
              AND suppression_evidence_message_ids_json='[]'
            """
        )["c"],
        "stale_evidence_self_reg_suppression": db.query_one(
            """
            SELECT COUNT(*) AS c FROM strategy_pipeline_runs
            WHERE suppression_type='SELF_REGULATION'
              AND final_status='STALE'
            """
        )["c"],
    }
    assert metrics == {
        "self_regulation_detected_and_published": 0,
        "OI_detected_and_published": 0,
        "empty_evidence_self_reg_suppression": 0,
        "stale_evidence_self_reg_suppression": 0,
    }


def test_live_database_copy_migrates_twice_without_touching_live_evidence(tmp_path):
    root = Path(__file__).resolve().parents[1]
    live_db = root / "ssrl_esp.db"
    live_log = root / "logs" / "nqy_20260729_144616.log"
    live_db_hash = _sha256(live_db)
    live_log_hash = _sha256(live_log) if live_log.exists() else None
    copied_db = tmp_path / "batch5_migration_copy.db"
    shutil.copy2(live_db, copied_db)

    migrations = importlib.import_module("migrations")
    conn = sqlite3.connect(str(copied_db))
    try:
        migrations.run_pending_migrations(conn)
        conn.commit()
        migrations.run_pending_migrations(conn)
        conn.commit()
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(strategy_pipeline_runs)")
        }
        assert {
            "fresh_detected_self_regulation",
            "suppression_type",
            "suppression_strategy_id",
            "suppression_evidence_message_ids_json",
            "suppression_source_batch_id",
            "suppression_source_segment_id",
            "suppression_decision_reason",
            "suppression_decision_at",
        } <= columns
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list('strategy_pipeline_runs')")
        }
        assert "idx_strategy_pipeline_runs_suppression" in indexes
    finally:
        conn.close()

    live_conn = sqlite3.connect(
        f"file:{live_db.as_posix()}?mode=ro",
        uri=True,
    )
    try:
        assert live_conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        live_conn.close()
    assert _sha256(live_db) == live_db_hash
    if live_log_hash is not None:
        assert _sha256(live_log) == live_log_hash
