# -*- coding: utf-8 -*-
import json

import pytest


def _task(db, suffix):
    return db.execute(
        "INSERT INTO learning_tasks(title, question, created_at) VALUES(?,?,?)",
        (f"Agent mode {suffix}", "Discuss", db.now_str()),
    )


def _group(db, suffix):
    return db.execute(
        "INSERT INTO groups(name, group_code, state, created_at) VALUES(?,?,?,?)",
        (f"Mode group {suffix}", f"AM{suffix}", "OPEN", db.now_str()),
    )


@pytest.mark.parametrize(
    ("mode", "strategy", "emotion", "state_monitoring"),
    (
        ("none", False, False, False),
        ("strategy", True, False, True),
        ("emotion", False, True, True),
    ),
)
def test_all_agent_modes_persist_and_gate_runtime(
    db_and_app, mode, strategy, emotion, state_monitoring
):
    db, _app, _client = db_and_app
    task_id = _task(db, mode)
    group_id = _group(db, mode)

    from services.session_lifecycle import check_agent_allowed
    from services.teacher_session_service import create_session, start_session

    draft = create_session(
        operator_id=1,
        session_no={"none": 701, "strategy": 702, "emotion": 703}[mode],
        task_id=task_id,
        agent_mode=mode,
    )
    assert draft["agent_mode"] == mode
    assert draft["strategy_agent_enabled"] is strategy
    assert draft["emotion_agent_enabled"] is emotion

    config = db.get_session_agent_config(session_id=draft["id"])
    assert config["agent_mode"] == mode
    assert config["state_monitoring_enabled"] is state_monitoring
    assert config["strategy_agent_enabled"] is strategy
    assert config["emotion_agent_enabled"] is emotion

    running = start_session(session_id=draft["id"], operator_id=1)
    common = {
        "session_id": running["id"],
        "task_id": task_id,
        "session_no": running["session_no"],
    }
    strategy_allowed, _ = check_agent_allowed(
        group_id, agent_type="strategy", **common
    )
    emotion_allowed, _ = check_agent_allowed(
        group_id, agent_type="emotion", **common
    )
    assert strategy_allowed is strategy
    assert emotion_allowed is emotion


def test_invalid_enum_and_legacy_dual_open_are_rejected(db_and_app):
    db, _app, _client = db_and_app
    task_id = _task(db, "invalid")

    from services.teacher_session_service import create_session

    with pytest.raises(ValueError, match="agent_mode"):
        create_session(
            operator_id=1,
            session_no=704,
            task_id=task_id,
            agent_mode="both",
        )
    with pytest.raises(ValueError, match="INVALID_AGENT_CONFIGURATION"):
        create_session(
            operator_id=1,
            session_no=705,
            task_id=task_id,
            strategy_agent_enabled=True,
            emotion_agent_enabled=True,
        )


def test_historical_dual_open_is_not_scheduled_or_started(db_and_app):
    db, _app, _client = db_and_app
    task_id = _task(db, "legacy")

    from services.teacher_session_service import create_session, start_session

    draft = create_session(
        operator_id=1, session_no=706, task_id=task_id, agent_mode="strategy"
    )
    db.execute(
        """UPDATE experiment_sessions
           SET strategy_agent_enabled=1, emotion_agent_enabled=1
           WHERE id=?""",
        (draft["id"],),
    )
    stored = db.query_one(
        "SELECT agent_mode FROM experiment_sessions WHERE id=?", (draft["id"],)
    )
    assert stored["agent_mode"] is None

    config = db.get_session_agent_config(session_id=draft["id"])
    assert config["configuration_error"] == "INVALID_AGENT_CONFIGURATION"
    assert config["strategy_agent_enabled"] is False
    assert config["emotion_agent_enabled"] is False
    with pytest.raises(ValueError, match="INVALID_AGENT_CONFIGURATION"):
        start_session(session_id=draft["id"], operator_id=1)


def test_agent_mode_change_audit_contains_old_new_and_timestamp(db_and_app):
    db, _app, _client = db_and_app
    task_id = _task(db, "audit")

    from services.teacher_session_service import (
        create_session,
        update_session_agent_config,
    )

    draft = create_session(
        operator_id=1, session_no=707, task_id=task_id, agent_mode="none"
    )
    updated = update_session_agent_config(
        session_id=draft["id"], operator_id=1, agent_mode="emotion"
    )
    assert updated["agent_mode"] == "emotion"
    row = db.query_one(
        """SELECT after_value FROM audit_logs
           WHERE target_type='experiment_session' AND target_id=?
             AND action_type='session.update_agent_config'
           ORDER BY id DESC LIMIT 1""",
        (draft["id"],),
    )
    audit = json.loads(row["after_value"])
    assert audit["old_agent_mode"] == "none"
    assert audit["new_agent_mode"] == "emotion"
    assert audit["changed_at"]


def test_teacher_api_rejects_dual_open_and_accepts_agent_mode(
    db_and_app, teacher_login
):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    task_id = _task(db, "api")

    rejected = client.post(
        "/api/teacher/session/create",
        json={
            "session_no": 708,
            "task_id": task_id,
            "strategy_agent_enabled": True,
            "emotion_agent_enabled": True,
        },
        headers=headers,
    )
    assert rejected.status_code == 400
    assert "INVALID_AGENT_CONFIGURATION" in rejected.get_json()["error"]

    accepted = client.post(
        "/api/teacher/session/create",
        json={"session_no": 709, "task_id": task_id, "agent_mode": "emotion"},
        headers=headers,
    )
    assert accepted.status_code == 201
    payload = accepted.get_json()["session"]
    assert payload["agent_mode"] == "emotion"
    assert payload["strategy_agent_enabled"] is False
    assert payload["emotion_agent_enabled"] is True


def test_teacher_ui_uses_one_radio_group_for_agent_mode(db_and_app, teacher_login):
    _db, _app, _client = db_and_app
    client, headers = teacher_login
    response = client.get("/teacher/session/control", headers=headers)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.count('name="createAgentMode"') == 3
    assert "createStrategyAgentEnabled" not in html
    assert "createEmotionAgentEnabled" not in html
