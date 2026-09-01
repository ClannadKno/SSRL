# -*- coding: utf-8 -*-
"""B7 end-to-end regression coverage for state-monitoring acceptance."""

import csv
import io
import json
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from tests.helpers import seed_running_session


ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = datetime(2026, 7, 17, 13, 0, 0)


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
            "username": "s%s" % speakers[index],
            "real_name": "S%s" % speakers[index],
            "content": content,
            "created_at": _dt(index * 30),
        }
        for index, content in enumerate(contents)
    ]


def _detect_replay_case(case):
    from services.feature_service import extract_group_features
    from services.rule_state_service import detect_group_state_rule

    rows = _rows(case["contents"], speakers=case.get("speakers"))
    window_seconds = case.get("window_seconds") or max(240, (len(rows) + 1) * 30)
    if case.get("silence_seconds") is not None:
        last_student_message_time = _dt(window_seconds - case["silence_seconds"])
    elif rows:
        last_student_message_time = rows[-1]["created_at"]
    else:
        last_student_message_time = None

    context = {
        "window_start": _dt(0),
        "window_end": _dt(window_seconds),
        "window_minutes": max(1, round(window_seconds / 60)),
        "window_messages": rows,
        "window_student_messages": rows,
        "low_window_student_messages": rows,
        "recent_student_messages": rows,
        "recent_checkins": [],
        "checkin_summary": {},
        "page_activity": {
            "active_students": case.get("active_students", 4),
            "active_duration_seconds": window_seconds,
        },
        "participant_count": 4,
        "last_student_message_time": last_student_message_time,
        "current_task": {
            "title": "生态调查",
            "question": "请小组讨论生态调查证据并形成结论",
            "key_concepts": ["生态", "证据", "结论"],
            "expected_dimensions": ["分工", "整理", "总结"],
        },
    }
    return detect_group_state_rule(context, extract_group_features(context))


def _run_node(args, cwd=ROOT):
    result = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        "command failed: %s\nstdout:\n%s\nstderr:\n%s"
        % (" ".join(args), result.stdout, result.stderr)
    )
    return result


def _zip_csv_header(zip_bytes, suffix):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        matches = [
            name for name in zf.namelist()
            if name.endswith("/" + suffix) or name == suffix
        ]
        assert matches, zf.namelist()
        with zf.open(matches[0]) as handle:
            text = handle.read().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows, suffix
    return set(rows[0])


def test_b7_replays_trigger_state_transcript_windows_to_final_states():
    from knowledge_base import FINAL_STATE_CODES, LEGACY_PRIMARY_STATE_CODES

    cases = [
        {
            "name": "00:10-00:25 unknown readiness",
            "contents": ["我先把材料打开，确认大家都进入讨论页。", "我也进来了，先等大家准备好。"],
            "speakers": [1, 2],
            "window_seconds": 45,
            "active_students": 2,
            "expected": "unknown",
        },
        {
            "name": "00:45-01:21 positive collaboration",
            "contents": ["我负责整理证据", "我补充一个案例", "同意，我们可以按这个推进", "最后总结两点结论"],
            "speakers": [1, 2, 3, 4],
            "expected": "positive_collaboration",
        },
        {
            "name": "01:56-02:56 participation imbalance normalizes to unknown",
            "contents": ["我负责整理证据", "我继续补充案例", "我来写结论", "我再总结一下", "我就按这个写"],
            "speakers": [1, 1, 1, 1, 1],
            "expected": "unknown",
        },
        {
            "name": "03:56-04:46 coordination disorder normalizes to blocked",
            "contents": ["谁负责记录", "下一步干嘛", "没人整理材料", "证据不够，不知道写哪部分"],
            "speakers": [1, 2, 3, 4],
            "expected": "blocked_frustration",
        },
        {
            "name": "05:46-06:46 conflict tension",
            "contents": ["不对，你这个想法不行", "你错了，按你的来不合理", "别乱说", "我们先看证据"],
            "speakers": [1, 2, 1, 3],
            "expected": "conflict_tension",
        },
        {
            "name": "07:56-08:36 task detached",
            "contents": ["先点奶茶吧", "我想打游戏", "随便吧，听你们的", "我没什么想说"],
            "speakers": [1, 2, 3, 4],
            "expected": "task_detached",
        },
        {
            "name": "09:46-10:36 blocked frustration",
            "contents": ["卡住了", "不知道怎么推进", "没思路", "证据不足，结论连不起来"],
            "speakers": [1, 2, 3, 4],
            "expected": "blocked_frustration",
        },
        {
            "name": "10:36-14:56 negative silence",
            "contents": [],
            "window_seconds": 260,
            "silence_seconds": 245,
            "active_students": 4,
            "expected": "negative_silence",
        },
    ]

    observed = set()
    for case in cases:
        result = _detect_replay_case(case)
        candidate_codes = {item["state_code"] for item in result["candidates"]}
        assert result["winning_state_code"] == case["expected"], case["name"]
        assert candidate_codes <= set(FINAL_STATE_CODES), case["name"]
        assert not (candidate_codes & set(LEGACY_PRIMARY_STATE_CODES)), case["name"]
        observed.add(result["winning_state_code"])

    assert set(FINAL_STATE_CODES) <= observed


def test_b7_load_test_reports_room_ai_intervening_as_separate_category():
    script = """
const { Metrics, isRoomAiInterveningError } = require('./src/metrics');
const scenario = {
  name: 'b7-room-ai',
  baseUrl: 'http://127.0.0.1:8000',
  resourceMode: 'light',
  totalStudents: 1,
  discussionDurationMs: 0
};
const metrics = new Metrics({ runId: 'b7-room-ai', scenario });
const error = new Error('HTTP 423: {"error":"ROOM_AI_INTERVENING"}');
if (!isRoomAiInterveningError(error)) throw new Error('423 classifier returned false');
metrics.registerStudent({ id: 'S1', profileName: 'scripted' });
metrics.recordHelpAttempt('S1', 'G01');
metrics.recordHelpFailure('S1', 'G01', error);
const summary = metrics.summary();
if (summary.counters.helpFailed !== 1) throw new Error('helpFailed counter mismatch');
if (summary.counters.roomAiIntervening !== 1) throw new Error('roomAiIntervening counter mismatch');
if (!metrics.events.some((event) => event.type === 'room_ai_intervening' && event.status === 423)) {
  throw new Error('room_ai_intervening event missing');
}
"""
    _run_node(["node", "-e", script], cwd=ROOT / "load-test")


def test_b7_emotion_trend_page_static_asset_has_no_console_error(teacher_login):
    client, headers = teacher_login

    response = client.get("/teacher/emotion-trend", headers=headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "/static/teacher/emotion-trend.js" in html

    asset = client.get("/static/teacher/emotion-trend.js", headers=headers)
    assert asset.status_code == 200
    source = asset.get_data(as_text=True)
    assert "console.error" not in source
    assert "const PRIMARY_STATE_ORDER" in source

    _run_node(["node", "--check", "static/teacher/emotion-trend.js"])


def test_b7_empty_export_has_metadata_without_empty_data_directories(db_and_app, teacher_login):
    db, _app_module, _unused_client = db_and_app
    client, headers = teacher_login
    context = seed_running_session(db, session_no=71, member_count=2, limit_minutes=20)

    response = client.get(
        "/export/all?session_id=%s" % context["session_id"],
        headers=headers,
    )
    assert response.status_code == 200
    zip_bytes = response.get_data()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        assert set(names) == {"manifest.json", "README.md"}
        assert all("_empty" not in name for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["included_files"] == []
        assert manifest["warnings"]
