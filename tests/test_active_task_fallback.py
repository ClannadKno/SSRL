# -*- coding: utf-8 -*-


def test_legacy_active_task_without_keywords_does_not_crash(db_and_app):
    db, _app_module, _client = db_and_app

    db.execute("DELETE FROM experiment_sessions")
    db.execute("DELETE FROM tasks")
    db.execute(
        "INSERT INTO tasks(title, description, is_active, created_at) VALUES(?,?,?,?)",
        ("Legacy task", "Fallback task without keyword metadata", 1, db.now_str()),
    )

    from agent.detector import get_active_task, task_relevance_score

    task = get_active_task()

    assert task["title"] == "Legacy task"
    assert task["keywords"] == ""
    assert task["key_concepts"] == []
    assert task_relevance_score("anything") == 0


def test_task_relevance_score_accepts_json_keyword_string(db_and_app):
    db, _app_module, _client = db_and_app

    db.execute("DELETE FROM experiment_sessions")
    task_id = db.execute(
        """
        INSERT INTO learning_tasks(
            title, question, keywords, key_concepts_json,
            expected_dimensions_json, time_limit_minutes, is_active, created_at
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            "Structured task",
            "Discuss the shared space budget.",
            '["budget", "space"]',
            '["budget", "space"]',
            "[]",
            30,
            1,
            db.now_str(),
        ),
    )
    now = db.now_str()
    db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status,
            start_time, created_at, updated_at
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (1, "S1_baseline", task_id, "running", now, now, now),
    )

    from agent.detector import task_relevance_score

    assert task_relevance_score("The group should compare budget options.") == 1
