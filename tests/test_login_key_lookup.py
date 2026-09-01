# -*- coding: utf-8 -*-


def _fake_hash(login_key):
    return "hash:" + login_key


def _fake_check_password_hash(stored_hash, login_key):
    return stored_hash == _fake_hash(login_key)


def _create_group(db):
    return db.execute(
        "INSERT INTO groups(name, group_code, condition, state, created_at) VALUES(?,?,?,?,?)",
        ("Lookup Group", "GLK", "experiment", "OPEN", db.now_str()),
    )


def _insert_student_key(db, group_id, index, login_key, *, with_lookup=True):
    from services.login_key_lookup import compute_login_key_lookup_hash

    participant_code = f"LK-P{index}"
    user_id = db.execute(
        """
        INSERT INTO users(username, password_hash, real_name, participant_code, role, created_at)
        VALUES(?,?,?,?,?,?)
        """,
        (f"lookup_student_{index}", "x", participant_code, participant_code, "student", db.now_str()),
    )
    db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (group_id, user_id))
    if with_lookup:
        db.execute(
            """
            INSERT INTO experiment_participants(
                participant_code, login_key_hash, key_lookup_hash, group_no, member_no,
                group_id, user_id, display_name, is_active, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                participant_code,
                _fake_hash(login_key),
                compute_login_key_lookup_hash(login_key),
                90,
                index,
                group_id,
                user_id,
                participant_code,
                1,
                db.now_str(),
            ),
        )
    else:
        db.execute(
            """
            INSERT INTO experiment_participants(
                participant_code, login_key_hash, group_no, member_no,
                group_id, user_id, display_name, is_active, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                participant_code,
                _fake_hash(login_key),
                90,
                index,
                group_id,
                user_id,
                participant_code,
                1,
                db.now_str(),
            ),
        )
    return participant_code


def test_indexed_participant_login_checks_only_candidate(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    from auth import verify_participant_login_key

    group_id = _create_group(db)
    for index in range(1, 16):
        _insert_student_key(db, group_id, index, f"DISTRACTOR-{index}")
    participant_code = _insert_student_key(db, group_id, 99, "MATCH-STUDENT")

    calls = []

    def counted_check(stored_hash, login_key):
        calls.append(stored_hash)
        return _fake_check_password_hash(stored_hash, login_key)

    monkeypatch.setattr("werkzeug.security.check_password_hash", counted_check)

    result = verify_participant_login_key("MATCH-STUDENT")

    assert result["participant_code"] == participant_code
    assert calls == [_fake_hash("MATCH-STUDENT")]

    calls.clear()
    assert verify_participant_login_key("NO-SUCH-STUDENT") is None
    assert calls == []


def test_legacy_participant_login_backfills_lookup_hash(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    from auth import verify_participant_login_key
    from services.login_key_lookup import compute_login_key_lookup_hash

    group_id = _create_group(db)
    participant_code = _insert_student_key(db, group_id, 1, "LEGACY-STUDENT", with_lookup=False)
    monkeypatch.setattr("werkzeug.security.check_password_hash", _fake_check_password_hash)

    result = verify_participant_login_key("LEGACY-STUDENT")

    assert result["participant_code"] == participant_code
    row = db.query_one(
        "SELECT key_lookup_hash FROM experiment_participants WHERE participant_code=?",
        (participant_code,),
    )
    assert row["key_lookup_hash"] == compute_login_key_lookup_hash("LEGACY-STUDENT")


def test_indexed_teacher_login_checks_only_candidate(db_and_app, monkeypatch):
    db, _app_module, _client = db_and_app
    from auth import verify_teacher_login_key
    from services.login_key_lookup import compute_login_key_lookup_hash

    teacher = db.query_one("SELECT id FROM users WHERE role='teacher' ORDER BY id LIMIT 1")
    teacher_id = teacher["id"]
    for index in range(1, 12):
        login_key = f"DISTRACTOR-TEACHER-{index}"
        db.execute(
            """
            INSERT INTO teacher_access_keys(
                key_name, key_hash, key_lookup_hash, teacher_user_id, is_active, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                f"lookup_teacher_{index}",
                _fake_hash(login_key),
                compute_login_key_lookup_hash(login_key),
                teacher_id,
                1,
                db.now_str(),
            ),
        )
    db.execute(
        """
        INSERT INTO teacher_access_keys(
            key_name, key_hash, key_lookup_hash, teacher_user_id, is_active, created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            "lookup_teacher_match",
            _fake_hash("MATCH-TEACHER"),
            compute_login_key_lookup_hash("MATCH-TEACHER"),
            teacher_id,
            1,
            db.now_str(),
        ),
    )

    calls = []

    def counted_check(stored_hash, login_key):
        calls.append(stored_hash)
        return _fake_check_password_hash(stored_hash, login_key)

    monkeypatch.setattr("werkzeug.security.check_password_hash", counted_check)

    result = verify_teacher_login_key("MATCH-TEACHER")

    assert result["key_name"] == "lookup_teacher_match"
    assert calls == [_fake_hash("MATCH-TEACHER")]

    calls.clear()
    assert verify_teacher_login_key("NO-SUCH-TEACHER") is None
    assert calls == []
