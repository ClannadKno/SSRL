# -*- coding: utf-8 -*-
"""Normalized rule-based state scoring for the SSRL-ESP analysis pipeline."""
from datetime import datetime

from config import (
    AGENT_CONFLICT_QUICK_HITS,
    AGENT_OFFTASK_QUICK_HITS,
    CHECKIN_VALID_WINDOW_MINUTES,
    ONLINE_ACTIVE_SECONDS,
    ONLINE_LOW_INTERACTION_MSG_COUNT,
    ONLINE_LOW_INTERACTION_SPEAKERS,
    ONLINE_SILENCE_MIN_ACTIVE_MEMBERS,
    ONLINE_SILENCE_NO_MSG_SECONDS,
    SINGLE_SPEAKER_SILENCE_SHARE,
)
from knowledge_base import (
    CONFLICT_REPAIR_PHRASES,
    CONSTRUCTIVE_CONFLICT_WORDS,
    COORDINATION_CONFUSION_WORDS,
    CONSENSUS_SUMMARY_WORDS,
    EVIDENCE_COMPARISON_WORDS,
    EXECUTION_WORDS,
    FINAL_STATE_CODES,
    FRUSTRATION_WORDS,
    LOW_MOTIVATION_WORDS,
    OFF_TASK_WORDS,
    PASSIVE_DETACHMENT_WORDS,
    POSITIVE_WORDS,
    SELF_REGULATION_WORDS,
    STATE_META,
    STRONG_CONFLICT_WORDS,
    TASK_STRUCTURING_WORDS,
    count_destructive_conflict_hits,
    VALUE_DOUBT_WORDS,
    normalize_state_payload,
)

RULE_STATE_VERSION = "phase6_rule_norm_v1"
RULE_STATE_PRIORITY = [
    "conflict_tension",
    "negative_silence",
    "blocked_frustration",
    "task_detached",
    "positive_collaboration",
    "unknown",
]


def _parse_dt(value):
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


def _count_hits(text, words):
    return sum(1 for word in words if word and word in text)


def _text_has_any(text, words):
    return any(word and word in text for word in words)


def _clamp_score(value):
    return round(max(0.0, min(float(value), 1.0)), 3)


def _priority_index(state_code):
    try:
        return RULE_STATE_PRIORITY.index(state_code)
    except ValueError:
        return len(RULE_STATE_PRIORITY)


def _candidate_score(candidates, state_code):
    return float((candidates.get(state_code) or {}).get("score") or 0.0)


def _has_clear_task_detached_evidence(signals, text_features):
    recent_offtask = signals.get("recent_offtask") or {}
    return any([
        bool(recent_offtask.get("has_recent_offtask")),
        int(text_features.get("off_task_hits") or 0) >= AGENT_OFFTASK_QUICK_HITS,
        int(text_features.get("low_motivation_hits") or 0) >= 2,
        int(text_features.get("passive_detachment_hits") or 0) >= 2,
        int(signals.get("value_doubt_hits") or 0) >= 2,
    ])


def _opening_task_detached_guard(signals, text_features):
    if signals["msg_count"] >= 4 and signals["unique_speakers"] >= 2:
        return False
    return not _has_clear_task_detached_evidence(signals, text_features)


def _positive_gate_allows(candidates, signals, behavior, text_features):
    if _candidate_score(candidates, "positive_collaboration") < 0.55:
        return False
    if signals["unique_speakers"] < 2 or signals["msg_count"] < 3:
        return False
    if int(behavior.get("same_speaker_run_length") or 0) >= 3:
        return False
    if float(behavior.get("same_speaker_recent_share") or 0.0) >= 0.75:
        return False
    if signals.get("response_after_dominance") in {"none", "pushback"}:
        return False
    has_task_progress = any([
        signals["execution_hits"] >= 1,
        int(text_features.get("reasoning_hits") or 0) >= 1,
        int(text_features.get("evidence_hits") or 0) >= 1,
        int(text_features.get("summary_hits") or 0) >= 1,
        float(text_features.get("task_relevance_score") or 0.0) > 0,
        float(behavior.get("task_progress") or 0.0) > 0,
    ])
    has_followup = (
        int(text_features.get("agreement_hits") or 0) >= 1
        or signals["unique_speakers"] >= 3
        or int(text_features.get("summary_hits") or 0) >= 1
    )
    return has_task_progress and has_followup


def _select_winning_state(candidates, signals, behavior, text_features):
    if signals.get("online_no_text_silence") or signals.get("online_low_interaction_silence"):
        if _candidate_score(candidates, "negative_silence") >= 0.55:
            return "negative_silence"
    gated_thresholds = [
        ("conflict_tension", 0.55),
        ("negative_silence", 0.55),
        ("blocked_frustration", 0.55),
        ("task_detached", 0.55),
    ]
    for state_code, threshold in gated_thresholds:
        if _candidate_score(candidates, state_code) >= threshold:
            return state_code
    if _positive_gate_allows(candidates, signals, behavior, text_features):
        return "positive_collaboration"
    return "unknown"


def _row_signal_categories(content):
    categories = set()
    if _text_has_any(content, CONFLICT_REPAIR_PHRASES):
        categories.add("deescalation")
    if _text_has_any(content, SELF_REGULATION_WORDS):
        categories.add("self_regulation")
    if _text_has_any(content, CONSTRUCTIVE_CONFLICT_WORDS):
        categories.add("constructive_conflict")
    if _text_has_any(content, TASK_STRUCTURING_WORDS + EXECUTION_WORDS):
        categories.add("task_structuring")
    if _text_has_any(content, EVIDENCE_COMPARISON_WORDS):
        categories.add("evidence_comparison")
    if _text_has_any(content, CONSENSUS_SUMMARY_WORDS):
        categories.add("consensus_summary")
    return categories


def _is_constructive_disagreement(content, conflict_hits):
    if conflict_hits <= 0 or _text_has_any(content, STRONG_CONFLICT_WORDS):
        return False
    categories = _row_signal_categories(content)
    return bool(
        categories
        & {
            "constructive_conflict",
            "evidence_comparison",
            "task_structuring",
        }
    )


def analyze_recent_conflict_timeline(rows):
    """Return an ordered student-only conflict/recovery interpretation."""
    rows = [
        row
        for row in rows or []
        if str((row or {}).get("role") or "student").strip().lower()
        == "student"
        and str((row or {}).get("sender_type") or "student").strip().lower()
        not in {"agent", "teacher", "system"}
    ]
    if not rows:
        return {
            "has_recent_conflict": False,
            "has_conflict_history": False,
            "has_constructive_repair": False,
            "self_regulation_detected": False,
            "recovery_phase": "none",
            "recovery_completed": False,
            "conflict_hits": 0,
            "speaker_count": 0,
            "recovery_speaker_count": 0,
            "recovery_evidence_message_ids": [],
            "recovery_evidence_sequences": [],
            "recovery_signal_categories": [],
            "evidence": "",
        }

    snippets = []
    speakers = set()
    destructive_indexes = []
    constructive_disagreement_indexes = []
    row_categories = []
    total_hits = 0

    for index, row in enumerate(rows):
        content = str(row.get("content") or "").strip()
        speakers.add(row.get("user_id"))
        short = content[:60] + ("..." if len(content) > 60 else "")
        display_name = row.get("real_name") or row.get("username") or str(row.get("user_id"))
        snippets.append(f"{display_name}: {short}")
        hits = count_destructive_conflict_hits(content)
        categories = _row_signal_categories(content)
        row_categories.append(categories)
        if _is_constructive_disagreement(content, hits):
            constructive_disagreement_indexes.append(index)
        elif hits > 0:
            destructive_indexes.append(index)
            total_hits += hits

    if not destructive_indexes:
        return {
            "has_recent_conflict": False,
            "has_conflict_history": False,
            "has_constructive_repair": False,
            "self_regulation_detected": False,
            "recovery_phase": "none",
            "recovery_completed": False,
            "conflict_hits": 0,
            "speaker_count": len(speakers),
            "recovery_speaker_count": 0,
            "recovery_evidence_message_ids": [],
            "recovery_evidence_sequences": [],
            "recovery_signal_categories": [],
            "constructive_disagreement_count": len(
                constructive_disagreement_indexes
            ),
            "evidence": "recent_conflict=" + " | ".join(snippets[-5:]),
        }

    last_destructive = max(destructive_indexes)
    recovery_indexes = [
        index
        for index, categories in enumerate(row_categories)
        if index > last_destructive and categories
    ]
    recovery_categories = set()
    recovery_speakers = set()
    recovery_evidence_message_ids = []
    recovery_evidence_sequences = []
    for index in recovery_indexes:
        row = rows[index]
        recovery_categories.update(row_categories[index])
        speaker = row.get("user_id") or row.get("username") or row.get(
            "participant_code"
        )
        if speaker is not None:
            recovery_speakers.add(speaker)
        try:
            recovery_evidence_message_ids.append(int(row.get("id")))
        except (TypeError, ValueError):
            pass
        try:
            recovery_evidence_sequences.append(int(row.get("sequence")))
        except (TypeError, ValueError):
            pass

    explicit_repair = bool(
        recovery_categories
        & {
            "deescalation",
            "self_regulation",
            "constructive_conflict",
        }
    )
    progress_categories = recovery_categories & {
        "task_structuring",
        "evidence_comparison",
        "consensus_summary",
    }
    has_repair_after_conflict = bool(
        explicit_repair
        or (
            len(recovery_indexes) >= 3
            and len(recovery_speakers) >= 2
            and len(progress_categories) >= 2
        )
    )
    recovery_completed = bool(
        has_repair_after_conflict
        and len(recovery_indexes) >= 3
        and len(recovery_speakers) >= 2
        and len(progress_categories) >= 2
        and (explicit_repair or len(recovery_indexes) >= 4)
    )
    recovery_phase = (
        "completed"
        if recovery_completed
        else "observing"
        if has_repair_after_conflict
        else "none"
    )
    return {
        "has_recent_conflict": not recovery_completed,
        "has_conflict_history": True,
        "has_constructive_repair": has_repair_after_conflict,
        "self_regulation_detected": has_repair_after_conflict,
        "recovery_phase": recovery_phase,
        "recovery_completed": recovery_completed,
        "conflict_hits": total_hits,
        "speaker_count": len(speakers),
        "recovery_speaker_count": len(recovery_speakers),
        "recovery_evidence_message_ids": sorted(
            set(recovery_evidence_message_ids)
        ),
        "recovery_evidence_sequences": sorted(
            set(recovery_evidence_sequences)
        ),
        "recovery_signal_categories": sorted(recovery_categories),
        "constructive_disagreement_count": len(
            constructive_disagreement_indexes
        ),
        "last_destructive_message_id": rows[last_destructive].get("id"),
        "last_destructive_sequence": rows[last_destructive].get("sequence"),
        "evidence": "recent_conflict=" + " | ".join(snippets[-5:]),
    }


def _recent_conflict_signal(rows):
    return analyze_recent_conflict_timeline(rows)


def _recent_offtask_signal(rows):
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
        "说正事",
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
        content = str(row.get("content") or "").strip()
        speakers.add(row.get("user_id"))
        short = content[:50] + ("..." if len(content) > 50 else "")
        display_name = row.get("real_name") or row.get("username") or str(row.get("user_id"))
        snippets.append(f"{display_name}: {short}")
        if _count_hits(content, OFF_TASK_WORDS) > 0:
            offtask_indexes.append(index)
        if _text_has_any(content, return_words):
            return_indexes.append(index)

    if not offtask_indexes:
        return {
            "has_recent_offtask": False,
            "has_recent_return": False,
            "speaker_count": len(speakers),
            "evidence": "recent_messages=" + " | ".join(snippets[-4:]),
        }

    last_offtask = max(offtask_indexes)
    return {
        "has_recent_offtask": True,
        "has_recent_return": any(index > last_offtask for index in return_indexes),
        "speaker_count": len(speakers),
        "evidence": "recent_messages=" + " | ".join(snippets[-4:]),
    }


def _build_signal_bundle(context, features):
    behavior = features.get("behavior", {})
    text_features = features.get("text", {})
    window_messages = [row for row in context.get("window_messages", []) if row.get("role") != "agent"]
    student_messages = context.get("window_student_messages", [])
    recent_rows = context.get("recent_student_messages", [])
    checkins = context.get("recent_checkins", [])
    checkin_summary = context.get("checkin_summary", {})
    page_activity = context.get("page_activity") or {}
    participant_count = max(int(context.get("participant_count") or 0), 1)
    unique_speakers = len({
        row.get("user_id") or row.get("username") or row.get("participant_code")
        for row in student_messages
        if row.get("user_id") or row.get("username") or row.get("participant_code")
    })
    speaker_ratio = unique_speakers / participant_count if participant_count else 0.0
    active_students = int(page_activity.get("active_students") or 0)
    active_duration_seconds = int(page_activity.get("active_duration_seconds") or 0)
    msg_count = len(window_messages)
    low_msg_count = int(behavior.get("low_window_student_message_count") or 0)
    low_unique_speakers = int(behavior.get("low_window_active_members") or 0)
    silent_seconds = behavior.get("silence_seconds")
    silence_seconds = int(silent_seconds) if silent_seconds is not None else None
    last_student_message_dt = _parse_dt(context.get("last_student_message_time"))
    online_no_text_silence = (
        active_students >= ONLINE_SILENCE_MIN_ACTIVE_MEMBERS
        and (
            (
                last_student_message_dt is not None
                and silence_seconds is not None
                and silence_seconds >= ONLINE_SILENCE_NO_MSG_SECONDS
            )
            or (
                last_student_message_dt is None
                and active_duration_seconds >= ONLINE_SILENCE_NO_MSG_SECONDS
            )
        )
    )
    online_low_interaction_silence = (
        active_students >= ONLINE_SILENCE_MIN_ACTIVE_MEMBERS
        and active_duration_seconds >= ONLINE_SILENCE_NO_MSG_SECONDS
        and low_msg_count <= ONLINE_LOW_INTERACTION_MSG_COUNT
        and low_unique_speakers <= ONLINE_LOW_INTERACTION_SPEAKERS
    )
    full_text = " ".join(str(row.get("content") or "") for row in student_messages)
    recent_conflict = _recent_conflict_signal(recent_rows)
    recent_offtask = _recent_offtask_signal(recent_rows)
    dominant_option = checkin_summary.get("dominant_option") or "none"

    return {
        "behavior": behavior,
        "text": text_features,
        "msg_count": msg_count,
        "unique_speakers": unique_speakers,
        "speaker_ratio": round(speaker_ratio, 3),
        "participant_count": participant_count,
        "active_students": active_students,
        "active_duration_seconds": active_duration_seconds,
        "low_msg_count": low_msg_count,
        "low_unique_speakers": low_unique_speakers,
        "silent_seconds": silence_seconds,
        "online_no_text_silence": online_no_text_silence,
        "online_low_interaction_silence": online_low_interaction_silence,
        "single_speaker_silence": bool(behavior.get("single_speaker_silence")),
        "dominant_speaker_key": behavior.get("dominant_speaker_key"),
        "dominant_speaker_recent_count": int(behavior.get("dominant_speaker_recent_count") or 0),
        "dominant_speaker_recent_share": float(behavior.get("dominant_speaker_recent_share") or 0.0),
        "silent_active_peer_count": int(behavior.get("silent_active_peer_count") or 0),
        "single_speaker_silence_seconds_threshold": int(behavior.get("single_speaker_silence_seconds_threshold") or 0),
        "checkin_count": len(checkins),
        "avg_positivity": round(float(checkin_summary.get("avg_positivity") or 3.0), 2),
        "avg_engagement": round(float(checkin_summary.get("avg_engagement") or 3.0), 2),
        "avg_atmosphere": round(float(checkin_summary.get("avg_atmosphere") or 3.0), 2),
        "avg_expression": round(float(checkin_summary.get("avg_expression") or 3.0), 2),
        "dominant_option": dominant_option,
        "relevance_hits": 1 if float(text_features.get("task_relevance_score") or 0) > 0 else 0,
        "coordination_hits": max(
            _count_hits(full_text, COORDINATION_CONFUSION_WORDS),
            len(text_features.get("coordination_evidence_tags") or []),
        ),
        "coordination_evidence_tags": text_features.get("coordination_evidence_tags") or [],
        "evidence_tags": text_features.get("evidence_tags") or [],
        "execution_hits": max(
            int(text_features.get("execution_hits") or 0),
            _count_hits(full_text, EXECUTION_WORDS),
        ),
        "value_doubt_hits": _count_hits(full_text, VALUE_DOUBT_WORDS),
        "passive_detachment_hits": max(
            int(text_features.get("passive_detachment_hits") or 0),
            _count_hits(full_text, PASSIVE_DETACHMENT_WORDS),
        ),
        "same_speaker_run_length": int(behavior.get("same_speaker_run_length") or 0),
        "same_speaker_recent_share": float(behavior.get("same_speaker_recent_share") or 0.0),
        "recent_speaker_entropy": float(behavior.get("recent_speaker_entropy") or 0.0),
        "dominance_phrases_hits": max(
            int(behavior.get("dominance_phrases_hits") or 0),
            int(text_features.get("dominance_phrases_hits") or 0),
        ),
        "response_after_dominance": behavior.get("response_after_dominance"),
        "recent_conflict": recent_conflict,
        "recent_offtask": recent_offtask,
    }


def _empty_candidate(state_code):
    label, risk_level, risk_label = STATE_META[state_code]
    return {
        "state_code": state_code,
        "state_label": label,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "score": 0.0,
        "signals": [],
    }


def _bump(candidates, state_code, score, reason, value):
    if score <= 0:
        return
    normalized = normalize_state_payload(state_code, evidence_tags=[reason])
    normalized_code = normalized["state_code"]
    if normalized_code not in candidates:
        return
    candidate = candidates[normalized_code]
    candidate["score"] = _clamp_score(candidate["score"] + score)
    signal = {"reason": reason, "value": value, "score": _clamp_score(score)}
    if normalized.get("legacy_state_code"):
        signal["legacy_state_code"] = normalized["legacy_state_code"]
        signal["normalization_reason"] = normalized["normalization_reason"]
    candidate["signals"].append(signal)


def _message_id(row):
    try:
        return int(row.get("id"))
    except (TypeError, ValueError, AttributeError):
        return None


def _message_hits(row, word_groups):
    text = str((row or {}).get("content") or "")
    return any(_text_has_any(text, words) for words in word_groups)


def _rule_evidence_by_state(context):
    evidence = {
        "positive_collaboration": [],
        "conflict_tension": [],
        "blocked_frustration": [],
        "task_detached": [],
    }
    student_rows = [
        row for row in (context or {}).get("window_student_messages") or []
        if (row or {}).get("role", "student") == "student"
    ]
    for row in student_rows:
        msg_id = _message_id(row)
        if msg_id is None:
            continue
        content = str(row.get("content") or "")
        destructive_hits = count_destructive_conflict_hits(content)
        if destructive_hits > 0 and not _is_constructive_disagreement(
            content, destructive_hits
        ):
            evidence["conflict_tension"].append(msg_id)
        if _message_hits(row, (FRUSTRATION_WORDS, COORDINATION_CONFUSION_WORDS)):
            evidence["blocked_frustration"].append(msg_id)
        if _message_hits(row, (OFF_TASK_WORDS, LOW_MOTIVATION_WORDS, PASSIVE_DETACHMENT_WORDS, VALUE_DOUBT_WORDS)):
            evidence["task_detached"].append(msg_id)
        if _message_hits(
            row,
            (
                POSITIVE_WORDS,
                EXECUTION_WORDS,
                SELF_REGULATION_WORDS,
                CONSTRUCTIVE_CONFLICT_WORDS,
                CONFLICT_REPAIR_PHRASES,
                TASK_STRUCTURING_WORDS,
                EVIDENCE_COMPARISON_WORDS,
                CONSENSUS_SUMMARY_WORDS,
            ),
        ):
            evidence["positive_collaboration"].append(msg_id)
    return evidence


def _candidate_scores(candidates):
    return {
        state_code: _clamp_score((candidates.get(state_code) or {}).get("score") or 0.0)
        for state_code in FINAL_STATE_CODES
        if state_code != "unknown"
    }


def _reason_codes(candidate):
    codes = []
    for signal in (candidate or {}).get("signals") or []:
        reason = signal.get("reason")
        if reason and reason not in codes:
            codes.append(reason)
    return codes


def _feature_summary(signals, text_features):
    keys = (
        "msg_count",
        "unique_speakers",
        "participant_count",
        "speaker_ratio",
        "active_students",
        "silent_seconds",
        "online_no_text_silence",
        "online_low_interaction_silence",
        "same_speaker_run_length",
        "same_speaker_recent_share",
        "dominance_phrases_hits",
        "response_after_dominance",
        "coordination_hits",
        "execution_hits",
        "passive_detachment_hits",
    )
    summary = {key: signals.get(key) for key in keys if key in signals}
    for key in (
        "positive_hits",
        "conflict_hits",
        "frustration_hits",
        "low_motivation_hits",
        "off_task_hits",
        "agreement_hits",
        "reasoning_hits",
        "evidence_hits",
        "summary_hits",
        "task_relevance_score",
    ):
        if key in text_features:
            summary[key] = text_features.get(key)
    return summary


def detect_group_state_rule(context, features):
    signals = _build_signal_bundle(context, features)
    behavior = signals["behavior"]
    text_features = signals["text"]
    recent_conflict = signals["recent_conflict"]
    recent_offtask = signals["recent_offtask"]
    candidates = {
        state_code: _empty_candidate(state_code)
        for state_code in FINAL_STATE_CODES
    }

    conflict_hits = int(text_features.get("conflict_hits") or 0)
    off_task_hits = int(text_features.get("off_task_hits") or 0)
    frustration_hits = int(text_features.get("frustration_hits") or 0)
    low_motivation_hits = int(text_features.get("low_motivation_hits") or 0)
    positive_hits = int(text_features.get("positive_hits") or 0)
    agreement_hits = int(text_features.get("agreement_hits") or 0)
    reasoning_hits = int(text_features.get("reasoning_hits") or 0)
    evidence_hits = int(text_features.get("evidence_hits") or 0)
    task_progress = float(behavior.get("task_progress") or 0.0)
    constructive_conflict = (
        not recent_conflict.get("has_recent_conflict")
        and int(recent_conflict.get("constructive_disagreement_count") or 0)
        >= 1
        and signals["msg_count"] >= 4
        and (evidence_hits >= 1 or reasoning_hits >= 1)
        and (agreement_hits >= 1 or positive_hits >= 1)
    )

    if recent_conflict.get("has_recent_conflict"):
        if recent_conflict.get("recovery_phase") == "observing":
            _bump(
                candidates,
                "conflict_tension",
                0.55,
                "conflict_under_autonomous_regulation",
                recent_conflict["conflict_hits"],
            )
            _bump(
                candidates,
                "positive_collaboration",
                0.18,
                "autonomous_regulation_observed",
                recent_conflict.get("recovery_signal_categories"),
            )
        else:
            _bump(candidates, "conflict_tension", 0.55, "recent_conflict_detected", recent_conflict["conflict_hits"])
        if not recent_conflict.get("has_constructive_repair"):
            _bump(candidates, "conflict_tension", 0.25, "recent_conflict_unrepaired", True)
    elif recent_conflict.get("recovery_completed"):
        _bump(
            candidates,
            "positive_collaboration",
            0.48,
            "conflict_recovery_completed",
            {
                "speakers": recent_conflict.get("recovery_speaker_count"),
                "categories": recent_conflict.get(
                    "recovery_signal_categories"
                ),
            },
        )
    elif constructive_conflict:
        _bump(
            candidates,
            "conflict_tension",
            0.22,
            "constructive_conflict_observed",
            recent_conflict.get("constructive_disagreement_count"),
        )
        _bump(
            candidates,
            "positive_collaboration",
            0.32,
            "constructive_conflict_progress",
            True,
        )
    if conflict_hits >= AGENT_CONFLICT_QUICK_HITS:
        _bump(candidates, "conflict_tension", 0.18, "conflict_keyword_hits", conflict_hits)
    if signals["checkin_count"] and (
        signals["dominant_option"] == "conflict" or signals["avg_atmosphere"] <= 2.2
    ):
        _bump(candidates, "conflict_tension", 0.16, "conflict_checkin_signal", signals["dominant_option"])

    if recent_offtask.get("has_recent_offtask"):
        _bump(candidates, "task_detached", 0.45, "recent_offtask_detected", True)
        if not recent_offtask.get("has_recent_return"):
            _bump(candidates, "task_detached", 0.22, "recent_offtask_unrepaired", True)
    if low_motivation_hits >= 1 and (
        signals["msg_count"] >= 2 or (signals["checkin_count"] and signals["avg_engagement"] <= 3.2)
    ):
        _bump(candidates, "task_detached", 0.28, "low_motivation_signal", low_motivation_hits)
        if low_motivation_hits >= 3:
            _bump(
                candidates,
                "task_detached",
                min(0.22, (low_motivation_hits - 2) * 0.05),
                "low_motivation_escalation",
                low_motivation_hits,
            )
    if off_task_hits >= AGENT_OFFTASK_QUICK_HITS and signals["msg_count"] >= 1 and signals["relevance_hits"] == 0:
        _bump(candidates, "task_detached", 0.24, "offtask_without_relevance", off_task_hits)
    if (
        off_task_hits >= 2
        and signals["checkin_count"]
        and signals["avg_engagement"] <= 3.0
    ) or (
        signals["msg_count"] >= 5
        and signals["relevance_hits"] == 0
        and signals["checkin_count"]
        and signals["avg_engagement"] <= 3.0
    ):
        _bump(candidates, "task_detached", 0.16, "low_engagement_offtask_pattern", signals["avg_engagement"])
    if signals["value_doubt_hits"] >= 1:
        _bump(candidates, "task_detached", 0.08, "value_doubt_language", signals["value_doubt_hits"])
    if signals["passive_detachment_hits"] >= 1:
        _bump(candidates, "task_detached", 0.08, "passive_detachment_language", signals["passive_detachment_hits"])
    if (
        signals["passive_detachment_hits"] >= 2
        and signals["msg_count"] >= 2
    ):
        _bump(candidates, "task_detached", 0.62, "clear_passive_detachment_pattern", signals["passive_detachment_hits"])

    if signals["online_no_text_silence"]:
        _bump(candidates, "negative_silence", 0.64, "online_no_text_silence", signals["active_students"])
    if signals["online_low_interaction_silence"]:
        _bump(candidates, "negative_silence", 0.38, "online_low_interaction_silence", signals["low_msg_count"])
    if signals["single_speaker_silence"]:
        _bump(
            candidates,
            "negative_silence",
            0.58,
            "single_speaker_silence",
            {
                "dominant_share": signals["dominant_speaker_recent_share"],
                "silent_active_peers": signals["silent_active_peer_count"],
            },
        )
    if (
        signals["msg_count"] < 5
        and signals["checkin_count"]
        and (
            signals["avg_engagement"] <= 3.0
            or signals["avg_expression"] <= 3.0
            or signals["dominant_option"] in ["silent", "stuck"]
        )
    ):
        _bump(candidates, "negative_silence", 0.24, "negative_silence_checkin_signal", signals["dominant_option"])
    if signals["silent_seconds"] is not None and signals["silent_seconds"] >= ONLINE_SILENCE_NO_MSG_SECONDS:
        _bump(candidates, "negative_silence", 0.08, "silence_duration", signals["silent_seconds"])

    if frustration_hits >= 2:
        _bump(candidates, "blocked_frustration", 0.52, "frustration_keyword_hits", frustration_hits)
        if frustration_hits >= 4:
            _bump(
                candidates,
                "blocked_frustration",
                min(0.18, (frustration_hits - 2) * 0.04),
                "frustration_escalation",
                frustration_hits,
            )
    if frustration_hits >= 2 and low_motivation_hits >= 1:
        _bump(candidates, "blocked_frustration", 0.08, "frustration_with_low_motivation", low_motivation_hits)
    if signals["checkin_count"] and (
        signals["dominant_option"] == "frustrated" or signals["avg_positivity"] <= 2.4
    ):
        _bump(candidates, "blocked_frustration", 0.26, "frustration_checkin_signal", signals["dominant_option"])

    for tag in signals["coordination_evidence_tags"]:
        score = 0.22 if tag == "coordination_blocked" else 0.12
        _bump(candidates, "blocked_frustration", score, tag, True)
    if signals["coordination_hits"] >= 1 and signals["msg_count"] >= 2:
        _bump(candidates, "blocked_frustration", 0.42, "coordination_confusion_signal", signals["coordination_hits"])

    if (
        signals["msg_count"] >= 4
        and signals["speaker_ratio"] <= 0.25
        and signals["active_students"] >= ONLINE_SILENCE_MIN_ACTIVE_MEMBERS
    ):
        _bump(candidates, "unknown", 0.62, "participation_imbalance", signals["speaker_ratio"])
    elif (
        signals["msg_count"] >= 6
        and signals["speaker_ratio"] <= 0.5
        and signals["active_students"] >= ONLINE_SILENCE_MIN_ACTIVE_MEMBERS
    ):
        _bump(candidates, "unknown", 0.5, "participation_imbalance", signals["speaker_ratio"])
    imbalance_score = float(behavior.get("participation_imbalance") or 0.0)
    if imbalance_score > 0:
        _bump(candidates, "unknown", min(0.18, imbalance_score * 0.35), "participation_distribution", imbalance_score)
    if signals["same_speaker_run_length"] >= 3:
        _bump(candidates, "unknown", 0.24, "same_speaker_run_protection", signals["same_speaker_run_length"])
    if signals["same_speaker_recent_share"] >= 0.75 and signals["msg_count"] >= 4:
        _bump(candidates, "unknown", 0.2, "same_speaker_share_protection", signals["same_speaker_recent_share"])
    if signals["dominance_phrases_hits"] >= 1:
        if signals["response_after_dominance"] == "pushback":
            _bump(candidates, "conflict_tension", 0.55, "dominance_conflict", signals["dominance_phrases_hits"])
        elif signals["response_after_dominance"] == "none":
            _bump(candidates, "unknown", 0.28, "unconfirmed_dominance", signals["dominance_phrases_hits"])

    if signals["msg_count"] >= 4 and signals["speaker_ratio"] >= 0.5:
        _bump(candidates, "positive_collaboration", 0.26, "broad_participation", signals["speaker_ratio"])
    if positive_hits >= 1:
        _bump(candidates, "positive_collaboration", min(0.18, positive_hits * 0.08), "positive_language", positive_hits)
    if agreement_hits >= 1:
        _bump(candidates, "positive_collaboration", min(0.14, agreement_hits * 0.06), "agreement_language", agreement_hits)
    if reasoning_hits >= 1:
        _bump(candidates, "positive_collaboration", min(0.1, reasoning_hits * 0.04), "reasoning_language", reasoning_hits)
    if evidence_hits >= 1:
        _bump(candidates, "positive_collaboration", min(0.12, evidence_hits * 0.05), "evidence_language", evidence_hits)
    if signals["execution_hits"] >= 1:
        _bump(candidates, "positive_collaboration", 0.16, "execution_language", signals["execution_hits"])
    if float(text_features.get("task_relevance_score") or 0.0) > 0:
        _bump(candidates, "positive_collaboration", 0.1, "task_relevance", text_features.get("task_relevance_score"))
    if task_progress > 0:
        _bump(candidates, "positive_collaboration", min(0.12, task_progress * 0.18), "task_progress", task_progress)

    ranked = sorted(
        candidates.values(),
        key=lambda item: (-item["score"], _priority_index(item["state_code"])),
    )
    winning_state_code = _select_winning_state(candidates, signals, behavior, text_features)
    if (
        winning_state_code == "task_detached"
        and _opening_task_detached_guard(signals, text_features)
    ):
        _bump(
            candidates,
            "unknown",
            0.72,
            "opening_task_detached_protection",
            {
                "msg_count": signals["msg_count"],
                "unique_speakers": signals["unique_speakers"],
            },
        )
        winning_state_code = "unknown"
    winning_candidate = candidates.get(winning_state_code) or candidates["unknown"]
    other_best_score = max(
        [item["score"] for item in ranked if item["state_code"] != winning_state_code],
        default=0.0,
    )
    score_gap = round(max(0.0, winning_candidate["score"] - other_best_score), 3)
    if winning_state_code == "unknown":
        best_non_unknown_score = max(
            [item["score"] for item in ranked if item["state_code"] != "unknown"],
            default=0.0,
        )
        confidence = _clamp_score(
            max(candidates["unknown"]["score"], 0.05, 0.35 - best_non_unknown_score)
        )
        winning = dict(candidates["unknown"])
        winning["score"] = confidence
    else:
        confidence = _clamp_score(min(0.98, max(0.55, winning_candidate["score"]) * 0.8 + score_gap * 0.2))
        winning = dict(winning_candidate)
        winning["score"] = confidence

    assessment_status = "state_detected" if winning_state_code != "unknown" else "insufficient_evidence"
    best_non_unknown_score = max(
        [item["score"] for item in ranked if item["state_code"] != "unknown"],
        default=0.0,
    )
    unknown_score = _clamp_score(max(candidates["unknown"]["score"], 1.0 - best_non_unknown_score))
    candidates["unknown"]["score"] = unknown_score
    if not candidates["unknown"]["signals"]:
        candidates["unknown"]["signals"].append(
            {"reason": "fallback_unknown", "value": assessment_status, "score": unknown_score}
        )
    ranked_with_unknown = list(candidates.values())
    ranked_with_unknown = sorted(
        ranked_with_unknown,
        key=lambda item: (-item["score"], _priority_index(item["state_code"])),
    )

    compatibility_scores = _candidate_scores(candidates)
    compatibility_evidence = _rule_evidence_by_state(context)
    return {
        "version": RULE_STATE_VERSION,
        "assessment_status": assessment_status,
        "winning_state_code": winning_state_code,
        "winning_state_label": winning["state_label"],
        "winning_score": winning["score"],
        "score_gap": score_gap,
        "checkin_valid_window_minutes": CHECKIN_VALID_WINDOW_MINUTES,
        "online_active_seconds": ONLINE_ACTIVE_SECONDS,
        "candidates": ranked_with_unknown,
        "signals": {
            "msg_count": signals["msg_count"],
            "unique_speakers": signals["unique_speakers"],
            "participant_count": signals["participant_count"],
            "speaker_ratio": signals["speaker_ratio"],
            "active_students": signals["active_students"],
            "active_duration_seconds": signals["active_duration_seconds"],
            "low_msg_count": signals["low_msg_count"],
            "low_unique_speakers": signals["low_unique_speakers"],
            "silent_seconds": signals["silent_seconds"],
            "online_no_text_silence": signals["online_no_text_silence"],
            "online_low_interaction_silence": signals["online_low_interaction_silence"],
            "single_speaker_silence": signals["single_speaker_silence"],
            "dominant_speaker_recent_count": signals["dominant_speaker_recent_count"],
            "dominant_speaker_recent_share": signals["dominant_speaker_recent_share"],
            "silent_active_peer_count": signals["silent_active_peer_count"],
            "single_speaker_silence_share_threshold": SINGLE_SPEAKER_SILENCE_SHARE,
            "single_speaker_silence_seconds_threshold": signals["single_speaker_silence_seconds_threshold"],
            "avg_positivity": signals["avg_positivity"],
            "avg_engagement": signals["avg_engagement"],
            "avg_atmosphere": signals["avg_atmosphere"],
            "avg_expression": signals["avg_expression"],
            "dominant_option": signals["dominant_option"],
            "relevance_hits": signals["relevance_hits"],
            "coordination_hits": signals["coordination_hits"],
            "coordination_evidence_tags": signals["coordination_evidence_tags"],
            "evidence_tags": signals["evidence_tags"],
            "execution_hits": signals["execution_hits"],
            "passive_detachment_hits": signals["passive_detachment_hits"],
            "same_speaker_run_length": signals["same_speaker_run_length"],
            "same_speaker_recent_share": signals["same_speaker_recent_share"],
            "recent_speaker_entropy": signals["recent_speaker_entropy"],
            "dominance_phrases_hits": signals["dominance_phrases_hits"],
            "response_after_dominance": signals["response_after_dominance"],
            "recent_offtask": recent_offtask,
            "recent_conflict": recent_conflict,
        },
        "scores": compatibility_scores,
        "candidate_state": winning_state_code,
        "candidate_confidence": winning["score"],
        "evidence": compatibility_evidence,
        "feature_summary": _feature_summary(signals, text_features),
        "reason_codes": _reason_codes(winning_candidate),
        "self_regulation_detected": bool(
            recent_conflict.get("self_regulation_detected")
        ),
        "self_regulation_sub_type": (
            "conflict_recovery_completed"
            if recent_conflict.get("recovery_completed")
            else "conflict_recovery_observed"
            if recent_conflict.get("self_regulation_detected")
            else None
        ),
        "autonomous_regulation_reason": (
            "autonomous_regulation_observed"
            if recent_conflict.get("self_regulation_detected")
            else None
        ),
    }
