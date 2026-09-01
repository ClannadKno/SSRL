# -*- coding: utf-8 -*-
"""Small data builders shared by the curated test suite."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any


def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def login_with_key(client, login_key: str) -> dict[str, str]:
    response = client.post("/login", data={"login_key": login_key}, follow_redirects=False)
    assert response.status_code == 302

    from urllib.parse import parse_qs, urlparse

    tab_token = parse_qs(urlparse(response.headers["Location"]).query)["tab_token"][0]
    return {"X-Tab-Token": tab_token}


def create_group(db, *, name: str = "Test Group", code: str | None = None) -> int:
    now = db.now_str()
    return db.execute(
        "INSERT INTO groups(name, group_code, condition, state, created_at) VALUES(?,?,?,?,?)",
        (name, code or f"G{datetime.now().strftime('%H%M%S%f')}", "experiment", "OPEN", now),
    )


def create_student(db, group_id: int, *, index: int = 1, username_prefix: str = "student") -> tuple[int, str]:
    from werkzeug.security import generate_password_hash

    now = db.now_str()
    participant_code = f"P{group_id}{index}"
    user_id = db.execute(
        """
        INSERT INTO users(username, password_hash, real_name, participant_code, role, created_at)
        VALUES(?,?,?,?,?,?)
        """,
        (
            f"{username_prefix}_{group_id}_{index}",
            "x",
            f"Student {index}",
            participant_code,
            "student",
            now,
        ),
    )
    db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (group_id, user_id))
    login_key = f"TEST-{group_id}-{index}-{user_id}-KEY"
    db.execute(
        """
        INSERT INTO experiment_participants(
            participant_code, login_key_hash, group_no, member_no, group_id,
            user_id, display_name, is_active, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            participant_code,
            generate_password_hash(login_key),
            group_id,
            index,
            group_id,
            user_id,
            f"G{group_id}-M{index}",
            1,
            now,
        ),
    )
    return user_id, login_key


def seed_running_session(
    db,
    *,
    session_no: int = 1,
    member_count: int = 1,
    limit_minutes: int = 10,
    session_role: str = "discussion",
) -> dict[str, Any]:
    now = db.now_str()
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE status='running'")
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        (f"Task {session_no}", "Discuss together", limit_minutes, now),
    )
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (session_no, session_role, task_id, "running", now, limit_minutes, now, now),
    )
    db.set_setting("current_session_id", str(session_id))
    db.set_setting("current_session_no", str(session_no))
    db.set_setting("current_task_id", str(task_id))
    group_id = create_group(db, name=f"Group {session_no}", code=f"G{session_no}")
    students = [
        create_student(db, group_id, index=i + 1, username_prefix=f"s{session_no}")
        for i in range(member_count)
    ]
    return {
        "session_id": session_id,
        "session_no": session_no,
        "task_id": task_id,
        "group_id": group_id,
        "students": students,
    }


def attach_state_assessment_to_monitor(
    db,
    monitor_run_id: int,
    *,
    group_id: int,
    state_code: str,
    confidence: float = 0.86,
    cutoff_sequence: int | None = None,
    session_id: int | None = None,
    task_id: int | None = None,
    session_no: int | None = None,
    should_intervene: bool = True,
    assessment_status: str = "confirmed",
) -> int:
    monitor_row = db.query_one(
        "SELECT cutoff_sequence, rule_result_json FROM monitor_runs WHERE id=?",
        (monitor_run_id,),
    )
    monitor = dict(monitor_row) if monitor_row else {}
    if cutoff_sequence is None and monitor:
        cutoff_sequence = monitor["cutoff_sequence"]
    evidence_sequences = [int(cutoff_sequence)] if cutoff_sequence is not None else []
    assessment_id = db.execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no,
            fused_state_code, assessment_status, confidence, should_intervene,
            evidence_summary, fusion_json, rule_assessment_json, context_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            session_id,
            task_id,
            session_no,
            state_code,
            assessment_status,
            confidence,
            1 if should_intervene else 0,
            "state assessment from test fixture",
            json.dumps(
                {
                    "decision_source": "test_fixture",
                    "evidence_sequences": evidence_sequences,
                },
                ensure_ascii=False,
            ),
            json.dumps({"evidence_sequences": evidence_sequences}, ensure_ascii=False),
            json.dumps({"evidence_sequences": evidence_sequences}, ensure_ascii=False),
            db.now_str(),
        ),
    )
    payload = json.loads((monitor or {}).get("rule_result_json") or "{}")
    payload.setdefault("winning_state_code", state_code)
    payload.setdefault("winning_score", confidence)
    payload["monitor_audit"] = {
        **(payload.get("monitor_audit") or {}),
        "state_assessment_id": assessment_id,
    }
    db.execute(
        """
        UPDATE monitor_runs
        SET rule_result_json=?, state_assessment_id=?, session_id=?, task_id=?
        WHERE id=?
        """,
        (json.dumps(payload, ensure_ascii=False), assessment_id, session_id, task_id, monitor_run_id),
    )
    return assessment_id


def expire_group_discussion(db, session_id: int, group_id: int, user_id: int) -> int:
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    runtime = enter_group_discussion_stage(session_id, group_id, user_id)
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE group_session_discussions SET status='running', deadline=?, updated_at=? WHERE id=?",
        (past, db.now_str(), runtime["id"]),
    )
    return runtime["id"]


def create_questionnaire(db, code: str, timing: str) -> tuple[int, int]:
    qid = db.create_questionnaire(
        {
            "code": code,
            "title": code,
            "timing": timing,
            "scale_max": 5,
            "active": True,
        },
        items=[
            {
                "item_code": f"{code}_Q1",
                "prompt_text": "Question 1",
                "dimension_label": "Dim",
                "sort_order": 1,
            }
        ],
    )
    item = db.query_one(
        "SELECT id FROM questionnaire_items WHERE questionnaire_id=? ORDER BY id LIMIT 1",
        (qid,),
    )
    return qid, item["id"]
