# -*- coding: utf-8 -*-
"""Read-only guards for teacher-facing state APIs."""

import json

from tests.helpers import seed_running_session


WATCHED_TABLES = [
    "messages",
    "monitor_runs",
    "state_assessments",
    "group_states",
    "collaboration_state_segments",
    "help_requests",
    "intervention_runs",
    "intervention_logs",
]


def _counts(db):
    return {
        table: db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in WATCHED_TABLES
    }


def _seed_v2_state(db):
    ctx = seed_running_session(db, session_no=701, member_count=2)
    from services.group_discussion_runtime_service import (
        enter_group_discussion_stage,
    )

    discussion = enter_group_discussion_stage(
        ctx["session_id"],
        ctx["group_id"],
        ctx["students"][0][0],
    )
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no, discussion_id,
            rule_state_code, llm_state_code, fused_state_code, fused_state_label,
            assessment_status, confidence, risk_level, risk_label, should_intervene,
            evidence_summary, fusion_json, rule_assessment_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            ctx["session_id"],
            ctx["task_id"],
            ctx["session_no"],
            discussion["id"],
            "task_detached",
            "task_detached",
            "task_detached",
            "任务脱离",
            "confirmed",
            0.86,
            2,
            "medium",
            1,
            "persisted V2 state",
            json.dumps({"decision_source": "state_llm"}, ensure_ascii=False),
            json.dumps({"winning_state_code": "task_detached"}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    group_state_id = db.execute(
        """
        INSERT INTO group_states(
            group_id, state_code, state_label, risk_level, risk_label,
            evidence, task_id, session_no, session_id, discussion_id, state_score,
            state_assessment_id, assessment_status, confirmed_windows,
            confirmation_status, llm_state_code, fusion_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            "task_detached",
            "任务脱离",
            2,
            "medium",
            "persisted V2 group state",
            ctx["task_id"],
            ctx["session_no"],
            ctx["session_id"],
            discussion["id"],
            0.86,
            assessment_id,
            "confirmed",
            2,
            "confirmed",
            "task_detached",
            json.dumps({"decision_source": "state_llm"}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    return ctx, assessment_id, group_state_id


def test_group_state_get_reads_v2_without_legacy_detection(db_and_app, teacher_login, monkeypatch):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    ctx, assessment_id, group_state_id = _seed_v2_state(db)

    import routes.api as api_routes

    monkeypatch.setattr(api_routes, "analyze_group", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy analyze called")))
    before = _counts(db)

    response = client.get(f"/api/group/{ctx['group_id']}/state", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["read_only"] is True
    assert payload["source"] == "canonical_state_read_model"
    assert payload["state_code"] == "unclassified"
    assert payload["state_label"] == "未分类"
    assert payload["final_sub_state_code"] is None
    assert payload["coarse_state_code"] == "task_detached"
    assert payload["group_state_id"] == group_state_id
    assert payload["state_assessment_id"] == assessment_id
    assert (
        payload["latest_coarse_assessment"]["fused_state_code"]
        == "task_detached"
    )
    assert _counts(db) == before


def test_teacher_group_refreshes_do_not_grow_state_tables(db_and_app, teacher_login, monkeypatch):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    ctx, _assessment_id, _group_state_id = _seed_v2_state(db)

    import routes.api as api_routes

    monkeypatch.setattr(api_routes, "analyze_group", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy analyze called")))
    monkeypatch.setattr(api_routes, "latest_group_state", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy latest_group_state called")))
    before = _counts(db)

    for _ in range(5):
        response = client.get("/api/teacher/groups?all=1", headers=headers)
        assert response.status_code == 200
        group_payload = next(g for g in response.get_json()["groups"] if g["group_id"] == ctx["group_id"])
        assert group_payload["state_label"] == "未分类"
        assert group_payload["final_sub_state_code"] is None
        assert group_payload["coarse_state_code"] == "task_detached"
        assert group_payload["confidence"] is None

    assert _counts(db) == before


def test_teacher_group_rollup_preserves_post_intervention_observing(
    db_and_app,
    teacher_login,
    monkeypatch,
):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    ctx, _assessment_id, _group_state_id = _seed_v2_state(db)

    import services.teacher_emotion_trend_service as trend_service

    monkeypatch.setattr(
        trend_service,
        "get_current_canonical_state",
        lambda group_id, session_id=None: {
            "group_id": group_id,
            "requested_session_id": session_id,
            "resolved_session_id": session_id,
            "final_sub_state_code": None,
            "final_sub_state_label": None,
            "state_code": "observing",
            "state_label": "观察中",
            "assessment_status": "observing",
            "assignment_source": "post_intervention_observation",
            "inferred": False,
            "confidence": None,
            "source": "canonical_state_read_model",
            "read_only": True,
        },
    )

    response = client.get("/api/teacher/groups?all=1", headers=headers)

    assert response.status_code == 200
    group_payload = next(
        group
        for group in response.get_json()["groups"]
        if group["group_id"] == ctx["group_id"]
    )
    assert group_payload["final_sub_state_code"] is None
    assert group_payload["state_code"] == "observing"
    assert group_payload["assessment_status"] == "observing"
    assert (
        group_payload["assignment_source"]
        == "post_intervention_observation"
    )


def test_legacy_group_analyze_post_is_disabled_by_default(db_and_app, teacher_login, monkeypatch):
    db, _app, _client = db_and_app
    client, headers = teacher_login
    ctx, _assessment_id, _group_state_id = _seed_v2_state(db)

    import routes.api as api_routes

    monkeypatch.setattr(api_routes, "analyze_group", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy analyze called")))
    before = _counts(db)

    response = client.post(f"/api/group/{ctx['group_id']}/agent/analyze", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["code"] == "LEGACY_ANALYZE_DISABLED"
    assert _counts(db) == before
