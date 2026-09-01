# -*- coding: utf-8 -*-
import json

from tests.helpers import create_group, create_student


def _create_session(db, *, session_no=1, task_payload=None, full_task=True):
    now = db.now_str()
    if full_task:
        task_id = db.execute(
            """
            INSERT INTO learning_tasks(
                title, description, question, task_goal, output_requirement,
                key_concepts_json, expected_dimensions_json, task_payload_json,
                created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f"Task {session_no}",
                "Task description",
                "Task question",
                "Task goal",
                "Task output",
                json.dumps(["concept-a"]),
                json.dumps(["dimension-a"]),
                json.dumps(task_payload or {"evaluation_criteria": ["criterion-a"]}),
                now,
            ),
        )
    else:
        task_id = db.execute(
            "INSERT INTO learning_tasks(title, created_at) VALUES(?,?)",
            (f"Task {session_no}", now),
        )
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, title, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            session_no,
            "discussion",
            task_id,
            "running",
            now,
            10,
            f"Session {session_no}",
            now,
            now,
        ),
    )
    return {"session_id": session_id, "session_no": session_no, "task_id": task_id}


def _activate_session(db, session):
    db.set_setting("current_session_id", str(session["session_id"]))
    db.set_setting("current_session_no", str(session["session_no"]))
    db.set_setting("current_task_id", str(session["task_id"]))


def _monitor_run(db, group_id, cutoff, *, state="conflict_tension", rule_result=None):
    now = db.now_str()
    return db.execute(
        """
        INSERT INTO monitor_runs(
            group_id, cutoff_sequence, trigger_type, rule_result_json,
            final_state, confidence, status, analyzer_version, created_at, completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            cutoff,
            "new_message",
            json.dumps(rule_result or {
                "winning_state_code": state,
                "winning_score": 0.82,
                "evidence_sequences": [cutoff],
            }),
            state,
            0.82,
            "completed",
            "test",
            now,
            now,
        ),
    )


def _agent_id(db):
    row = db.query_one("SELECT id FROM users WHERE role='agent' ORDER BY id LIMIT 1")
    return row["id"]


def test_strategy_review_context_without_history_starts_at_first_student_message(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db, name="Context Group", code="CTX-A")
    student_id, _key = create_student(db, group_id)
    session = _create_session(db, session_no=11)
    _activate_session(db, session)

    first = db.create_message(group_id, student_id, "first", role="student")
    second = db.create_message(group_id, student_id, "second", role="student")
    monitor_run_id = _monitor_run(db, group_id, second["sequence"])

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    context = ContextBuilder.build_strategy_review_context(
        group_id,
        session["session_id"],
        monitor_run_id,
        second["sequence"],
    )

    assert context["context_from_sequence"] == first["sequence"]
    assert context["context_to_sequence"] == second["sequence"]
    assert context["input_message_sequences"] == [first["sequence"], second["sequence"]]
    assert context["task"]["title"] == "Task 11"
    assert context["task"]["goal"] == "Task goal"
    assert context["task"]["key_concepts"] == ["concept-a"]
    assert context["task"]["evaluation_criteria"] == ["criterion-a"]
    assert context["allowed_strategies"]


def test_strategy_context_uses_latest_strategy_boundary_and_excludes_emotion(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db, name="Boundary Group", code="CTX-B")
    student_id, _key = create_student(db, group_id)
    agent_id = _agent_id(db)
    session = _create_session(db, session_no=12)
    _activate_session(db, session)

    db.create_message(group_id, student_id, "before strategy", role="student")
    strategy_msg = db.create_message(group_id, agent_id, "strategy support", role="agent")
    db.execute(
        "UPDATE messages SET agent_type='strategy', strategy_id='v2_conflict_evidence' WHERE id=?",
        (strategy_msg["id"],),
    )
    after_strategy = db.create_message(group_id, student_id, "after strategy", role="student")
    emotion_msg = db.create_message(group_id, agent_id, "emotion support", role="agent")
    db.execute("UPDATE messages SET agent_type='emotion' WHERE id=?", (emotion_msg["id"],))
    latest = db.create_message(group_id, student_id, "latest", role="student")
    monitor_run_id = _monitor_run(db, group_id, latest["sequence"])

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    context = ContextBuilder.build_strategy_review_context(
        group_id,
        session["session_id"],
        monitor_run_id,
        latest["sequence"],
    )

    assert context["previous_strategy_intervention"]["sequence"] == strategy_msg["sequence"]
    assert context["context_from_sequence"] == strategy_msg["sequence"] + 1
    assert context["input_message_sequences"] == [
        after_strategy["sequence"],
        latest["sequence"],
    ]
    assert all(msg["agent_type"] != "emotion" for msg in context["messages"])
    by_sequence = {msg["sequence"]: msg for msg in context["messages"]}
    assert by_sequence[after_strategy["sequence"]]["can_be_state_evidence"] is True
    assert by_sequence[after_strategy["sequence"]]["sender_type"] == "student"
    assert emotion_msg["sequence"] not in by_sequence
    assert by_sequence[latest["sequence"]]["message_id"] == latest["sequence"]
    assert by_sequence[latest["sequence"]]["session_id"] == session["session_id"]


def test_strategy_boundary_infers_legacy_student_help_reply(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db, name="Help Boundary Group", code="CTX-C")
    student_id, _key = create_student(db, group_id)
    agent_id = _agent_id(db)
    session = _create_session(db, session_no=13)
    _activate_session(db, session)

    db.create_message(group_id, student_id, "help request", role="student")
    log_id = db.execute(
        """
        INSERT INTO intervention_logs(
            group_id, push_mode, trigger_source, title, message,
            session_id, task_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            "student_request",
            "student_help_request",
            "help",
            "legacy help reply",
            session["session_id"],
            session["task_id"],
            db.now_str(),
        ),
    )
    help_reply = db.create_message(
        group_id,
        agent_id,
        "legacy help reply",
        role="agent",
        linked_log_id=log_id,
    )
    latest = db.create_message(group_id, student_id, "after help", role="student")
    monitor_run_id = _monitor_run(db, group_id, latest["sequence"])

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    context = ContextBuilder.build_strategy_review_context(
        group_id,
        session["session_id"],
        monitor_run_id,
        latest["sequence"],
    )

    assert context["previous_strategy_intervention"]["sequence"] == help_reply["sequence"]
    assert context["previous_strategy_intervention"]["source"] == "student_request"
    assert context["input_message_sequences"] == [latest["sequence"]]


def test_strategy_context_is_session_isolated_and_pass_does_not_move_boundary(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db, name="Session Boundary Group", code="CTX-D")
    student_id, _key = create_student(db, group_id)
    agent_id = _agent_id(db)

    first_session = _create_session(db, session_no=14)
    _activate_session(db, first_session)
    db.create_message(group_id, student_id, "session one student", role="student")
    old_strategy = db.create_message(group_id, agent_id, "session one strategy", role="agent")
    db.execute("UPDATE messages SET agent_type='strategy' WHERE id=?", (old_strategy["id"],))

    second_session = _create_session(db, session_no=15)
    _activate_session(db, second_session)
    first = db.create_message(group_id, student_id, "session two first", role="student")
    pass_run = _monitor_run(db, group_id, first["sequence"], state="task_detached")
    db.execute(
        """
        UPDATE monitor_runs
        SET review_decision='PASS', context_from_sequence=?, context_to_sequence=?
        WHERE id=?
        """,
        (first["sequence"], first["sequence"], pass_run),
    )
    latest = db.create_message(group_id, student_id, "session two latest", role="student")
    monitor_run_id = _monitor_run(db, group_id, latest["sequence"])

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    context = ContextBuilder.build_strategy_review_context(
        group_id,
        second_session["session_id"],
        monitor_run_id,
        latest["sequence"],
    )

    assert context["previous_strategy_intervention"] is None
    assert context["context_from_sequence"] == first["sequence"]
    assert context["input_message_sequences"] == [first["sequence"], latest["sequence"]]


def test_strategy_context_truncates_deterministically_and_keeps_rule_evidence(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db, name="Long Context Group", code="CTX-E")
    student_id, _key = create_student(db, group_id)
    session = _create_session(db, session_no=16)
    _activate_session(db, session)

    last = None
    for index in range(45):
        last = db.create_message(group_id, student_id, f"message-{index + 1}", role="student")
    evidence_sequence = 18
    monitor_run_id = _monitor_run(
        db,
        group_id,
        last["sequence"],
        rule_result={
            "winning_state_code": "blocked_frustration",
            "winning_score": 0.9,
            "evidence_sequences": [evidence_sequence],
        },
    )

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    context = ContextBuilder.build_strategy_review_context(
        group_id,
        session["session_id"],
        monitor_run_id,
        last["sequence"],
    )

    sequences = context["input_message_sequences"]
    assert context["context_truncated"] is True
    assert sequences[:5] == [1, 2, 3, 4, 5]
    assert evidence_sequence in sequences
    assert sequences[-20:] == list(range(26, 46))
    assert context["omitted_sequence_ranges"]


def test_strategy_context_keeps_missing_task_fields_as_none(db_and_app):
    db, _app, _client = db_and_app
    group_id = create_group(db, name="Sparse Task Group", code="CTX-F")
    student_id, _key = create_student(db, group_id)
    session = _create_session(db, session_no=17, full_task=False)
    _activate_session(db, session)
    msg = db.create_message(group_id, student_id, "one message", role="student")
    monitor_run_id = _monitor_run(db, group_id, msg["sequence"])

    from services.intervention_pipeline_v2.context_builder import ContextBuilder

    context = ContextBuilder.build_strategy_review_context(
        group_id,
        session["session_id"],
        monitor_run_id,
        msg["sequence"],
    )

    assert context["task"]["title"] == "Task 17"
    assert context["task"]["description"] is None
    assert context["task"]["goal"] is None
    assert context["task"]["output_requirement"] is None
    assert context["task"]["key_concepts"] is None


def test_strategy_review_audit_columns_exist(db_and_app):
    db, _app, _client = db_and_app

    monitor_columns = {row["name"] for row in db.query_all("PRAGMA table_info(monitor_runs)")}
    intervention_columns = {row["name"] for row in db.query_all("PRAGMA table_info(intervention_runs)")}

    assert {
        "state_assessment_id",
        "session_id",
        "task_id",
        "decision",
        "teacher_reason",
        "message_id",
        "lock_acquired",
        "cooldown_result",
        "context_from_sequence",
        "context_to_sequence",
        "input_message_sequences_json",
        "evidence_sequences_json",
        "review_decision",
        "review_final_state",
        "review_confidence",
        "review_reason",
        "selected_strategy_id",
        "generated_message",
        "prompt_version",
        "review_started_at",
        "review_completed_at",
        "review_error",
    }.issubset(monitor_columns)
    assert {
        "context_from_sequence",
        "context_to_sequence",
        "input_message_sequences_json",
        "evidence_sequences_json",
        "selected_strategy_id",
        "prompt_version",
    }.issubset(intervention_columns)
