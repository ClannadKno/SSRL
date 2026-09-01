# -*- coding: utf-8 -*-
"""Batch 11 pre-experiment simulation acceptance.

These tests intentionally read the existing load-test dialogue scripts without
editing their conversation content.  Precise sub-state acceptance uses the
normal isolated pytest database so no formal business data or real student
accounts are touched.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.helpers import create_group, create_student, seed_running_session
from tests.test_three_stage_batch6_decision_gate import _add_message, _publish, _ready_pipeline


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_4P = ROOT / "load-test" / "config" / "trigger-states-4p.js"
SCRIPT_4P_60M = ROOT / "load-test" / "config" / "trigger-states-4p-60m.js"
SCRIPT_ALL_GROUPS_60M = ROOT / "load-test" / "config" / "trigger-states-all-groups-60m.js"

INTERVENTION_CANONICAL_STATES = (
    "interpersonal_conflict",
    "confusion",
    "frustration",
    "burnout",
    "off_topic_unregulated",
    "perfunctory_detachment",
    "individual_marginalization",
    "psychological_safety_risk",
    "high_intensity_overload",
)

OI_CANONICAL_STATES = (
    "deep_thinking",
    "execution_progress",
    "constructive_conflict",
    "off_topic_self_regulated",
)

NON_INTERVENTION_CANONICAL_STATES = (
    "standard",
    "deep_thinking",
    "execution_progress",
    "constructive_conflict",
    "off_topic_self_regulated",
    "stage_achievement",
    "unknown_sub_state",
)

REQUIRED_BATCH11_SCENARIOS = {
    "positive_standard": "standard",
    "deep_thinking": "deep_thinking",
    "execution_progress": "execution_progress",
    "constructive_conflict": "constructive_conflict",
    "interpersonal_conflict": "interpersonal_conflict",
    "confusion": "confusion",
    "frustration": "frustration",
    "burnout": "burnout",
    "off_topic_self_regulated": "off_topic_self_regulated",
    "off_topic_unregulated": "off_topic_unregulated",
    "perfunctory_detachment": "perfunctory_detachment",
    "individual_marginalization": "individual_marginalization",
    "active_help": "student_help",
    "negative_silence": "perfunctory_detachment",
    "post_intervention_recovered": "standard",
    "post_intervention_persistent_risk": "interpersonal_conflict",
    "session_end_cancel": "session_end",
}


def _load_scenario(module_path: Path) -> dict:
    relative = module_path.relative_to(ROOT).as_posix()
    command = (
        f"const scenario=require('./{relative}');"
        "process.stdout.write(JSON.stringify({"
        "name: scenario.name,"
        "totalStudents: scenario.totalStudents,"
        "groupCount: scenario.groupCount,"
        "membersPerGroup: scenario.membersPerGroup,"
        "minReadyStudents: scenario.minReadyStudents,"
        "discussionDurationMs: scenario.discussionDurationMs,"
        "scriptedDiscussion: scenario.scriptedDiscussion"
        "}));"
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
    return json.loads(result.stdout)


@pytest.fixture
def batch11_db(test_env, monkeypatch):
    db = importlib.import_module("db")
    db.ensure_database_ready()
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "AUTO_INTERVENTION_V2_ENABLED", True)
    return db


def _time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _acceptance_scope(db, *, session_no: int, member_count: int = 4) -> dict:
    scope = seed_running_session(db, session_no=session_no, member_count=member_count, limit_minutes=60)
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
    db.execute("UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?", (scope["task_id"],))
    db.execute(
        "UPDATE groups SET auto_intervention_enabled=1, state='OPEN' WHERE id=?",
        (scope["group_id"],),
    )
    now = datetime.now().replace(microsecond=0)
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, deadline,
            expected_student_count, ready_student_count,
            created_at, updated_at
        ) VALUES(?,?,'running',?,?,?,?,?,?)
        """,
        (
            scope["session_id"],
            scope["group_id"],
            _time(now - timedelta(minutes=10)),
            _time(now + timedelta(minutes=50)),
            member_count,
            member_count,
            _time(now),
            _time(now),
        ),
    )
    scope["discussion_id"] = discussion_id
    scope["student_id"] = scope["students"][0][0]
    return scope


def _strategy_text(strategy_id: str) -> str:
    if strategy_id == "ER-002":
        return "卡住很正常，先把最难的一点说出来再选一个能验证的小步骤。"
    return "先把当前证据说清楚，再选一个大家都能执行的小步骤。"


def _insert_oi_pipeline(db, scope, *, canonical: str, cutoff: int) -> int:
    schema = importlib.import_module("services.three_stage_schema")
    route = schema.route_for_sub_state(canonical)
    inhibition_id = route["inhibition_strategy_id"]
    assert inhibition_id
    for sequence, content in (
        (1, "我们正在认真比对现有证据。"),
        (2, "暂时先想一下怎么整理，不需要马上提示。"),
        (3, "我会把思路拉回任务要求。"),
    ):
        if not db.query_one(
            "SELECT id FROM messages WHERE group_id=? AND sequence=?",
            (scope["group_id"], sequence),
        ):
            _add_message(db, scope, sequence, content)
    now = db.now_str()
    lock_token = f"batch11-oi-{scope['group_id']}-{canonical}-{cutoff}"
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
            inhibition_reason,
            stage3_status, stage3_started_at, stage3_completed_at,
            strategy_candidate_ids_json, selected_strategy_id,
            selected_strategy_name, selected_strategy_type,
            supporting_strategy_ids_json, strategy_selection_reason,
            strategy_library_version, strategy_library_hash,
            strategy_model_name, strategy_model_version, strategy_prompt_version,
            strategy_raw_response_json,
            room_lock_token, room_lock_acquired_at,
            publish_status, final_status,
            idempotency_key, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"batch11-oi-{scope['group_id']}-{canonical}-{cutoff}",
            scope["group_id"],
            scope["session_id"],
            scope["session_no"],
            scope["discussion_id"],
            scope["task_id"],
            "student_message",
            7,
            1,
            cutoff,
            cutoff,
            "SUCCEEDED",
            "SUCCEEDED",
            canonical,
            canonical,
            "[]",
            0.92,
            "batch11 OI suppression fixture",
            1,
            cutoff,
            "[2,3]",
            json.dumps([{"canonical_sub_state": canonical, "evidence_message_ids": [2, 3]}], ensure_ascii=False),
            1 if canonical == "off_topic_self_regulated" else 0,
            0,
            inhibition_id,
            "OI route suppresses student-visible strategy intervention",
            "SUCCEEDED",
            now,
            now,
            json.dumps(route["candidate_strategy_ids"], ensure_ascii=False),
            inhibition_id,
            "观察抑制",
            "观察抑制",
            "[]",
            "non-intervention route",
            route["strategy_library_version"],
            route["strategy_library_hash"],
            "batch11-local",
            "batch11-local",
            "stage3.v1",
            "{}",
            lock_token,
            now,
            "NOT_READY",
            "PENDING_DECISION_GATE",
            f"batch11:oi:{scope['group_id']}:{canonical}:{cutoff}",
            now,
            now,
        ),
    )
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
    return pipeline_id


def test_existing_four_person_and_sixty_minute_scripts_are_located_and_unchanged_shape():
    script = _load_scenario(SCRIPT_4P)
    script_60 = _load_scenario(SCRIPT_4P_60M)
    all_groups_60 = _load_scenario(SCRIPT_ALL_GROUPS_60M)

    assert script["totalStudents"] == 4
    assert script["membersPerGroup"] == 4
    assert script["minReadyStudents"] == 4
    assert script["discussionDurationMs"] == 30 * 60 * 1000
    messages = script["scriptedDiscussion"]["messages"]
    assert len(messages) == 29
    assert {row["studentId"].rsplit("M", 1)[-1] for row in messages} == {"1", "2", "3", "4"}
    assert set(script["scriptedDiscussion"]["expectedFinalStates"]) == {
        "positive_collaboration",
        "negative_silence",
        "conflict_tension",
        "blocked_frustration",
        "task_detached",
        "unknown",
    }

    messages_60 = script_60["scriptedDiscussion"]["messages"]
    assert script_60["discussionDurationMs"] == 60 * 60 * 1000
    assert [row["text"] for row in messages_60] == [row["text"] for row in messages]
    assert [
        row.get("afterSeconds") for row in messages_60
    ] == [
        (row.get("afterSeconds") * 2 if row.get("afterSeconds") is not None else None)
        for row in messages
    ]

    assert all_groups_60["name"] == "trigger-states-all-groups-60m"
    assert all_groups_60["totalStudents"] == 60
    assert all_groups_60["groupCount"] == 15
    assert all_groups_60["membersPerGroup"] == 4
    assert all_groups_60["scriptedDiscussion"]["repeatForEachGroup"] is True


def test_batch11_required_precise_state_acceptance_matrix_is_covered_by_routes():
    schema = importlib.import_module("services.three_stage_schema")
    library = importlib.import_module("services.three_stage_strategy_library")
    issues = library.validate_strategy_library()
    assert issues == []

    covered_canonical = {
        state
        for state in REQUIRED_BATCH11_SCENARIOS.values()
        if state in schema.CANONICAL_SUB_STATE_CODES
    }
    assert set(schema.CANONICAL_SUB_STATE_CODES).issuperset(covered_canonical)
    assert set(INTERVENTION_CANONICAL_STATES).issubset(set(schema.CANONICAL_SUB_STATE_CODES))
    assert set(NON_INTERVENTION_CANONICAL_STATES).issubset(set(schema.CANONICAL_SUB_STATE_CODES))

    for canonical in schema.CANONICAL_SUB_STATE_CODES:
        route = schema.route_for_sub_state(canonical)
        assert route["canonical_sub_state"] == canonical
        if canonical in INTERVENTION_CANONICAL_STATES:
            assert route["should_intervene"] is True
            assert route["candidate_strategy_ids"]
            assert not set(route["candidate_strategy_ids"]) & set(schema.OI_STRATEGY_IDS)
        elif canonical in OI_CANONICAL_STATES:
            assert route["should_intervene"] is False
            assert route["inhibition_strategy_id"] in schema.OI_STRATEGY_IDS
            assert route["candidate_strategy_ids"] == [route["inhibition_strategy_id"]]
        else:
            assert route["should_intervene"] is False


def test_intervention_precise_states_publish_with_complete_strategy_ids(batch11_db):
    db = batch11_db
    schema = importlib.import_module("services.three_stage_schema")
    published = {}
    for offset, canonical in enumerate(INTERVENTION_CANONICAL_STATES, start=1):
        scope = _acceptance_scope(db, session_no=1110 + offset, member_count=4)
        route = schema.route_for_sub_state(canonical)
        selected = route["candidate_strategy_ids"][0]
        pipeline_id = _ready_pipeline(
            db,
            scope,
            canonical=canonical,
            selected_strategy_id=selected,
            candidates=route["candidate_strategy_ids"],
            cutoff=3,
            priority=2 if canonical in {"interpersonal_conflict", "psychological_safety_risk"} else 4,
            text=_strategy_text(selected),
        )

        result = _publish(pipeline_id)

        assert result["published"] is True, (canonical, result)
        row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
        message = db.query_one("SELECT * FROM messages WHERE id=?", (row["published_message_id"],))
        run = db.query_one("SELECT * FROM intervention_runs WHERE strategy_pipeline_run_id=?", (pipeline_id,))
        assert row["canonical_sub_state_code"] == canonical
        assert row["selected_strategy_id"] == selected
        assert row["publish_status"] == "PUBLISHED"
        assert message["strategy_id"] == selected
        assert run["selected_strategy_id"] == selected
        assert run["canonical_sub_state_code"] == canonical
        published[canonical] = selected

    assert set(published) == set(INTERVENTION_CANONICAL_STATES)


def test_oi_precise_states_never_publish_and_release_room_locks(batch11_db):
    db = batch11_db
    scope = _acceptance_scope(db, session_no=1120, member_count=4)

    for offset, canonical in enumerate(OI_CANONICAL_STATES, start=1):
        pipeline_id = _insert_oi_pipeline(db, scope, canonical=canonical, cutoff=10 + offset)

        result = _publish(pipeline_id)

        assert result["published"] is False
        assert result["reason"] == "should_intervene_false"
        row = db.query_one("SELECT * FROM strategy_pipeline_runs WHERE id=?", (pipeline_id,))
        assert row["publish_status"] == "SKIPPED"
        assert row["final_status"] == "SUPPRESSED"
        assert row["selected_strategy_id"].startswith("OI-")
        assert row["published_message_id"] is None
        group = db.query_one(
            "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
            (scope["group_id"],),
        )
        assert dict(group) == {"state": "OPEN", "lock_token": None, "active_intervention_run_id": None}

    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 0


def test_same_group_serial_and_cross_group_parallel_acceptance(batch11_db):
    db = batch11_db
    scope = _acceptance_scope(db, session_no=1130, member_count=4)
    owner = _ready_pipeline(db, scope, cutoff=3)
    contender = _ready_pipeline(
        db,
        scope,
        canonical="off_topic_unregulated",
        selected_strategy_id="ER-003",
        candidates=["ER-003"],
        cutoff=4,
        lock=False,
        text=_strategy_text("ER-003"),
    )
    db.execute(
        "UPDATE strategy_pipeline_runs SET room_lock_token=? WHERE id=?",
        ("contender-without-lock", contender),
    )

    blocked = _publish(contender)

    assert blocked["published"] is False
    assert blocked["reason"] == "lock_token_mismatch"
    room = db.query_one(
        "SELECT state, lock_token, active_intervention_run_id FROM groups WHERE id=?",
        (scope["group_id"],),
    )
    assert room["state"] == "AI_INTERVENING"
    assert room["active_intervention_run_id"] == -owner

    other_group = create_group(db, name="Batch11 parallel group", code="B11-PARALLEL")
    other_student, _key = create_student(db, other_group, index=1, username_prefix="b11_parallel")
    db.execute("UPDATE groups SET auto_intervention_enabled=1 WHERE id=?", (other_group,))
    other_discussion = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, deadline,
            expected_student_count, ready_student_count,
            created_at, updated_at
        ) VALUES(?,?,'running',?,?,?,?,?,?)
        """,
        (
            scope["session_id"],
            other_group,
            db.now_str(),
            _time(datetime.now() + timedelta(minutes=45)),
            1,
            1,
            db.now_str(),
            db.now_str(),
        ),
    )
    other_scope = {
        **scope,
        "group_id": other_group,
        "student_id": other_student,
        "discussion_id": other_discussion,
    }
    parallel = _ready_pipeline(db, other_scope, cutoff=3)

    owner_result = _publish(owner)
    parallel_result = _publish(parallel)

    assert owner_result["published"] is True
    assert parallel_result["published"] is True
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (scope["group_id"],),
    )["c"] == 1
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE group_id=? AND role='agent'",
        (other_group,),
    )["c"] == 1
