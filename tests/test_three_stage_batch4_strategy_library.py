# -*- coding: utf-8 -*-
"""Batch 4 coverage for the Markdown-backed three-stage strategy library."""

from __future__ import annotations

import json
import importlib

from services.three_stage_schema import CANONICAL_SUB_STATE_CODES, OI_STRATEGY_IDS
from services.three_stage_strategy_library import (
    get_strategy_definition,
    load_strategy_library,
    rank_candidate_strategies,
    route_for_sub_state,
    validate_strategy_library,
)


def _stage2_payload(canonical: str, candidates: list[str]) -> dict:
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
        "should_intervene": True,
        "inhibition": {"is_inhibited": False, "strategy_id": None, "reason": None},
        "candidate_strategy_ids": candidates,
        "decision_reason": "validated precise sub-state",
    }


def test_markdown_strategy_library_is_authoritative_and_unique():
    library = load_strategy_library()
    ids = [item.strategy_id for item in library.definitions]

    assert library.version == "v2.2"
    assert len(library.definitions) == 28
    assert len(set(ids)) == 28
    assert get_strategy_definition("ER-001").strategy_name == "冲突认知重评"
    assert get_strategy_definition("OI-003").strategy_name == "群体自修复保护"
    assert validate_strategy_library() == []


def test_precise_sub_state_routes_cover_all_canonical_states():
    library = load_strategy_library()
    by_id = library.by_id

    for canonical in CANONICAL_SUB_STATE_CODES:
        route = route_for_sub_state(canonical)
        assert route["strategy_library_version"] == "v2.2"
        assert route["strategy_library_hash"] == library.library_hash
        assert route["canonical_sub_state"] == canonical
        assert set(route["candidate_strategy_ids"]).issubset(by_id)
        for strategy_id in route["candidate_strategy_ids"]:
            assert canonical in by_id[strategy_id].applicable_sub_states


def test_oi_routes_are_exclusive_and_never_intervene():
    for canonical, expected_strategy in {
        "constructive_conflict": "OI-001",
        "deep_thinking": "OI-002",
        "off_topic_self_regulated": "OI-003",
        "execution_progress": "OI-004",
    }.items():
        route = route_for_sub_state(canonical)
        strategy = get_strategy_definition(expected_strategy)

        assert route["candidate_strategy_ids"] == [expected_strategy]
        assert route["primary_strategy_ids"] == [expected_strategy]
        assert route["backup_strategy_ids"] == []
        assert route["should_intervene"] is False
        assert route["inhibition_strategy_id"] == expected_strategy
        assert strategy.strategy_id in OI_STRATEGY_IDS
        assert strategy.should_intervene is False
        assert strategy.is_exclusive is True


def test_history_repetition_penalty_preserves_primary_backup_metadata():
    route = route_for_sub_state("frustration")
    ranked = rank_candidate_strategies(
        "frustration",
        usage_counts={"ER-002": 3},
        last_strategy_ids=["ER-002", "ER-002"],
    )

    assert route["primary_strategy_ids"] == ["ER-002"]
    assert route["backup_strategy_ids"] == ["EE-003", "SS-006", "EA-007"]
    assert ranked[-1]["strategy_id"] == "ER-002"
    assert ranked[-1]["route_role"] == "primary"
    assert ranked[0]["route_role"] == "backup"


def test_state_detector_rejects_model_invented_strategy_id():
    from services.discussion_pipeline_v2.llm_state_detector import parse_llm_json_content

    parsed = parse_llm_json_content(
        _stage2_payload("interpersonal_conflict", ["ER-999"]),
        [1, 2],
    )

    assert parsed["valid"] is False
    assert parsed["error_type"] == "invalid_candidate_strategy_id"


def test_route_manifest_adds_optional_support_and_new_candidates():
    standard = route_for_sub_state("standard")
    frustration = route_for_sub_state("frustration")
    conflict = route_for_sub_state("interpersonal_conflict")

    assert standard["route_mode"] == "OPTIONAL_SUPPORT"
    assert standard["candidate_strategy_ids"] == ["SS-001", "EA-006"]
    assert standard["should_intervene"] is False
    assert "EA-007" in frustration["candidate_strategy_ids"]
    assert "ER-007" in conflict["candidate_strategy_ids"]


def test_strategy_definitions_sync_is_idempotent(db_and_app):
    db, _app, _client = db_and_app
    library_service = importlib.import_module("services.three_stage_strategy_library")

    first = library_service.sync_strategy_definitions()
    second = library_service.sync_strategy_definitions()

    assert first["version"] == second["version"] == "v2.2"
    assert first["library_hash"] == second["library_hash"]
    rows = db.query_all(
        """
        SELECT strategy_id, strategy_name, applicable_sub_states_json,
               should_intervene, is_exclusive, version, content_hash
        FROM strategy_definitions
        WHERE version=?
        ORDER BY strategy_id
        """,
        ("v2.2",),
    )
    assert len(rows) == 28
    er002 = next(row for row in rows if row["strategy_id"] == "ER-002")
    assert er002["strategy_name"] == "任务难度重评"
    assert "frustration" in json.loads(er002["applicable_sub_states_json"])
    oi_rows = [row for row in rows if row["strategy_id"].startswith("OI-")]
    assert len(oi_rows) == 4
    assert all(row["should_intervene"] == 0 for row in oi_rows)
    assert all(row["is_exclusive"] == 1 for row in oi_rows)


def test_stage2_persistence_saves_strategy_library_version_and_hash(db_and_app):
    db, _app, _client = db_and_app
    from services.three_stage_stage2 import Stage2PipelineService
    from tests.helpers import seed_running_session

    context = seed_running_session(db, session_no=904, member_count=1)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (
            context["session_id"],
            context["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    context["discussion_id"] = discussion_id
    student_id = context["students"][0][0]
    for sequence in (1, 2):
        db.execute(
            """
            INSERT INTO messages(
                group_id, user_id, content, sequence, sender_type, role,
                session_id, session_no, task_id, discussion_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                context["group_id"],
                student_id,
                f"evidence {sequence}",
                sequence,
                "student",
                "student",
                context["session_id"],
                context["session_no"],
                context["task_id"],
                discussion_id,
                db.now_str(),
            ),
        )
    preliminary_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence, stage1_status, stage2_status,
            publish_status, final_status, idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "batch4-preliminary-stage1",
            context["group_id"],
            context["session_id"],
            context["session_no"],
            discussion_id,
            context["task_id"],
            "student_message",
            1,
            1,
            1,
            "SUCCEEDED",
            "PENDING",
            "NOT_READY",
            "PENDING_STAGE2",
            "batch4:preliminary:stage1",
            db.now_str(),
            db.now_str(),
        ),
    )
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, session_no, discussion_id, task_id,
            trigger_source, input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence, stage1_status, stage2_status,
            publish_status, final_status, idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "batch4-stage2-library",
            context["group_id"],
            context["session_id"],
            context["session_no"],
            discussion_id,
            context["task_id"],
            "message_count_periodic",
            1,
            2,
            2,
            "SUCCEEDED",
            "PENDING",
            "NOT_READY",
            "PENDING_STAGE2",
            "batch4:stage2:library",
            db.now_str(),
            db.now_str(),
        ),
    )
    payload = _stage2_payload("interpersonal_conflict", ["ER-001", "EE-001", "SS-004", "ER-007"])

    result = Stage2PipelineService.persist_success(
        batch={
            "group_id": context["group_id"],
            "session_id": context["session_id"],
            "session_no": context["session_no"],
            "discussion_id": discussion_id,
            "task_id": context["task_id"],
            "candidate_start_sequence": 1,
            "candidate_end_sequence": 2,
            "trigger_type": "message_count_periodic",
            "request_priority": 200,
        },
        stage2_result=payload,
        llm_meta={"model_name": "batch4-model", "prompt_version": "stage2.v1"},
        saved_segments=[],
    )

    assert result["pipeline_run_id"] == pipeline_id
    assert result["superseded_pipeline_ids"] == [preliminary_id]
    row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
    library = importlib.import_module(
        "services.three_stage_strategy_library"
    ).load_strategy_library()
    assert row["strategy_library_version"] == "v2.2"
    assert row["strategy_library_hash"] == library.library_hash
    assert json.loads(row["strategy_candidate_ids_json"]) == [
        "ER-001",
        "EE-001",
        "SS-004",
        "ER-007",
    ]
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM strategy_definitions WHERE version='v2.2'"
    )["c"] == 28
    preliminary = db.query_one(
        """
        SELECT final_status, publish_status, skip_reason, superseded_by_run_id
        FROM strategy_pipeline_runs WHERE id=?
        """,
        (preliminary_id,),
    )
    assert preliminary["final_status"] == "SUPERSEDED"
    assert preliminary["publish_status"] == "SKIPPED"
    assert preliminary["skip_reason"] == "SUPERSEDED_BY_STATE_BATCH"
    assert preliminary["superseded_by_run_id"] == pipeline_id
