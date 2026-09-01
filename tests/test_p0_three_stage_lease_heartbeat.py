# -*- coding: utf-8 -*-
"""P0 batch 3 coverage for bounded authoritative room-lease renewal."""

from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import uuid

from tests.helpers import seed_running_session
from tests.test_room_lock_lifecycle_batch6 import (
    _attach_authoritative_batch,
    _pipeline_run,
    _room,
)


ROOT = Path(__file__).resolve().parents[1]


def _active_lease(db, seeded, *, lock_seconds=30):
    pipeline_id = _pipeline_run(
        db,
        seeded,
        start_sequence=1,
        cutoff_sequence=2,
    )
    scope = _attach_authoritative_batch(
        db,
        seeded,
        pipeline_id,
        start_sequence=1,
        cutoff_sequence=2,
        status="running",
    )
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    lease = RoomLeaseService.claim_strategy_pipeline(
        pipeline_id,
        lock_seconds=lock_seconds,
    )
    assert lease["acquired"] is True
    pipeline = db.query_one(
        "SELECT room_lock_acquired_at FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    acquired_at = datetime.fromisoformat(pipeline["room_lock_acquired_at"])
    return pipeline_id, scope, lease, acquired_at


def test_active_stage2_and_stage3_renew_but_terminal_pipeline_cannot(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=631, member_count=1)
    pipeline_id, _scope, lease, acquired_at = _active_lease(db, seeded)
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET stage2_status='RUNNING', final_status='ASSESSING'
         WHERE id=?
        """,
        (pipeline_id,),
    )
    stage2 = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        now=acquired_at + timedelta(seconds=10),
    )
    assert stage2["renewed"] is True

    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET stage2_status='SUCCEEDED', stage3_status='RUNNING',
               canonical_sub_state_code='confusion',
               sub_state_evidence_message_ids_json='[1]',
               should_intervene=1, final_status='GENERATING'
         WHERE id=?
        """,
        (pipeline_id,),
    )
    stage3 = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        now=acquired_at + timedelta(seconds=20),
    )
    assert stage3["renewed"] is True

    db.execute(
        "UPDATE strategy_pipeline_runs SET final_status='FAILED' WHERE id=?",
        (pipeline_id,),
    )
    terminal = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        now=acquired_at + timedelta(seconds=21),
    )
    assert terminal["renewed"] is False
    assert terminal["reason"] == "pipeline_terminal"


def test_heartbeat_pulse_renews_near_initial_expiry(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=632, member_count=1)
    pipeline_id, _scope, lease, acquired_at = _active_lease(
        db,
        seeded,
        lock_seconds=30,
    )
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    db.execute(
        """
        UPDATE strategy_pipeline_runs
           SET stage2_status='RUNNING', final_status='ASSESSING'
         WHERE id=?
        """,
        (pipeline_id,),
    )
    heartbeat = RoomLeaseService.strategy_pipeline_heartbeat(
        pipeline_id,
        lease["lock_token"],
    )
    renewed = heartbeat.pulse(now=acquired_at + timedelta(seconds=29))
    assert renewed["renewed"] is True
    assert datetime.fromisoformat(renewed["lock_expires_at"]) > datetime.fromisoformat(
        lease["lock_expires_at"]
    )


def test_renewal_rejects_wrong_token_owner_and_max_total(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=633, member_count=1)
    pipeline_id, _scope, lease, acquired_at = _active_lease(db, seeded)
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    wrong_token = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        "not-the-current-token",
        now=acquired_at + timedelta(seconds=1),
    )
    assert wrong_token["renewed"] is False
    assert wrong_token["reason"] == "room_lease_token_mismatch"

    db.execute(
        "UPDATE groups SET active_intervention_run_id=? WHERE id=?",
        (-(pipeline_id + 1), seeded["group_id"]),
    )
    wrong_owner = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        now=acquired_at + timedelta(seconds=2),
    )
    assert wrong_owner["renewed"] is False
    assert wrong_owner["reason"] == "room_lease_owner_mismatch"

    db.execute(
        "UPDATE groups SET active_intervention_run_id=? WHERE id=?",
        (-pipeline_id, seeded["group_id"]),
    )
    maxed = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        max_total_seconds=30,
        now=acquired_at + timedelta(seconds=30),
    )
    assert maxed["renewed"] is False
    assert maxed["reason"] == "room_lease_max_total_exceeded"


def test_old_expiry_task_cannot_release_renewed_same_token(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=634, member_count=1)
    pipeline_id, _scope, lease, acquired_at = _active_lease(
        db,
        seeded,
        lock_seconds=5,
    )
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    renewed = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        lock_seconds=75,
        now=acquired_at + timedelta(seconds=4),
    )
    assert renewed["renewed"] is True
    assert RoomLeaseService.release_expired(
        seeded["group_id"],
        lease["lock_token"],
    ) is False
    room = _room(db, seeded["group_id"])
    assert room["state"] == "AI_INTERVENING"
    assert room["lock_token"] == lease["lock_token"]


def test_stopped_heartbeat_allows_bounded_ttl_recovery(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=635, member_count=1)
    pipeline_id, _scope, lease, _acquired_at = _active_lease(db, seeded)
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    heartbeat = RoomLeaseService.strategy_pipeline_heartbeat(
        pipeline_id,
        lease["lock_token"],
    )
    heartbeat.stop()
    db.execute(
        "UPDATE groups SET lock_expires_at=? WHERE id=?",
        (
            (datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            seeded["group_id"],
        ),
    )
    assert RoomLeaseService.release_expired(
        seeded["group_id"],
        lease["lock_token"],
    ) is True
    assert _room(db, seeded["group_id"])["state"] == "OPEN"
    pipeline = db.query_one(
        "SELECT final_status, failure_code FROM strategy_pipeline_runs WHERE id=?",
        (pipeline_id,),
    )
    assert pipeline["final_status"] == "FAILED"
    assert pipeline["failure_code"] == "ROOM_LEASE_EXPIRED"


def test_renew_audit_keeps_ttl_heartbeat_cooldown_and_spacing_distinct(db_and_app):
    db, _app_module, _client = db_and_app
    seeded = seed_running_session(db, session_no=636, member_count=1)
    pipeline_id, _scope, lease, acquired_at = _active_lease(db, seeded)
    from services.intervention_pipeline_v2.room_lease_service import (
        RoomLeaseService,
    )

    renewed = RoomLeaseService.renew_strategy_pipeline(
        pipeline_id,
        lease["lock_token"],
        lock_seconds=31,
        max_total_seconds=181,
        now=acquired_at + timedelta(seconds=1),
    )
    assert renewed["renewed"] is True
    event = db.query_one(
        """
        SELECT details_json
          FROM strategy_pipeline_latency_events
         WHERE pipeline_run_id=? AND event='room_lock_renewed'
         ORDER BY id DESC LIMIT 1
        """,
        (pipeline_id,),
    )
    details = json.loads(event["details_json"])
    assert details["lock_ttl_seconds"] == 31
    assert details["lock_heartbeat_seconds"] == 20
    assert details["lock_max_total_seconds"] == 181
    assert details["strategy_cooldown_seconds"] == 120
    assert details["emotion_strategy_spacing_seconds"] == 60


def _load_config(monkeypatch, values):
    keys = {
        "INTERVENTION_V2_LOCK_SECONDS",
        "THREE_STAGE_ROOM_LOCK_SECONDS",
        "THREE_STAGE_LOCK_INITIAL_TTL_SECONDS",
        "THREE_STAGE_LOCK_HEARTBEAT_SECONDS",
        "THREE_STAGE_LOCK_MAX_TOTAL_SECONDS",
        "INTERVENTION_V2_COOLDOWN_SECONDS",
        "STRATEGY_COOLDOWN_SECONDS",
        "AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS",
        "EMOTION_STRATEGY_SPACING_SECONDS",
    }
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    name = f"batch3_config_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_lock_config_names_override_legacy_fallbacks(monkeypatch):
    legacy = _load_config(
        monkeypatch,
        {
            "THREE_STAGE_ROOM_LOCK_SECONDS": 91,
            "INTERVENTION_V2_COOLDOWN_SECONDS": 131,
            "AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS": 71,
        },
    )
    assert legacy.THREE_STAGE_LOCK_INITIAL_TTL_SECONDS == 91
    assert legacy.THREE_STAGE_ROOM_LOCK_SECONDS == 91
    assert legacy.STRATEGY_COOLDOWN_SECONDS == 131
    assert legacy.EMOTION_STRATEGY_SPACING_SECONDS == 71

    current = _load_config(
        monkeypatch,
        {
            "THREE_STAGE_ROOM_LOCK_SECONDS": 91,
            "THREE_STAGE_LOCK_INITIAL_TTL_SECONDS": 81,
            "THREE_STAGE_LOCK_HEARTBEAT_SECONDS": 22,
            "THREE_STAGE_LOCK_MAX_TOTAL_SECONDS": 190,
            "INTERVENTION_V2_COOLDOWN_SECONDS": 131,
            "STRATEGY_COOLDOWN_SECONDS": 141,
            "AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS": 71,
            "EMOTION_STRATEGY_SPACING_SECONDS": 73,
        },
    )
    assert current.THREE_STAGE_LOCK_INITIAL_TTL_SECONDS == 81
    assert current.THREE_STAGE_ROOM_LOCK_SECONDS == 81
    assert current.THREE_STAGE_LOCK_HEARTBEAT_SECONDS == 22
    assert current.THREE_STAGE_LOCK_MAX_TOTAL_SECONDS == 190
    assert current.STRATEGY_COOLDOWN_SECONDS == 141
    assert current.EMOTION_STRATEGY_SPACING_SECONDS == 73
