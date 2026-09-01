# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.helpers import create_group, create_student, seed_running_session


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _insert_message(
    db,
    ctx,
    *,
    sequence: int,
    role: str = "student",
    content: str = "message",
    user_id: int = None,
    session_id: int = None,
    session_no: int = None,
    group_id: int = None,
    task_id: int = None,
    created_at: str = None,
    agent_type: str = None,
) -> int:
    gid = group_id if group_id is not None else ctx["group_id"]
    uid = user_id if user_id is not None else ctx["students"][0][0]
    sid = session_id if session_id is not None else ctx["session_id"]
    sno = session_no if session_no is not None else ctx["session_no"]
    tid = task_id if task_id is not None else ctx["task_id"]
    sender_type = "agent" if role == "agent" else role
    mid = db.execute(
        """
        INSERT INTO messages(
            group_id, user_id, content, sequence, sender_type, role,
            session_no, task_id, session_id, agent_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            gid,
            uid,
            content,
            sequence,
            sender_type,
            role,
            sno,
            tid,
            sid,
            agent_type,
            created_at or db.now_str(),
        ),
    )
    db.execute(
        """
        UPDATE groups
        SET last_message_sequence=MAX(COALESCE(last_message_sequence, 0), ?)
        WHERE id=?
        """,
        (sequence, gid),
    )
    return mid


def _save_segments(db, ctx, segments, *, anchor=1, source_run_id=10):
    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    return CollaborationStateSegmentService.save_strategy_llm_segments(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_segments=segments,
        source_run_id=source_run_id,
        analysis_anchor_message_id=anchor,
        analysis_window_start_message_id=anchor,
        analysis_window_end_message_id=max(
            [anchor] + [seg.get("end_message_id", anchor) for seg in segments]
        ),
        prompt_version="test_prompt_v1",
    )


def _count_segments(db, ctx):
    row = db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        """,
        (ctx["group_id"], ctx["session_id"]),
    )
    return int(row["c"])


def _start_discussion_window(db, ctx, *, base: datetime):
    from services.group_discussion_runtime_service import enter_group_discussion_stage

    for uid, _login_key in ctx["students"]:
        runtime = enter_group_discussion_stage(ctx["session_id"], ctx["group_id"], uid)
    started = base - timedelta(minutes=8)
    deadline = base + timedelta(minutes=8)
    db.execute(
        """
        UPDATE group_session_discussions
        SET status='running', started_at=?, deadline=?, updated_at=?
        WHERE id=?
        """,
        (_ts(started), _ts(deadline), db.now_str(), runtime["id"]),
    )


def test_strategy_segments_save_multiple_and_positive(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=301, member_count=2)
    _insert_message(db, ctx, sequence=1, content="先分工找证据。")
    _insert_message(db, ctx, sequence=2, user_id=ctx["students"][1][0], content="我来整理标准。")
    _insert_message(db, ctx, sequence=3, content="这个点我们先再核对。")

    result = _save_segments(
        db,
        ctx,
        [
            {
                "state": "positive_collaboration",
                "start_message_id": 1,
                "end_message_id": 2,
                "evidence_message_ids": [1, 2],
                "confidence": 0.91,
            },
            {
                "state": "conflict_tension",
                "start_message_id": 3,
                "end_message_id": 3,
                "evidence_message_ids": [3],
                "confidence": 0.72,
            },
        ],
    )

    assert result["saved_count"] == 2
    rows = db.query_all(
        "SELECT state_code, evidence_message_ids_json FROM collaboration_state_segments ORDER BY start_message_id",
    )
    assert [row["state_code"] for row in rows] == ["positive_collaboration", "conflict_tension"]
    assert json.loads(rows[0]["evidence_message_ids_json"]) == [1, 2]


def test_observing_segment_is_rejected_and_not_saved(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=302)
    _insert_message(db, ctx, sequence=1)

    from services.collaboration_state_segment_service import SegmentValidationError

    with pytest.raises(SegmentValidationError):
        _save_segments(
            db,
            ctx,
            [
                {
                    "state": "unknown",
                    "start_message_id": 1,
                    "end_message_id": 1,
                    "evidence_message_ids": [1],
                    "confidence": 0.5,
                }
            ],
        )
    assert _count_segments(db, ctx) == 0


def test_agent_message_evidence_is_rejected(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=303)
    _insert_message(db, ctx, sequence=1)
    _insert_message(db, ctx, sequence=2, role="agent", agent_type="strategy")

    from services.collaboration_state_segment_service import SegmentValidationError

    with pytest.raises(SegmentValidationError):
        _save_segments(
            db,
            ctx,
            [
                {
                    "state": "conflict_tension",
                    "start_message_id": 1,
                    "end_message_id": 2,
                    "evidence_message_ids": [2],
                    "confidence": 0.8,
                }
            ],
        )
    assert _count_segments(db, ctx) == 0


def test_cross_group_reference_is_rejected(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=304)
    other_group = create_group(db, name="Other", code="G304X")
    other_student, _key = create_student(db, other_group, index=1, username_prefix="other304")
    _insert_message(db, ctx, group_id=other_group, user_id=other_student, sequence=9)

    from services.collaboration_state_segment_service import SegmentValidationError

    with pytest.raises(SegmentValidationError):
        _save_segments(
            db,
            ctx,
            [
                {
                    "state": "positive_collaboration",
                    "start_message_id": 9,
                    "end_message_id": 9,
                    "evidence_message_ids": [9],
                    "confidence": 0.8,
                }
            ],
        )
    assert _count_segments(db, ctx) == 0


def test_cross_session_reference_is_rejected(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=305)
    now = db.now_str()
    other_session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (9305, "discussion", ctx["task_id"], "running", now, 10, now, now),
    )
    _insert_message(db, ctx, sequence=9, session_id=other_session_id, session_no=9305)

    from services.collaboration_state_segment_service import SegmentValidationError

    with pytest.raises(SegmentValidationError):
        _save_segments(
            db,
            ctx,
            [
                {
                    "state": "positive_collaboration",
                    "start_message_id": 9,
                    "end_message_id": 9,
                    "evidence_message_ids": [9],
                    "confidence": 0.8,
                }
            ],
        )
    assert _count_segments(db, ctx) == 0


def test_same_dedupe_retry_does_not_increase_records(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=306)
    _insert_message(db, ctx, sequence=1)
    segment = {
        "state": "positive_collaboration",
        "start_message_id": 1,
        "end_message_id": 1,
        "evidence_message_ids": [1],
        "confidence": 0.82,
    }

    _save_segments(db, ctx, [segment], source_run_id=20)
    _save_segments(db, ctx, [segment], source_run_id=20)

    assert _count_segments(db, ctx) == 1


def test_state_monitor_merges_adjacent_same_state_17_20_and_21_24(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=317)
    for sequence in range(17, 25):
        _insert_message(db, ctx, sequence=sequence, content=f"student message {sequence}")

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    first = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="blocked_frustration",
        start_message_id=17,
        end_message_id=20,
        evidence_message_ids=[17, 20],
        confidence=0.84,
        source_run_id=701,
        assessment_id=1701,
        analysis_window_start_message_id=17,
        analysis_window_end_message_id=20,
    )
    second = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="blocked_frustration",
        start_message_id=21,
        end_message_id=24,
        evidence_message_ids=[21, 24],
        confidence=0.91,
        source_run_id=702,
        assessment_id=1702,
        analysis_window_start_message_id=21,
        analysis_window_end_message_id=24,
    )

    assert first["saved"] is True
    assert second["merged"] is True
    rows = db.query_all(
        """
        SELECT state_code, segment_kind, start_message_id, end_message_id,
               evidence_message_ids_json, confidence, source, source_run_id,
               assessment_id, is_finalized
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        """,
        (ctx["group_id"], ctx["session_id"]),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["state_code"] == "blocked_frustration"
    assert row["segment_kind"] == "message_range"
    assert row["start_message_id"] == 17
    assert row["end_message_id"] == 24
    assert json.loads(row["evidence_message_ids_json"]) == [17, 20, 21, 24]
    assert row["confidence"] == 0.91
    assert row["source"] == "state_monitor"
    assert row["source_run_id"] == 702
    assert row["assessment_id"] == 1701
    assert row["is_finalized"] == 1


def test_state_monitor_different_state_creates_new_segment(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=318)
    for sequence in range(1, 5):
        _insert_message(db, ctx, sequence=sequence, content=f"student message {sequence}")

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="blocked_frustration",
        start_message_id=1,
        end_message_id=2,
        evidence_message_ids=[1, 2],
        confidence=0.82,
        source_run_id=801,
        assessment_id=1801,
    )
    CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="conflict_tension",
        start_message_id=3,
        end_message_id=4,
        evidence_message_ids=[3, 4],
        confidence=0.86,
        source_run_id=802,
        assessment_id=1802,
    )

    rows = db.query_all(
        """
        SELECT state_code, start_message_id, end_message_id, source
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        ORDER BY start_message_id
        """,
        (ctx["group_id"], ctx["session_id"]),
    )
    assert [dict(row) for row in rows] == [
        {
            "state_code": "blocked_frustration",
            "start_message_id": 1,
            "end_message_id": 2,
            "source": "state_monitor",
        },
        {
            "state_code": "conflict_tension",
            "start_message_id": 3,
            "end_message_id": 4,
            "source": "state_monitor",
        },
    ]


def test_state_monitor_retry_same_assessment_does_not_duplicate(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=319)
    _insert_message(db, ctx, sequence=1)
    _insert_message(db, ctx, sequence=2)

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    first = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="task_detached",
        start_message_id=1,
        end_message_id=2,
        evidence_message_ids=[1, 2],
        confidence=0.81,
        source_run_id=901,
        assessment_id=1901,
    )
    second = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="task_detached",
        start_message_id=1,
        end_message_id=2,
        evidence_message_ids=[1, 2],
        confidence=0.81,
        source_run_id=901,
        assessment_id=1901,
    )

    assert first["segment_id"] == second["segment_id"]
    assert second["reason"] == "assessment_already_persisted"
    assert _count_segments(db, ctx) == 1


def test_state_monitor_unknown_is_skipped(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=320)

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    result = CollaborationStateSegmentService.upsert_monitor_assessment_segment(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        task_id=ctx["task_id"],
        state_code="unknown",
        start_message_id=1,
        end_message_id=1,
        evidence_message_ids=[1],
        confidence=0.5,
        source_run_id=1001,
        assessment_id=2001,
    )

    assert result == {"skipped": True, "reason": "final_state_unknown", "saved_count": 0}
    assert _count_segments(db, ctx) == 0


def test_same_anchor_replaces_provisional_results(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=307)
    _insert_message(db, ctx, sequence=1)
    _insert_message(db, ctx, sequence=2)

    _save_segments(
        db,
        ctx,
        [
            {
                "state": "conflict_tension",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.72,
            }
        ],
        source_run_id=21,
    )
    _save_segments(
        db,
        ctx,
        [
            {
                "state": "positive_collaboration",
                "start_message_id": 2,
                "end_message_id": 2,
                "evidence_message_ids": [2],
                "confidence": 0.88,
            }
        ],
        source_run_id=22,
    )

    rows = db.query_all(
        "SELECT state_code, start_message_id FROM collaboration_state_segments WHERE group_id=? AND session_id=?",
        (ctx["group_id"], ctx["session_id"]),
    )
    assert [dict(row) for row in rows] == [
        {"state_code": "positive_collaboration", "start_message_id": 2}
    ]


def test_finalized_anchor_is_not_overwritten(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=308)
    _insert_message(db, ctx, sequence=1)
    _insert_message(db, ctx, sequence=2)

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    _save_segments(
        db,
        ctx,
        [
            {
                "state": "conflict_tension",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.72,
            }
        ],
        source_run_id=31,
    )
    CollaborationStateSegmentService.finalize_anchor(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        analysis_anchor_message_id=1,
    )
    _save_segments(
        db,
        ctx,
        [
            {
                "state": "positive_collaboration",
                "start_message_id": 2,
                "end_message_id": 2,
                "evidence_message_ids": [2],
                "confidence": 0.88,
            }
        ],
        source_run_id=32,
    )

    rows = db.query_all(
        """
        SELECT state_code, start_message_id, is_finalized
        FROM collaboration_state_segments
        WHERE group_id=? AND session_id=?
        ORDER BY id
        """,
        (ctx["group_id"], ctx["session_id"]),
    )
    assert [dict(row) for row in rows] == [
        {"state_code": "conflict_tension", "start_message_id": 1, "is_finalized": 1},
        {"state_code": "positive_collaboration", "start_message_id": 2, "is_finalized": 0},
    ]


def test_new_anchor_keeps_separate_provisional_tail(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=309)
    _insert_message(db, ctx, sequence=1)
    _insert_message(db, ctx, sequence=3)

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    _save_segments(
        db,
        ctx,
        [
            {
                "state": "conflict_tension",
                "start_message_id": 1,
                "end_message_id": 1,
                "evidence_message_ids": [1],
                "confidence": 0.72,
            }
        ],
        anchor=1,
        source_run_id=41,
    )
    CollaborationStateSegmentService.finalize_anchor(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
        analysis_anchor_message_id=1,
    )
    _save_segments(
        db,
        ctx,
        [
            {
                "state": "positive_collaboration",
                "start_message_id": 3,
                "end_message_id": 3,
                "evidence_message_ids": [3],
                "confidence": 0.9,
            }
        ],
        anchor=3,
        source_run_id=42,
    )

    tail = CollaborationStateSegmentService.get_latest_unfinalized_analysis_range(
        group_id=ctx["group_id"],
        session_id=ctx["session_id"],
        session_no=ctx["session_no"],
    )
    assert tail["analysis_anchor_message_id"] == 3
    assert _count_segments(db, ctx) == 2


def test_negative_silence_saved_as_time_range(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=310)
    base = datetime.now()
    _start_discussion_window(db, ctx, base=base)
    _insert_message(db, ctx, sequence=1, created_at=_ts(base - timedelta(minutes=4)))

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    result = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        source_run_id=51,
        assessment_id=5101,
        now_value=_ts(base),
    )

    assert result["skipped"] is False
    row = db.query_one("SELECT * FROM collaboration_state_segments WHERE group_id=?", (ctx["group_id"],))
    assert row["state_code"] == "negative_silence"
    assert row["segment_kind"] == "time_range"
    assert row["start_message_id"] is None
    assert row["previous_student_message_id"] == 1
    assert row["assessment_id"] == 5101
    assert row["raw_silence_started_at"] == _ts(base - timedelta(minutes=4))
    assert row["threshold_reached_at"] == _ts(base - timedelta(minutes=1))
    assert row["start_at"] == row["threshold_reached_at"]
    assert row["end_at"] is None
    assert row["is_active"] == 1
    assert row["silent_seconds_at_detection"] >= 180
    assert row["gap_seconds"] >= 0


def test_agent_message_does_not_close_silence(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=311)
    base = datetime.now()
    _start_discussion_window(db, ctx, base=base)
    _insert_message(db, ctx, sequence=1, created_at=_ts(base - timedelta(minutes=4)))

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        now_value=_ts(base),
    )
    agent_id = _insert_message(
        db,
        ctx,
        sequence=2,
        role="agent",
        agent_type="emotion",
        created_at=_ts(base + timedelta(seconds=5)),
    )
    result = CollaborationStateSegmentService.close_open_silence_on_student_message(message_id=agent_id)

    assert result["skipped"] is True
    row = db.query_one("SELECT is_finalized, next_student_message_id FROM collaboration_state_segments")
    assert row["is_finalized"] == 0
    assert row["next_student_message_id"] is None


def test_student_message_closes_silence(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=312)
    base = datetime.now()
    _start_discussion_window(db, ctx, base=base)
    _insert_message(db, ctx, sequence=1, created_at=_ts(base - timedelta(minutes=4)))

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        now_value=_ts(base),
    )
    student_id = _insert_message(
        db,
        ctx,
        sequence=3,
        created_at=_ts(base + timedelta(seconds=10)),
    )
    result = CollaborationStateSegmentService.close_open_silence_on_student_message(message_id=student_id)

    assert result["closed"] is True
    row = db.query_one(
        "SELECT is_finalized, next_student_message_id FROM collaboration_state_segments"
    )
    assert row["is_finalized"] == 1
    assert row["next_student_message_id"] == 3


def test_session_ended_does_not_create_new_silence(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=313)
    base = datetime.now()
    _start_discussion_window(db, ctx, base=base)
    _insert_message(db, ctx, sequence=1, created_at=_ts(base - timedelta(minutes=4)))
    db.execute("UPDATE experiment_sessions SET status='ended' WHERE id=?", (ctx["session_id"],))

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    result = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        now_value=_ts(base),
    )

    assert result == {"skipped": True, "reason": "session_not_running"}
    assert _count_segments(db, ctx) == 0


def test_frozen_task_does_not_create_new_silence(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=314)
    base = datetime.now()
    _start_discussion_window(db, ctx, base=base)
    _insert_message(db, ctx, sequence=1, created_at=_ts(base - timedelta(minutes=4)))
    db.execute(
        """
        INSERT INTO collaborative_documents(
            group_id, task_id, session_no, title, status, created_by,
            created_at, updated_at, submitted_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            ctx["group_id"],
            ctx["task_id"],
            ctx["session_no"],
            "Doc",
            "submitted",
            ctx["students"][0][0],
            db.now_str(),
            db.now_str(),
            db.now_str(),
        ),
    )

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    result = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        now_value=_ts(base),
    )

    assert result == {"skipped": True, "reason": "task_frozen"}
    assert _count_segments(db, ctx) == 0


def test_repeated_silence_retry_updates_one_record(db_and_app):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=315)
    base = datetime.now()
    _start_discussion_window(db, ctx, base=base)
    _insert_message(db, ctx, sequence=1, created_at=_ts(base - timedelta(minutes=4)))

    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    first = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        now_value=_ts(base),
    )
    second = CollaborationStateSegmentService.record_negative_silence_if_applicable(
        group_id=ctx["group_id"],
        expected_sequence=1,
        now_value=_ts(base + timedelta(seconds=30)),
    )

    assert first["skipped"] is False
    assert second["skipped"] is False
    assert _count_segments(db, ctx) == 1
    row = db.query_one("SELECT gap_seconds FROM collaboration_state_segments")
    assert row["gap_seconds"] >= first["gap_seconds"]


def test_segment_close_failure_does_not_block_message_pipeline(db_and_app, monkeypatch):
    db, _app, _client = db_and_app
    ctx = seed_running_session(db, session_no=316)

    import services.discussion_pipeline_v2.monitoring_service as monitoring_module
    from agent import monitoring_tasks
    from services.collaboration_state_segment_service import CollaborationStateSegmentService

    def boom(**_kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(monitoring_module, "DISCUSSION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(
        CollaborationStateSegmentService,
        "close_open_silence_on_student_message",
        staticmethod(boom),
    )
    monkeypatch.setattr(
        monitoring_tasks.process_new_message_task,
        "schedule",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        monitoring_module.MonitoringService,
        "schedule_silence_check",
        staticmethod(lambda *_args, **_kwargs: None),
    )

    monitoring_module.MonitoringService.process_new_message(
        message_id=123,
        group_id=ctx["group_id"],
        sequence=1,
        is_student_msg=True,
    )
