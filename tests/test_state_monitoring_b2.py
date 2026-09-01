# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

import pytest


BASE_TIME = datetime(2026, 7, 16, 19, 50, 0)


def _dt(offset_seconds):
    return (BASE_TIME + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _rows(contents, speakers=None):
    if speakers is None:
        speakers = [(index % 4) + 1 for index in range(len(contents))]
    return [
        {
            "id": index + 1,
            "role": "student",
            "user_id": speakers[index],
            "username": f"s{speakers[index]}",
            "real_name": f"S{speakers[index]}",
            "content": content,
            "created_at": _dt(index * 30),
        }
        for index, content in enumerate(contents)
    ]


def _detect(contents, speakers=None, *, active_students=4, participant_count=4, last_message_time=None):
    from services.feature_service import extract_group_features
    from services.rule_state_service import detect_group_state_rule

    rows = _rows(contents, speakers=speakers)
    window_end_offset = ((len(rows) - 1) * 30 + 30) if rows else 240
    context = {
        "window_start": _dt(0),
        "window_end": _dt(window_end_offset),
        "window_minutes": 4,
        "window_messages": rows,
        "window_student_messages": rows,
        "low_window_student_messages": rows,
        "recent_student_messages": rows,
        "recent_checkins": [],
        "checkin_summary": {},
        "page_activity": {
            "active_students": active_students,
            "active_duration_seconds": 240,
        },
        "participant_count": participant_count,
        "last_student_message_time": last_message_time or (rows[-1]["created_at"] if rows else None),
        "current_task": {
            "title": "生态调查",
            "question": "请小组讨论生态调查证据并形成结论",
            "key_concepts": ["生态", "证据", "结论"],
            "expected_dimensions": ["分工", "整理", "总结"],
        },
    }
    features = extract_group_features(context)
    return detect_group_state_rule(context, features)


def _candidate_reasons(result, state_code):
    for candidate in result["candidates"]:
        if candidate["state_code"] == state_code:
            return {signal["reason"] for signal in candidate["signals"]}
    return set()


@pytest.mark.parametrize(
    ("name", "contents", "speakers", "expected"),
    [
        (
            "#53-#54",
            ["我先看一下材料", "我还在理解题目"],
            [1, 2],
            "unknown",
        ),
        (
            "#55-#58",
            ["我负责整理证据", "我补充一个案例", "同意，我们可以按这个推进", "最后总结两点结论"],
            [1, 2, 3, 4],
            "positive_collaboration",
        ),
        (
            "#59-#63",
            ["我负责整理证据", "我继续补充案例", "我来写结论", "我再总结一下", "我就按这个写"],
            [1, 1, 1, 1, 1],
            "unknown",
        ),
        (
            "#64-#67",
            ["谁负责记录", "下一步干嘛", "没人整理材料", "证据不够，不知道写哪部分"],
            [1, 2, 3, 4],
            "blocked_frustration",
        ),
        (
            "#68-#72",
            ["不对，你这个想法不行", "你错了，按你的来不合理", "别乱说", "我们先看证据"],
            [1, 2, 1, 3],
            "conflict_tension",
        ),
        (
            "#75-#78",
            ["先点奶茶吧", "我想打游戏", "随便吧，听你们的", "我没什么想说"],
            [1, 2, 3, 4],
            "task_detached",
        ),
        (
            "#79-#83",
            ["卡住了", "不知道怎么推进", "没思路", "证据不足，结论连不起来"],
            [1, 2, 3, 4],
            "blocked_frustration",
        ),
        (
            "#93-#96",
            ["回到任务，我负责整理证据", "我补充数据", "同意，下一步写结论", "我们总结成两点"],
            [1, 2, 3, 4],
            "positive_collaboration",
        ),
    ],
)
def test_b2_transcript_window_expectations(name, contents, speakers, expected):
    result = _detect(contents, speakers)

    assert result["winning_state_code"] == expected, name


def test_b2_participation_imbalance_is_evidence_not_final_state():
    result = _detect(
        ["我负责整理证据", "我继续补充案例", "我来写结论", "我再总结一下", "我就按这个写"],
        [1, 1, 1, 1, 1],
    )

    assert result["winning_state_code"] == "unknown"
    assert "participation_imbalance" in _candidate_reasons(result, "unknown")
    assert "participation_imbalance" not in {item["state_code"] for item in result["candidates"]}


def test_b2_coordination_blocked_exposes_evidence_tags():
    result = _detect(
        ["谁负责记录", "下一步干嘛", "没人整理材料", "证据不够，不知道写哪部分"],
        [1, 2, 3, 4],
    )

    assert result["winning_state_code"] == "blocked_frustration"
    assert "coordination_blocked" in result["signals"]["coordination_evidence_tags"]
    assert "coordination_blocked" in _candidate_reasons(result, "blocked_frustration")


def test_b2_negative_silence_can_be_generated_without_new_messages():
    from services.feature_service import extract_group_features
    from services.rule_state_service import detect_group_state_rule

    context = {
        "window_start": "2026-07-16 19:54:00",
        "window_end": "2026-07-16 19:57:30",
        "window_minutes": 4,
        "window_messages": [],
        "window_student_messages": [],
        "low_window_student_messages": [],
        "recent_student_messages": [],
        "recent_checkins": [],
        "checkin_summary": {},
        "page_activity": {
            "active_students": 3,
            "active_duration_seconds": 240,
        },
        "participant_count": 4,
        "last_student_message_time": "2026-07-16 19:54:00",
    }
    features = extract_group_features(context)
    result = detect_group_state_rule(context, features)

    assert result["winning_state_code"] == "negative_silence"
    assert result["signals"]["online_no_text_silence"] is True


def test_b2_single_speaker_with_active_silent_peers_is_negative_silence():
    from services.feature_service import extract_group_features
    from services.rule_state_service import detect_group_state_rule

    rows = _rows(
        [
            "I can draft the first point.",
            "I will add the evidence.",
            "I can write the conclusion.",
            "I will keep going.",
        ],
        speakers=[1, 1, 1, 1],
    )
    context = {
        "window_start": _dt(0),
        "window_end": _dt(300),
        "window_minutes": 5,
        "window_messages": rows,
        "window_student_messages": rows,
        "low_window_student_messages": rows,
        "recent_student_messages": rows,
        "recent_checkins": [],
        "checkin_summary": {},
        "page_activity": {
            "active_students": 4,
            "active_duration_seconds": 300,
        },
        "participant_count": 4,
        "last_student_message_time": rows[-1]["created_at"],
        "participants": [
            {
                "user_id": 1,
                "username": "s1",
                "participant_code": "S1",
                "message_count_10m": 4,
                "recent_message_count": 4,
                "last_message_at": rows[-1]["created_at"],
                "active_on_page": True,
                "active_session_count": 1,
            },
            {
                "user_id": 2,
                "username": "s2",
                "participant_code": "S2",
                "message_count_10m": 0,
                "recent_message_count": 0,
                "last_message_at": _dt(0),
                "active_on_page": True,
                "active_session_count": 1,
            },
            {
                "user_id": 3,
                "username": "s3",
                "participant_code": "S3",
                "message_count_10m": 0,
                "recent_message_count": 0,
                "last_message_at": _dt(0),
                "active_on_page": True,
                "active_session_count": 1,
            },
            {
                "user_id": 4,
                "username": "s4",
                "participant_code": "S4",
                "message_count_10m": 0,
                "recent_message_count": 0,
                "last_message_at": _dt(0),
                "active_on_page": True,
                "active_session_count": 1,
            },
        ],
    }

    result = detect_group_state_rule(context, extract_group_features(context))

    assert result["winning_state_code"] == "negative_silence"
    assert result["signals"]["single_speaker_silence"] is True
    assert result["signals"]["silent_active_peer_count"] == 3
