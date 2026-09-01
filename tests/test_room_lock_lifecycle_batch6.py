# -*- coding: utf-8 -*-
"""Batch 6 regression coverage for room lease lifecycle and 423 recovery."""

from datetime import datetime, timedelta
from pathlib import Path
import uuid

from tests.helpers import seed_running_session


ROOT = Path(__file__).resolve().parents[1]


def _time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _strategy_run(db, seeded, status="PENDING"):
    cutoff_sequence = int(
        db.query_one(
            "SELECT COUNT(*) AS c FROM intervention_runs WHERE group_id=?",
            (seeded["group_id"],),
        )["c"]
        or 0
    ) + 1
    return db.execute(
        """
        INSERT INTO intervention_runs(
            group_id, session_id, task_id, cutoff_sequence,
            status, agent_type,
            trigger_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"],
            seeded["session_id"],
            seeded["task_id"],
            cutoff_sequence,
            status,
            "strategy",
            "auto_state",
            db.now_str(),
        ),
    )


def _pipeline_run(
    db,
    seeded,
    *,
    start_sequence,
    cutoff_sequence,
    stage2_status="PENDING",
    final_status="PENDING_STAGE2",
):
    stage2_succeeded = stage2_status == "SUCCEEDED"
    return db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid,
            group_id, session_id, session_no, task_id,
            trigger_source, trigger_priority,
            input_start_sequence, input_end_sequence,
            input_cutoff_student_sequence,
            stage1_status, coarse_decision, coarse_state_code,
            coarse_should_escalate,
            stage2_status, canonical_sub_state_code,
            sub_state_evidence_message_ids_json, should_intervene,
            stage3_status, publish_status, final_status,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            seeded["group_id"],
            seeded["session_id"],
            seeded["session_no"],
            seeded["task_id"],
            "student_message",
            6,
            start_sequence,
            cutoff_sequence,
            cutoff_sequence,
            "SUCCEEDED",
            "ESCALATE",
            "POSSIBLE_BLOCKED",
            1,
            stage2_status,
            "confusion" if stage2_succeeded else None,
            "[1]" if stage2_succeeded else "[]",
            1 if stage2_succeeded else None,
            "PENDING" if stage2_succeeded else None,
            "NOT_READY",
            final_status,
            db.now_str(),
            db.now_str(),
        ),
    )


def _room(db, group_id):
    return dict(
        db.query_one(
            """
            SELECT state, lock_token, lock_expires_at,
                   active_intervention_run_id
              FROM groups
             WHERE id=?
            """,
            (group_id,),
        )
    )


def _attach_authoritative_batch(
    db,
    seeded,
    pipeline_id,
    *,
    start_sequence,
    cutoff_sequence,
    status,
):
    db.execute(
        """
        UPDATE experiment_sessions
           SET strategy_agent_enabled=1, agent_intervention_enabled=1
         WHERE id=?
        """,
        (seeded["session_id"],),
    )
    db.execute(
        "UPDATE learning_tasks SET agent_intervention_enabled=1 WHERE id=?",
        (seeded["task_id"],),
    )
    discussion_id = db.execute(
        """
        INSERT INTO group_session_discussions(
            session_id, group_id, status, started_at, created_at, updated_at
        ) VALUES(?,?,'running',?,?,?)
        """,
        (
            seeded["session_id"],
            seeded["group_id"],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )
    batch_id = db.execute(
        """
        INSERT INTO state_assessment_batches(
            group_id, session_id, session_no, task_id, discussion_id,
            candidate_start_sequence, candidate_end_sequence,
            trigger_type, window_key, status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seeded["group_id"],
            seeded["session_id"],
            seeded["session_no"],
            seeded["task_id"],
            discussion_id,
            start_sequence,
            cutoff_sequence,
            "rule_high_risk",
            f"room-lock-{uuid.uuid4()}",
            status,
            db.now_str(),
            db.now_str(),
        ),
    )
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET discussion_id=?, assessment_batch_id=?
         WHERE id=?
        """,
        (discussion_id, batch_id, pipeline_id),
    )
    return {"discussion_id": discussion_id, "batch_id": batch_id}


def test_expired_lease_recovery_unlocks_and_terminalizes_owner(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=601, member_count=1)
    run_id = _strategy_run(db, seeded, status="GENERATING")

    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    token = RoomLeaseService.acquire(
        seeded["group_id"],
        run_id,
        lock_seconds=-1,
    )
    assert token
    assert RoomLeaseService.recover_expired(seeded["group_id"]) is True
    assert _room(db, seeded["group_id"]) == {
        "state": "OPEN",
        "lock_token": None,
        "lock_expires_at": None,
        "active_intervention_run_id": None,
    }

    run = db.query_one(
        """
        SELECT status, decision, failure_reason, skip_reason, completed_at
          FROM intervention_runs
         WHERE id=?
        """,
        (run_id,),
    )
    assert dict(run) == {
        "status": "EXPIRED",
        "decision": "EXPIRED",
        "failure_reason": "room_lease_expired",
        "skip_reason": "room_lease_expired",
        "completed_at": run["completed_at"],
    }
    assert run["completed_at"]


def test_active_lease_and_token_mismatch_cannot_be_released(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=602, member_count=1)
    run_id = _strategy_run(db, seeded, status="LOCKED")

    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    token = RoomLeaseService.acquire(
        seeded["group_id"],
        run_id,
        lock_seconds=60,
    )
    assert token
    assert RoomLeaseService.release_expired(seeded["group_id"], token) is False
    assert RoomLeaseService.release(seeded["group_id"], "wrong-token") is False
    assert _room(db, seeded["group_id"])["lock_token"] == token
    assert RoomLeaseService.release(seeded["group_id"], token) is True
    assert _room(db, seeded["group_id"])["state"] == "OPEN"


def test_new_strategy_reclaims_orphaned_expired_lease_before_acquire(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=603, member_count=1)
    expired_run_id = _strategy_run(db, seeded, status="LOCKED")
    next_run_id = _strategy_run(db, seeded, status="PENDING")

    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    expired_token = RoomLeaseService.acquire(
        seeded["group_id"],
        expired_run_id,
        lock_seconds=-1,
    )
    assert expired_token
    next_token = RoomLeaseService.acquire(
        seeded["group_id"],
        next_run_id,
        lock_seconds=60,
    )
    assert next_token and next_token != expired_token

    previous = db.query_one(
        "SELECT status, failure_reason FROM intervention_runs WHERE id=?",
        (expired_run_id,),
    )
    assert dict(previous) == {
        "status": "EXPIRED",
        "failure_reason": "room_lease_expired",
    }
    current = _room(db, seeded["group_id"])
    assert current["state"] == "AI_INTERVENING"
    assert current["active_intervention_run_id"] == next_run_id
    assert current["lock_token"] == next_token
    assert RoomLeaseService.release(seeded["group_id"], next_token) is True


def test_consumerless_pipeline_cannot_adopt_legacy_preliminary_lease(
    db_and_app,
    monkeypatch,
):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=607, member_count=1)
    owner_id = _pipeline_run(
        db,
        seeded,
        start_sequence=10,
        cutoff_sequence=12,
        final_status="LOCKED",
    )
    target_id = _pipeline_run(
        db,
        seeded,
        start_sequence=10,
        cutoff_sequence=16,
        final_status="WAITING_FOR_LOCK",
    )

    import config
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    owner_token = RoomLeaseService.acquire(
        seeded["group_id"],
        -owner_id,
        lock_seconds=60,
    )
    assert owner_token
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET room_lock_token=?, room_lock_acquired_at=?
         WHERE id=?
        """,
        (owner_token, db.now_str(), owner_id),
    )

    claimed = RoomLeaseService.claim_strategy_pipeline(
        target_id,
        lock_seconds=120,
    )

    assert claimed["acquired"] is False
    assert claimed["reason"] == "authoritative_batch_missing"
    room = _room(db, seeded["group_id"])
    assert room["state"] == "AI_INTERVENING"
    assert room["lock_token"] == owner_token
    assert room["active_intervention_run_id"] == -owner_id
    target = db.query_one(
        """
        SELECT room_lock_token, room_lock_acquired_at,
               final_status, skip_reason
          FROM strategy_pipeline_runs
         WHERE id=?
        """,
        (target_id,),
    )
    assert target["room_lock_token"] is None
    assert target["room_lock_acquired_at"] is None
    assert target["final_status"] == "WAITING_FOR_LOCK"
    assert RoomLeaseService.release(seeded["group_id"], owner_token) is True


def test_state_batch_pipeline_renews_its_own_lease_for_stage3(
    db_and_app,
    monkeypatch,
):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=608, member_count=1)
    pipeline_id = _pipeline_run(
        db,
        seeded,
        start_sequence=20,
        cutoff_sequence=24,
        stage2_status="SUCCEEDED",
        final_status="PENDING_STAGE3",
    )
    _attach_authoritative_batch(
        db,
        seeded,
        pipeline_id,
        start_sequence=20,
        cutoff_sequence=24,
        status="succeeded",
    )

    import config
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    token = RoomLeaseService.acquire(
        seeded["group_id"],
        -pipeline_id,
        lock_seconds=10,
    )
    assert token
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET room_lock_token=?, room_lock_acquired_at=?
         WHERE id=?
        """,
        (token, db.now_str(), pipeline_id),
    )
    short_expiry = _time(datetime.now() + timedelta(seconds=10))
    db.execute(
        "UPDATE groups SET lock_expires_at=? WHERE id=?",
        (short_expiry, seeded["group_id"]),
    )

    renewed = RoomLeaseService.claim_strategy_pipeline(
        pipeline_id,
        lock_seconds=120,
    )

    assert renewed["acquired"] is True
    assert renewed["reason"] == "room_lease_renewed"
    assert renewed["renewed"] is True
    assert renewed["lock_token"] == token
    room = _room(db, seeded["group_id"])
    assert room["lock_expires_at"] > short_expiry
    assert room["active_intervention_run_id"] == -pipeline_id
    assert RoomLeaseService.release_expired(
        seeded["group_id"],
        token,
    ) is False
    assert RoomLeaseService.release(seeded["group_id"], token) is True


def test_state_batch_does_not_steal_stage2_complete_pipeline_lease(
    db_and_app,
    monkeypatch,
):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=609, member_count=1)
    owner_id = _pipeline_run(
        db,
        seeded,
        start_sequence=30,
        cutoff_sequence=32,
        stage2_status="SUCCEEDED",
        final_status="PENDING_STAGE3",
    )
    target_id = _pipeline_run(
        db,
        seeded,
        start_sequence=30,
        cutoff_sequence=36,
        final_status="WAITING_FOR_LOCK",
    )
    authoritative = _attach_authoritative_batch(
        db,
        seeded,
        target_id,
        start_sequence=30,
        cutoff_sequence=36,
        status="running",
    )
    db.execute(
        "UPDATE strategy_pipeline_runs SET discussion_id=? WHERE id=?",
        (authoritative["discussion_id"], owner_id),
    )

    import config
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    monkeypatch.setattr(config, "HUEY_IMMEDIATE", True)
    owner_token = RoomLeaseService.acquire(
        seeded["group_id"],
        -owner_id,
        lock_seconds=60,
    )
    assert owner_token
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET room_lock_token=?, room_lock_acquired_at=?
         WHERE id=?
        """,
        (owner_token, db.now_str(), owner_id),
    )

    blocked = RoomLeaseService.claim_strategy_pipeline(
        target_id,
        lock_seconds=120,
    )

    assert blocked["acquired"] is False
    assert blocked["reason"] == "room_locked_by_other_pipeline"
    assert blocked["lock_owner_run_id"] == owner_id
    room = _room(db, seeded["group_id"])
    assert room["lock_token"] == owner_token
    assert room["active_intervention_run_id"] == -owner_id
    target = db.query_one(
        "SELECT final_status, room_lock_token FROM strategy_pipeline_runs WHERE id=?",
        (target_id,),
    )
    assert target["final_status"] == "WAITING_FOR_LOCK"
    assert target["room_lock_token"] is None
    assert RoomLeaseService.release(
        seeded["group_id"],
        owner_token,
    ) is True


def test_423_identifies_owner_and_student_sync_recovers_expired_lock(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=604, member_count=1)
    run_id = _strategy_run(db, seeded, status="GENERATING")

    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )
    from routes.api import (
        _ai_intervening_locked_response,
        _student_sync_room_events,
    )

    token = RoomLeaseService.acquire(
        seeded["group_id"],
        run_id,
        lock_seconds=60,
    )
    assert token

    response, status = _ai_intervening_locked_response(seeded["group_id"])
    payload = response.get_json()
    assert status == 423
    assert payload["code"] == "ROOM_AI_INTERVENING"
    assert payload["ai_lock"]["lock_owner_type"] == "strategy"
    assert payload["ai_lock"]["lock_owner_run_id"] == run_id
    assert payload["ai_lock"]["lock_owner_status"] == "GENERATING"
    assert payload["ai_lock"]["lock_reason"] == "auto_state"
    assert payload["ai_lock"]["lock_expires_at"]

    db.execute(
        "UPDATE groups SET lock_expires_at=? WHERE id=?",
        (
            _time(datetime.now() - timedelta(seconds=1)),
            seeded["group_id"],
        ),
    )
    sync = _student_sync_room_events(
        seeded["group_id"],
        after_sequence=0,
        limit=10,
    )
    assert sync["room"]["state"] == "OPEN"
    assert sync["ai_lock"]["locked"] is False
    assert sync["ai_lock"]["reason"] is None
    assert db.query_one(
        "SELECT status FROM intervention_runs WHERE id=?",
        (run_id,),
    )["status"] == "EXPIRED"


def test_orphaned_student_help_lock_is_attributed_and_recovered(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=605, member_count=1)

    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    token = RoomLeaseService.acquire(
        seeded["group_id"],
        -31,
        lock_seconds=-1,
    )
    assert token
    info = RoomLeaseService.get_lock_info(seeded["group_id"])
    assert info["lock_owner_type"] == "student_help"
    assert info["lock_owner_run_id"] == 31
    assert info["lock_owner_status"] == "orphaned"
    assert info["lock_reason"] == "student_help_request"
    assert RoomLeaseService.recover_expired(seeded["group_id"]) is True
    assert _room(db, seeded["group_id"])["state"] == "OPEN"


def test_expired_three_stage_pipeline_lock_is_attributed_and_terminalized(
    db_and_app,
):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=606, member_count=1)
    pipeline_id = db.execute(
        """
        INSERT INTO strategy_pipeline_runs(
            run_uuid, group_id, session_id, task_id, trigger_source,
            stage1_status, stage2_status, publish_status, final_status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            seeded["group_id"],
            seeded["session_id"],
            seeded["task_id"],
            "student_message",
            "SUCCEEDED",
            "PENDING",
            "NOT_READY",
            "LOCKED",
        ),
    )

    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    token = RoomLeaseService.acquire(
        seeded["group_id"],
        -pipeline_id,
        lock_seconds=-1,
    )
    assert token
    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET room_lock_token=?, room_lock_acquired_at=?
         WHERE id=?
        """,
        (token, db.now_str(), pipeline_id),
    )

    info = RoomLeaseService.get_lock_info(seeded["group_id"])
    assert info["lock_owner_type"] == "strategy_pipeline"
    assert info["lock_owner_run_id"] == pipeline_id
    assert info["lock_owner_status"] == "LOCKED"
    assert info["lock_reason"] == "student_message"
    assert RoomLeaseService.recover_expired(seeded["group_id"]) is True

    pipeline = db.query_one(
        """
        SELECT final_status, publish_status, skip_reason, failure_code,
               failure_detail, room_lock_released_at
          FROM strategy_pipeline_runs
         WHERE id=?
        """,
        (pipeline_id,),
    )
    assert pipeline["final_status"] == "FAILED"
    assert pipeline["publish_status"] == "FAILED"
    assert pipeline["skip_reason"] == "ROOM_LEASE_EXPIRED"
    assert pipeline["failure_code"] == "ROOM_LEASE_EXPIRED"
    assert pipeline["failure_detail"]
    assert pipeline["room_lock_released_at"]
    assert _room(db, seeded["group_id"])["state"] == "OPEN"


def test_student_sync_open_payload_drives_frontend_composer_unlock():
    source = (ROOT / "routes" / "collab_pages.py").read_text(encoding="utf-8")
    assert "setComposerLocked(normalizeAiLock(data));" in source
    assert "const locked = !!normalized.locked;" in source
    assert "input.readOnly = locked;" in source
    assert "sendBtn.disabled = locked || sendingMessage;" in source
    assert "scheduleNextStudentSync" in source
