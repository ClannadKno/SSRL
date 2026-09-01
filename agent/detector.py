# -*- coding: utf-8 -*-
"""感知层：基于文本/情绪规则识别小组状态，含冲突与跑题上下文信号、文本计分等基础工具。"""
import re
import json
import time
from datetime import datetime, timedelta

from config import *
from db import *
from knowledge_base import *
from services.context_service import collect_group_context
from services.feature_service import extract_group_features
from services.rule_state_service import (
    analyze_recent_conflict_timeline,
    detect_group_state_rule,
)
from services.state_assessment_service import persist_state_assessment

def count_hits(text, words):
    return sum(1 for w in words if w in text)


def _coerce_text_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (list, tuple)):
            items = parsed
        elif parsed is not None:
            items = [parsed]
        else:
            items = re.split(r"[,，;；\n\r]+", text)
    result = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def get_active_task():
    task = get_current_learning_task()
    if task:
        return task
    legacy = query_one("SELECT * FROM tasks WHERE is_active=1 ORDER BY id DESC LIMIT 1")
    if not legacy:
        return None
    data = dict(legacy)
    data.setdefault("question", data.get("title") or "")
    data.setdefault("task_goal", data.get("description") or "")
    data.setdefault("output_requirement", "形成一份可提交的小组成果。")
    keywords = _coerce_text_list(data.get("keywords"))
    if not data.get("keywords"):
        data["keywords"] = ",".join(keywords)
    if not data.get("key_concepts"):
        data["key_concepts"] = keywords
    data.setdefault("expected_dimensions", [])
    data.setdefault("common_misconceptions", [])
    data.setdefault("acceptable_paths", [])
    data.setdefault("time_limit_minutes", 30)
    data.setdefault("remaining_minutes", get_current_task_remaining_minutes(data))
    return data


def task_relevance_score(text):
    task = get_active_task()
    if not task:
        return 0
    kws = _coerce_text_list(task.get("keywords") or task.get("key_concepts"))
    if not kws:
        return 0
    return sum(1 for k in kws if k in text)


def get_group_member_count(group_id):
    row = query_one("SELECT COUNT(*) AS c FROM group_members WHERE group_id=?", (group_id,))
    return row["c"] if row else 0



def _parse_dt(value):
    """兼容 SQLite 文本时间，解析失败则返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def get_online_activity_info(group_id, active_seconds=ONLINE_ACTIVE_SECONDS):
    """
    在线平台沉默识别用：统计当前小组最近 active_seconds 内仍在访问平台的学生。
    current_user() 会在每次 API 请求时刷新 client_sessions.last_seen；学生端轮询消息接口
    因此可作为“页面仍打开/学生仍在线”的近似信号。
    """
    since = (datetime.now() - timedelta(seconds=active_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    rows = query_all(
        """
        SELECT cs.user_id, MIN(cs.created_at) AS first_seen, MAX(cs.last_seen) AS last_seen
        FROM client_sessions cs
        JOIN group_members gm ON gm.user_id = cs.user_id
        JOIN users u ON u.id = cs.user_id
        WHERE gm.group_id=?
          AND u.role='student'
          AND cs.last_seen>=?
        GROUP BY cs.user_id
        """,
        (group_id, since),
    )
    active_students = len(rows)
    first_seen_values = [r["first_seen"] for r in rows if r["first_seen"]]
    last_seen_values = [r["last_seen"] for r in rows if r["last_seen"]]
    first_seen_dt = min([_parse_dt(v) for v in first_seen_values if _parse_dt(v)], default=None)
    last_seen_dt = max([_parse_dt(v) for v in last_seen_values if _parse_dt(v)], default=None)
    active_duration_seconds = None
    if first_seen_dt:
        active_duration_seconds = max(0, int((datetime.now() - first_seen_dt).total_seconds()))
    return {
        "active_students": active_students,
        "first_seen": first_seen_dt.strftime("%Y-%m-%d %H:%M:%S") if first_seen_dt else None,
        "last_seen": last_seen_dt.strftime("%Y-%m-%d %H:%M:%S") if last_seen_dt else None,
        "active_duration_seconds": active_duration_seconds,
    }


def get_last_student_message_time(group_id):
    row = query_one(
        """
        SELECT MAX(m.created_at) AS last_time
        FROM messages m
        JOIN users u ON u.id = m.user_id
        WHERE m.group_id=?
          AND u.role='student'
        """,
        (group_id,),
    )
    return row["last_time"] if row and row["last_time"] else None


def get_student_message_stats_since(group_id, since_text):
    rows = query_all(
        """
        SELECT m.user_id, m.content, m.created_at
        FROM messages m
        JOIN users u ON u.id = m.user_id
        WHERE m.group_id=?
          AND u.role='student'
          AND m.created_at>=?
        ORDER BY m.created_at ASC
        """,
        (group_id, since_text),
    )
    return {
        "count": len(rows),
        "speakers": len(set([r["user_id"] for r in rows])),
        "rows": rows,
    }


# -----------------------------
# v24 最近多轮上下文优先判断
# -----------------------------
def get_recent_student_rows_for_context(group_id, limit=8):
    rows = query_all(
        """
        SELECT m.*, u.real_name, u.username, u.role
        FROM messages m
        JOIN users u ON m.user_id=u.id
        WHERE m.group_id=? AND u.role='student'
        ORDER BY m.id DESC
        LIMIT ?
        """,
        (group_id, limit),
    )
    return list(reversed(rows))



def latest_conflict_context_signal(group_id=None, rows=None):
    """Inspect the latest student turns for conflict and repair signals."""
    if rows is None:
        if group_id is None:
            rows = []
        else:
            rows = get_recent_student_rows_for_context(group_id, limit=10)
    return analyze_recent_conflict_timeline(rows)


def latest_offtask_context_signal(group_id=None, rows=None):
    """Inspect the latest student turns for off-task drift and recovery signals."""
    if rows is None:
        if group_id is None:
            rows = []
        else:
            rows = get_recent_student_rows_for_context(group_id, limit=8)
    if not rows:
        return {
            "has_recent_offtask": False,
            "has_recent_return": False,
            "speaker_count": 0,
            "evidence": "",
        }

    return_words = [
        "回到任务",
        "回到主题",
        "回到正题",
        "跑题了",
        "拉回来",
        "说正经",
        "继续任务",
        "先确认",
        "目标设定",
        "行动步骤",
        "分工",
        "我负责",
        "你负责",
        "整理",
        "汇总",
        "提交",
        "方案",
        "成果",
    ]

    offtask_indexes = []
    return_indexes = []
    snippets = []
    speakers = set()

    for index, row in enumerate(rows):
        content = (row.get("content") or "").strip()
        speakers.add(row.get("user_id"))
        short = content[:50] + ("..." if len(content) > 50 else "")
        display_name = row.get("real_name") or row.get("username") or str(row.get("user_id"))
        snippets.append(f"{display_name}: {short}")
        if count_hits(content, OFF_TASK_WORDS) > 0:
            offtask_indexes.append(index)
        if text_has_any(content, return_words):
            return_indexes.append(index)

    if not offtask_indexes:
        return {
            "has_recent_offtask": False,
            "has_recent_return": False,
            "speaker_count": len(speakers),
            "evidence": "recent_messages=" + " | ".join(snippets[-4:]),
        }

    last_offtask = max(offtask_indexes)
    has_return_after_offtask = any(index > last_offtask for index in return_indexes)
    return {
        "has_recent_offtask": True,
        "has_recent_return": has_return_after_offtask,
        "speaker_count": len(speakers),
        "evidence": "recent_messages=" + " | ".join(snippets[-4:]),
    }


def analyze_group(group_id):
    """DEPRECATED: run legacy V1-style detection and persist state rows."""
    analysis_started_at = now_str()
    analysis_started_perf = time.perf_counter()
    runtime = get_runtime_message_context()
    context = collect_group_context(
        group_id,
        task_id=runtime.get("task_id"),
        session_no=runtime.get("session_no"),
    )
    features = extract_group_features(context)
    behavior = features["behavior"]
    text_features = features["text"]
    rule_assessment = detect_group_state_rule(context, features)
    signals = rule_assessment["signals"]
    recent_context = signals.get("recent_offtask", {})
    recent_conflict = signals.get("recent_conflict", {})

    state_code = rule_assessment["winning_state_code"]
    label, risk_level, risk_label = STATE_META[state_code]
    state_score = float(rule_assessment.get("winning_score") or 0.0)
    candidate_summary = ", ".join(
        f"{item['state_code']}={item['score']:.2f}"
        for item in rule_assessment.get("candidates", [])[:3]
    )
    silent_seconds = signals.get("silent_seconds")
    if silent_seconds is None:
        silent_desc = "no recent student text"
    else:
        silent_desc = f"silent_seconds={silent_seconds}"

    evidence = (
        f"assessment_status={rule_assessment.get('assessment_status')}; "
        f"rule_version={rule_assessment.get('version')}; "
        f"state_score={state_score:.2f}; score_gap={float(rule_assessment.get('score_gap') or 0.0):.2f}; "
        f"top_candidates={candidate_summary}; "
        f"msg_count_10m={signals.get('msg_count', 0)}; "
        f"unique_speakers={signals.get('unique_speakers', 0)}/{signals.get('participant_count', 1)}; "
        f"low_window_msgs={signals.get('low_msg_count', 0)}; low_window_speakers={signals.get('low_unique_speakers', 0)}; "
        f"active_students={signals.get('active_students', 0)}; active_duration_seconds={signals.get('active_duration_seconds', 0)}; {silent_desc}; "
        f"online_no_text_silence={signals.get('online_no_text_silence', False)}; "
        f"online_low_interaction_silence={signals.get('online_low_interaction_silence', False)}; "
        f"positive_hits={int(text_features.get('positive_hits') or 0)}; "
        f"frustration_hits={int(text_features.get('frustration_hits') or 0)}; "
        f"conflict_hits={int(text_features.get('conflict_hits') or 0)}; "
        f"off_task_hits={int(text_features.get('off_task_hits') or 0)}; "
        f"low_motivation_hits={int(text_features.get('low_motivation_hits') or 0)}; "
        f"coordination_hits={signals.get('coordination_hits', 0)}; "
        f"task_relevance_score={text_features.get('task_relevance_score', 0)}; "
        f"checkins={signals.get('checkin_count', 0)}; "
        f"avg_pos={float(signals.get('avg_positivity', 3.0)):.1f}; "
        f"avg_eng={float(signals.get('avg_engagement', 3.0)):.1f}; "
        f"avg_atm={float(signals.get('avg_atmosphere', 3.0)):.1f}; "
        f"avg_exp={float(signals.get('avg_expression', 3.0)):.1f}; "
        f"dominant_option={signals.get('dominant_option', 'none')}; "
        f"{recent_context.get('evidence', '')}; {recent_conflict.get('evidence', '')}"
    )

    analysis_finished_at = now_str()
    analysis_latency_ms = int((time.perf_counter() - analysis_started_perf) * 1000)

    persisted = persist_state_assessment(
        {
            "group_id": group_id,
            "state_code": state_code,
            "state_label": label,
            "risk_level": risk_level,
            "risk_label": risk_label,
            "state_score": round(state_score, 3),
            "evidence": evidence,
            "session_no": context.get("session_no"),
            "task_id": context.get("task_id"),
            "behavior_features": behavior,
            "text_features": text_features,
            "feature_json": features,
            "context_json": context,
            "rule_assessment": rule_assessment,
            "detector_version": "phase23_analysis_audit_v1",
            "analysis_started_at": analysis_started_at,
            "analysis_finished_at": analysis_finished_at,
            "analysis_latency_ms": analysis_latency_ms,
            "updated_at": now_str(),
        }
    )
    fusion = persisted["fusion"]
    confirmation = persisted["confirmation"]

    return {
        "group_id": group_id,
        "state_code": fusion["fused_state_code"],
        "state_label": fusion["fused_state_label"],
        "risk_level": fusion["risk_level"],
        "risk_label": fusion["risk_label"],
        "state_score": round(fusion["confidence"], 3),
        "evidence": persisted["evidence_summary"],
        "rule_state_code": state_code,
        "assessment_status": fusion["assessment_status"],
        "confirmed_windows": confirmation["confirmed_windows"],
        "confirmation_status": confirmation["confirmation_status"],
        "state_assessment_id": persisted["assessment_id"],
        "group_state_id": persisted["group_state_id"],
        "llm_state_code": fusion.get("llm_state_code"),
        "fusion_json": fusion,
        "msg_count_10m": signals.get("msg_count", 0),
        "unique_speakers_10m": signals.get("unique_speakers", 0),
        "member_count": signals.get("participant_count", 1),
        "avg_positivity": round(float(signals.get("avg_positivity", 3.0)), 2),
        "avg_engagement": round(float(signals.get("avg_engagement", 3.0)), 2),
        "avg_atmosphere": round(float(signals.get("avg_atmosphere", 3.0)), 2),
        "avg_expression": round(float(signals.get("avg_expression", 3.0)), 2),
        "positive_hits": int(text_features.get("positive_hits") or 0),
        "frustration_hits": int(text_features.get("frustration_hits") or 0),
        "conflict_hits": int(text_features.get("conflict_hits") or 0),
        "off_task_hits": int(text_features.get("off_task_hits") or 0),
        "low_motivation_hits": int(text_features.get("low_motivation_hits") or 0),
        "coordination_hits": signals.get("coordination_hits", 0),
        "relevance_hits": signals.get("relevance_hits", 0),
        "speaker_ratio": round(float(signals.get("speaker_ratio", 0.0)), 2),
        "dominant_option": signals.get("dominant_option", "none"),
        "has_fresh_checkins": bool(signals.get("checkin_count")),
        "checkin_valid_window_minutes": rule_assessment.get("checkin_valid_window_minutes", CHECKIN_VALID_WINDOW_MINUTES),
        "active_students": signals.get("active_students", 0),
        "online_active_seconds": rule_assessment.get("online_active_seconds", ONLINE_ACTIVE_SECONDS),
        "online_active_duration_seconds": signals.get("active_duration_seconds", 0),
        "last_student_message_time": context.get("last_student_message_time"),
        "silent_seconds": signals.get("silent_seconds"),
        "online_no_text_silence": signals.get("online_no_text_silence", False),
        "online_low_interaction_silence": signals.get("online_low_interaction_silence", False),
        "low_msg_count": signals.get("low_msg_count", 0),
        "low_unique_speakers": signals.get("low_unique_speakers", 0),
        "recent_has_offtask": recent_context.get("has_recent_offtask", False),
        "recent_has_return": recent_context.get("has_recent_return", False),
        "recent_speaker_count": recent_context.get("speaker_count", 0),
        "recent_has_conflict": recent_conflict.get("has_recent_conflict", False),
        "recent_conflict_repaired": recent_conflict.get("has_constructive_repair", False),
        "recent_conflict_hits": recent_conflict.get("conflict_hits", 0),
        "recent_conflict_speaker_count": recent_conflict.get("speaker_count", 0),
        "session_no": context.get("session_no"),
        "task_id": context.get("task_id"),
        "behavior_features": behavior,
        "text_features": text_features,
        "feature_json": features,
        "context_json": context,
        "rule_assessment": rule_assessment,
        "updated_at": now_str(),
    }

def latest_group_state(group_id, session_id=None):
    """Read the latest persisted group state without triggering analysis."""
    session_filter = ""
    params = [group_id]
    if session_id is not None:
        session_filter = " AND session_id=?"
        params.append(session_id)
    row = query_one(
        f"""
        SELECT *
        FROM group_states
        WHERE group_id=?{session_filter}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    )
    if row:
        return dict(row)
    return {
        "group_id": group_id,
        "state_code": "unknown",
        "state_label": "观察中",
        "risk_level": 0,
        "risk_label": "",
        "state_score": None,
        "confidence": None,
        "evidence": "",
        "assessment_status": "no_state",
        "confirmed_windows": 0,
        "confirmation_status": "",
        "state_assessment_id": None,
        "group_state_id": None,
        "read_only": True,
        "source": "group_states",
    }


def text_has_any(text, words):
    return any(w in text for w in words)


def summarize_context(rows):
    if not rows:
        return "近一段时间内暂无新的聊天记录。"
    parts = []
    for r in rows:
        content = (r["content"] or "").replace("\n", " ").strip()
        if len(content) > 140:
            content = content[:140] + "..."
        parts.append(f"{r['real_name']}：{content}")
    return "\n".join(parts)
