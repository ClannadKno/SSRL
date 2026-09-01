# -*- coding: utf-8 -*-
"""Feature extraction helpers for the SSRL-ESP analysis pipeline."""
import math
import re
from datetime import datetime

from config import (
    SINGLE_SPEAKER_SILENCE_MIN_ACTIVE,
    SINGLE_SPEAKER_SILENCE_MIN_MESSAGES,
    SINGLE_SPEAKER_SILENCE_SECONDS,
    SINGLE_SPEAKER_SILENCE_SHARE,
)
from knowledge_base import (
    CONFLICT_REPAIR_PHRASES,
    CONSTRUCTIVE_CONFLICT_WORDS,
    COORDINATION_CONFUSION_WORDS,
    CONSENSUS_SUMMARY_WORDS,
    EVIDENCE_COMPARISON_WORDS,
    EXECUTION_WORDS,
    FRUSTRATION_WORDS,
    LOW_MOTIVATION_WORDS,
    OFF_TASK_WORDS,
    PASSIVE_DETACHMENT_WORDS,
    POSITIVE_WORDS,
    SELF_REGULATION_WORDS,
    TASK_STRUCTURING_WORDS,
    count_destructive_conflict_hits,
)

AGREEMENT_WORDS = [
    "同意", "赞成", "没问题", "可以", "行", "好", "按这个", "一致", "赞同", "接受",
]
REASONING_WORDS = [
    "因为", "所以", "如果", "因此", "说明", "理由", "原因", "推导", "假设", "意味着",
]
EVIDENCE_WORDS = [
    "证据", "依据", "数据", "来源", "案例", "实验", "观察", "文献", "事实", "材料",
]
QUESTION_WORDS = [
    "吗", "呢", "为什么", "怎么", "如何", "是不是", "是否", "?", "？",
]
SUMMARY_WORDS = [
    "总结", "归纳", "结论", "梳理", "汇总", "概括", "回到", "下一步", "分工",
]


DOMINANCE_PHRASES = [
    "直接选",
    "我来定",
    "按我的",
    "听我的",
    "没必要展开",
    "大家只要",
    "我就按这个写",
    "后面补几句话",
]
ROLE_UNCLEAR_WORDS = ["谁负责", "谁来", "怎么分工", "没人管"]
PROCESS_UNCLEAR_WORDS = ["接下来干嘛", "下一步干嘛", "怎么推进", "流程", "顺序", "没有顺序"]
NO_NEXT_STEP_WORDS = ["没有明确下一步", "不知道干啥", "不知道怎么", "没思路", "没头绪"]
MISSING_RECORDING_WORDS = ["没人记录", "没人整理", "没人总结", "没有记录", "没有整理"]
EVIDENCE_BLOCK_WORDS = ["证据不够", "证据不足", "不知道怎么连", "不知道写哪部分", "材料不够"]

AGREEMENT_WORDS.extend(["同意", "赞成", "没问题", "可以", "行", "好", "一致", "接受"])
REASONING_WORDS.extend(["因为", "所以", "如果", "因此", "说明", "理由", "原因", "推导", "假设"])
EVIDENCE_WORDS.extend(["证据", "依据", "数据", "案例", "实验", "观察", "文献", "事实", "材料"])
QUESTION_WORDS.extend(["吗", "呢", "为什么", "怎么", "如何", "是不是", "是否"])
SUMMARY_WORDS.extend(["总结", "归纳", "结论", "梳理", "汇总", "概括", "回到", "下一步", "分工"])


def _count_hits(text, words):
    return sum(1 for word in words if word and word in text)


def _normalize_text(text):
    return re.sub(r"\s+", "", str(text or "").strip())


def _member_key(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _participant_key(participant):
    return (
        _member_key(participant.get("user_id"))
        or _member_key(participant.get("participant_code"))
        or _member_key(participant.get("username"))
    )


def _speaker_key(row):
    return (
        _member_key(row.get("user_id"))
        or _member_key(row.get("username"))
        or _member_key(row.get("participant_code"))
    )


def _recent_speaker_sequence(rows, limit=8):
    return [_speaker_key(row) for row in rows[-limit:] if _speaker_key(row) is not None]


def _same_speaker_run_length(sequence):
    if not sequence:
        return 0
    last = sequence[-1]
    run_length = 0
    for speaker in reversed(sequence):
        if speaker != last:
            break
        run_length += 1
    return run_length


def _same_speaker_recent_share(sequence):
    if not sequence:
        return 0.0
    counts = {}
    for speaker in sequence:
        counts[speaker] = counts.get(speaker, 0) + 1
    return round(max(counts.values()) / len(sequence), 3)


def _dominant_speaker(sequence):
    if not sequence:
        return None, 0, 0.0
    counts = {}
    first_index = {}
    for index, speaker in enumerate(sequence):
        counts[speaker] = counts.get(speaker, 0) + 1
        first_index.setdefault(speaker, index)
    speaker, count = max(counts.items(), key=lambda item: (item[1], -first_index[item[0]]))
    return speaker, count, round(count / len(sequence), 3)


def _speaker_entropy(sequence):
    if not sequence:
        return 0.0
    counts = {}
    for speaker in sequence:
        counts[speaker] = counts.get(speaker, 0) + 1
    entropy = 0.0
    for count in counts.values():
        share = count / len(sequence)
        entropy -= share * math.log(share, 2)
    max_entropy = math.log(max(1, len(counts)), 2)
    if max_entropy <= 0:
        return 0.0
    return round(entropy / max_entropy, 3)


def _response_after_dominance(rows):
    dominance_indexes = []
    for index, row in enumerate(rows):
        content = str(row.get("content") or "")
        if _count_hits(content, DOMINANCE_PHRASES) > 0:
            dominance_indexes.append(index)
    if not dominance_indexes:
        return None

    dominant_index = max(dominance_indexes)
    dominant_speaker = _speaker_key(rows[dominant_index])
    followups = [row for row in rows[dominant_index + 1:] if _speaker_key(row) != dominant_speaker]
    if not followups:
        return "none"

    followup_text = " ".join(str(row.get("content") or "") for row in followups)
    if count_destructive_conflict_hits(followup_text) > 0:
        return "pushback"
    if _count_hits(followup_text, AGREEMENT_WORDS + POSITIVE_WORDS) > 0:
        return "acknowledged"
    return "other_response"


def _coordination_evidence_tags(text):
    tags = []
    checks = [
        ("role_unclear", ROLE_UNCLEAR_WORDS),
        ("process_unclear", PROCESS_UNCLEAR_WORDS),
        ("no_next_step", NO_NEXT_STEP_WORDS),
        ("missing_recording", MISSING_RECORDING_WORDS),
        ("evidence_to_conclusion_block", EVIDENCE_BLOCK_WORDS),
    ]
    for tag, words in checks:
        if _count_hits(text, words) > 0:
            tags.append(tag)
    if tags or _count_hits(text, COORDINATION_CONFUSION_WORDS) > 0:
        if "coordination_blocked" not in tags:
            tags.insert(0, "coordination_blocked")
    return tags


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
    return None


def _message_lengths(rows):
    return [len(str(row.get("content") or "").strip()) for row in rows if str(row.get("content") or "").strip()]


def _message_intervals(rows):
    timestamps = [_parse_dt(row.get("created_at")) for row in rows if row.get("created_at")]
    timestamps = [value for value in timestamps if value]
    if len(timestamps) < 2:
        return []
    intervals = []
    for previous, current in zip(timestamps, timestamps[1:]):
        intervals.append(max(0.0, (current - previous).total_seconds()))
    return intervals


def _participant_distribution(context):
    counts = {}
    for participant in context.get("participants") or []:
        key = _participant_key(participant)
        if key:
            counts[key] = int(participant.get("message_count_10m") or 0)
    return counts


def _member_keys(context, rows):
    keys = []
    for participant in context.get("participants") or []:
        key = _participant_key(participant)
        if key and key not in keys:
            keys.append(key)
    for row in rows:
        key = _speaker_key(row)
        if key is not None and key not in keys:
            keys.append(key)
    return keys


def _count_by_member(rows, keys):
    counts = {key: 0 for key in keys}
    chars = {key: 0 for key in keys}
    for row in rows:
        key = _speaker_key(row)
        if key is None:
            continue
        counts.setdefault(key, 0)
        chars.setdefault(key, 0)
        counts[key] += 1
        chars[key] += len(str(row.get("content") or ""))
    return counts, chars


def _shares_by_member(counts, denominator):
    total = max(0, int(denominator or 0))
    return {
        key: round((int(value or 0) / total), 4) if total else 0.0
        for key, value in counts.items()
    }


def _participation_entropy(shares, base_count):
    base = max(2, int(base_count or 0))
    entropy = 0.0
    for share in shares.values():
        if share and share > 0:
            entropy -= share * math.log(share)
    return round(entropy / math.log(base), 4) if base > 1 else 0.0


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * float(percentile)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[int(rank)], 2)
    weight = rank - low
    return round((ordered[low] * (1 - weight)) + (ordered[high] * weight), 2)


def _messages_since_member_spoke(rows, key):
    count = 0
    for row in reversed(rows):
        if _speaker_key(row) == key:
            return count
        count += 1
    return count


def _messages_in_recent_window(rows, key, now_dt, seconds):
    result = 0
    for row in rows:
        if _speaker_key(row) != key:
            continue
        created = _parse_dt(row.get("created_at"))
        if created and (now_dt - created).total_seconds() <= seconds:
            result += 1
    return result


def _messages_per_minute_recent(rows, now_dt, seconds):
    count = 0
    for row in rows:
        created = _parse_dt(row.get("created_at"))
        if created and (now_dt - created).total_seconds() <= seconds:
            count += 1
    return round(count / max(1, seconds / 60.0), 3)


def _turn_switch_rate(sequence):
    if len(sequence) < 2:
        return 0.0
    switches = sum(1 for previous, current in zip(sequence, sequence[1:]) if previous != current)
    return round(switches / (len(sequence) - 1), 4)


def _same_speaker_streak_max(sequence):
    if not sequence:
        return 0
    longest = 1
    current = 1
    for previous, speaker in zip(sequence, sequence[1:]):
        if speaker == previous:
            current += 1
        else:
            longest = max(longest, current)
            current = 1
    return max(longest, current)


def _task_keywords(task):
    if not task:
        return []
    keywords = []
    for field in ("key_concepts", "expected_dimensions"):
        for item in task.get(field) or []:
            text = str(item or "").strip()
            if text:
                keywords.append(text)
    for field in ("title", "question"):
        text = str(task.get(field) or "").strip()
        if text:
            keywords.append(text)
    seen = []
    for keyword in keywords:
        if keyword not in seen:
            seen.append(keyword)
    return seen[:20]


def _task_progress_score(progress):
    if not progress:
        return 0.0
    latest = progress.get("latest_submission") or {}
    score = 0.0
    if progress.get("submission_count"):
        score += 0.35
    score += min(0.45, int(latest.get("content_length") or 0) / 800.0)
    if latest.get("has_file"):
        score += 0.20
    return round(min(1.0, score), 3)


def extract_behavior_features(context):
    now_dt = _parse_dt(context.get("window_end")) or datetime.now()
    window_messages = context.get("window_messages") or []
    student_rows = context.get("window_student_messages") or []
    low_window_student_rows = context.get("low_window_student_messages") or []
    participant_distribution = _participant_distribution(context)
    student_message_total = max(1, len(student_rows))
    student_lengths = _message_lengths(student_rows)
    normalized_messages = [_normalize_text(row.get("content")) for row in student_rows if _normalize_text(row.get("content"))]
    seen_messages = set()
    repeated_count = 0
    for message in normalized_messages:
        if message in seen_messages:
            repeated_count += 1
        else:
            seen_messages.add(message)
    unique_student_speakers = {_speaker_key(row) for row in student_rows if _speaker_key(row) is not None}
    recent_speaker_sequence = _recent_speaker_sequence(student_rows)
    combined_student_text = " ".join(str(row.get("content") or "") for row in student_rows)
    active_member_count = max(
        len(unique_student_speakers),
        int(context.get("page_activity", {}).get("active_students") or 0),
    )
    intervals = _message_intervals(window_messages)
    average_interval = round(sum(intervals) / len(intervals), 2) if intervals else None
    member_keys = _member_keys(context, student_rows)
    member_count_base = int(context.get("participant_count") or len(member_keys) or 4)
    message_count_by_member, character_count_by_member = _count_by_member(
        student_rows,
        member_keys,
    )
    message_share_by_member = _shares_by_member(
        message_count_by_member,
        len(student_rows),
    )
    total_character_count = sum(character_count_by_member.values())
    character_share_by_member = _shares_by_member(
        character_count_by_member,
        total_character_count,
    )
    participation_entropy = _participation_entropy(
        message_share_by_member,
        member_count_base,
    )
    full_speaker_sequence = [
        _speaker_key(row) for row in student_rows if _speaker_key(row) is not None
    ]
    median_interval = _percentile(intervals, 0.5)
    p90_interval = _percentile(intervals, 0.9)
    burst_message_ratio = round(
        sum(1 for interval in intervals if interval <= 15.0) / len(intervals),
        4,
    ) if intervals else 0.0
    last_student_time = _parse_dt(context.get("last_student_message_time"))
    silence_seconds = None
    if last_student_time:
        silence_seconds = max(0, int((now_dt - last_student_time).total_seconds()))
    elif context.get("page_activity", {}).get("last_seen"):
        last_seen = _parse_dt(context["page_activity"]["last_seen"])
        if last_seen:
            silence_seconds = max(0, int((now_dt - last_seen).total_seconds()))
    individual_silence = {}
    messages_in_last_5_minutes = {}
    messages_in_last_10_minutes = {}
    consecutive_without_member = {}
    for participant in context.get("participants") or []:
        key = _participant_key(participant)
        if not key:
            continue
        participant_time = _parse_dt(participant.get("last_message_at"))
        individual_silence[key] = (
            max(0, int((now_dt - participant_time).total_seconds()))
            if participant_time
            else None
        )
    for key in member_keys:
        if key in individual_silence:
            continue
        member_times = [
            _parse_dt(row.get("created_at"))
            for row in student_rows
            if _speaker_key(row) == key
        ]
        member_times = [value for value in member_times if value]
        individual_silence[key] = (
            max(0, int((now_dt - max(member_times)).total_seconds()))
            if member_times
            else None
        )
    for key in member_keys:
        messages_in_last_5_minutes[key] = _messages_in_recent_window(
            student_rows,
            key,
            now_dt,
            5 * 60,
        )
        messages_in_last_10_minutes[key] = _messages_in_recent_window(
            student_rows,
            key,
            now_dt,
            10 * 60,
        )
        consecutive_without_member[key] = _messages_since_member_spoke(
            student_rows,
            key,
        )
    dominant_speaker_key, dominant_speaker_recent_count, dominant_speaker_recent_share = _dominant_speaker(
        recent_speaker_sequence
    )
    active_participants = [
        participant
        for participant in (context.get("participants") or [])
        if participant.get("active_on_page") or int(participant.get("active_session_count") or 0) > 0
    ]
    silent_active_peer_count = 0
    if dominant_speaker_key is not None and active_participants:
        dominant_key_text = str(dominant_speaker_key)
        for participant in active_participants:
            participant_keys = {
                str(value)
                for value in (
                    participant.get("user_id"),
                    participant.get("username"),
                    participant.get("participant_code"),
                    participant.get("real_name"),
                )
                if value is not None
            }
            if dominant_key_text in participant_keys:
                continue
            primary_key = participant.get("participant_code") or participant.get("username") or str(participant.get("user_id"))
            silence_value = individual_silence.get(primary_key)
            has_no_recent_text = int(participant.get("recent_message_count") or 0) <= 0
            has_no_window_text = int(participant.get("message_count_10m") or 0) <= 0
            if silence_value is None:
                if has_no_recent_text and has_no_window_text:
                    silent_active_peer_count += 1
            elif silence_value >= SINGLE_SPEAKER_SILENCE_SECONDS:
                silent_active_peer_count += 1
    single_speaker_silence = (
        bool(active_participants)
        and len(recent_speaker_sequence) >= SINGLE_SPEAKER_SILENCE_MIN_MESSAGES
        and active_member_count >= SINGLE_SPEAKER_SILENCE_MIN_ACTIVE
        and dominant_speaker_recent_share >= SINGLE_SPEAKER_SILENCE_SHARE
        and silent_active_peer_count >= max(1, SINGLE_SPEAKER_SILENCE_MIN_ACTIVE - 1)
    )
    active_distribution = [count for count in participant_distribution.values() if count > 0]
    if active_distribution and sum(active_distribution) > 0:
        max_share = max(active_distribution) / sum(active_distribution)
        ideal_share = 1 / max(len(participant_distribution), 1)
        participation_imbalance = round(min(1.0, max(0.0, max_share - ideal_share)), 3)
    else:
        participation_imbalance = 0.0
    messages_per_minute = round(len(window_messages) / max(1, int(context.get("window_minutes") or 1)), 3)
    rate_score = min(1.0, messages_per_minute / 2.0)
    speaker_score = min(1.0, active_member_count / max(1, int(context.get("participant_count") or 1)))
    interval_score = 0.0 if average_interval is None else max(0.0, min(1.0, 1 - (average_interval / 180.0)))
    interaction_intensity = round((rate_score * 0.4) + (speaker_score * 0.4) + (interval_score * 0.2), 3)
    progress = context.get("current_progress") or {}
    editing_activity = round(
        min(
            1.0,
            (int(progress.get("recent_submission_count") or 0) * 0.6)
            + (0.4 if progress.get("has_submission") else 0.0),
        ),
        3,
    )
    return {
        "message_count": len(window_messages),
        "student_message_count": len(student_rows),
        "active_member_count": active_member_count,
        "unique_student_speakers": len(unique_student_speakers),
        "message_count_by_member": message_count_by_member,
        "message_share_by_member": message_share_by_member,
        "character_count_by_member": character_count_by_member,
        "character_share_by_member": character_share_by_member,
        "participation_entropy": participation_entropy,
        "dominant_member_share": max(message_share_by_member.values()) if message_share_by_member else 0.0,
        "minimum_member_share": min(message_share_by_member.values()) if message_share_by_member else 0.0,
        "member_participation_imbalance": round(1.0 - participation_entropy, 4),
        "participation_distribution": participant_distribution,
        "participation_imbalance": participation_imbalance,
        "recent_speaker_sequence": recent_speaker_sequence,
        "dominant_speaker_key": dominant_speaker_key,
        "dominant_speaker_recent_count": dominant_speaker_recent_count,
        "dominant_speaker_recent_share": dominant_speaker_recent_share,
        "silent_active_peer_count": silent_active_peer_count,
        "single_speaker_silence": single_speaker_silence,
        "single_speaker_silence_seconds_threshold": SINGLE_SPEAKER_SILENCE_SECONDS,
        "same_speaker_run_length": _same_speaker_run_length(recent_speaker_sequence),
        "same_speaker_recent_share": _same_speaker_recent_share(recent_speaker_sequence),
        "recent_speaker_entropy": _speaker_entropy(recent_speaker_sequence),
        "consecutive_group_messages_without_member": consecutive_without_member,
        "dominance_phrases_hits": _count_hits(combined_student_text, DOMINANCE_PHRASES),
        "response_after_dominance": _response_after_dominance(student_rows),
        "messages_per_minute": messages_per_minute,
        "messages_per_minute_1m": _messages_per_minute_recent(student_rows, now_dt, 60),
        "messages_per_minute_5m": _messages_per_minute_recent(student_rows, now_dt, 5 * 60),
        "average_message_interval": average_interval,
        "mean_message_interval": average_interval,
        "median_message_interval": median_interval,
        "p90_message_interval": p90_interval,
        "turn_switch_rate": _turn_switch_rate(full_speaker_sequence),
        "same_speaker_streak_max": _same_speaker_streak_max(full_speaker_sequence),
        "burst_message_ratio": burst_message_ratio,
        "silence_seconds": silence_seconds,
        "individual_silence_seconds": individual_silence,
        "seconds_since_last_message": individual_silence,
        "messages_in_last_5_minutes": messages_in_last_5_minutes,
        "messages_in_last_10_minutes": messages_in_last_10_minutes,
        "average_message_length": round(sum(student_lengths) / len(student_lengths), 2) if student_lengths else 0.0,
        "short_message_ratio": round(
            sum(1 for length in student_lengths if length <= 12) / len(student_lengths),
            3,
        ) if student_lengths else 0.0,
        "repeated_message_ratio": round(repeated_count / student_message_total, 3),
        "interaction_intensity_score": interaction_intensity,
        "editing_activity": editing_activity,
        "task_progress": _task_progress_score(progress),
        "low_window_student_message_count": len(low_window_student_rows),
        "low_window_active_members": len({row["user_id"] for row in low_window_student_rows}),
    }


def extract_text_features(context):
    analysis_rows = context.get("recent_student_messages") or context.get("window_student_messages") or []
    combined_text = " ".join(str(row.get("content") or "") for row in analysis_rows)
    normalized_messages = [_normalize_text(row.get("content")) for row in analysis_rows if _normalize_text(row.get("content"))]
    duplicate_ratio = 0.0
    if normalized_messages:
        duplicate_ratio = round(
            max(0.0, 1 - (len(set(normalized_messages)) / len(normalized_messages))),
            3,
        )
    task_keywords = _task_keywords(context.get("current_task"))
    relevance_hits = sum(1 for keyword in task_keywords if keyword and keyword in combined_text)
    task_relevance_score = round(
        min(1.0, relevance_hits / max(1, min(len(task_keywords), 6))),
        3,
    ) if task_keywords else 0.0
    positive_hits = _count_hits(combined_text, POSITIVE_WORDS)
    conflict_hits = sum(
        count_destructive_conflict_hits(row.get("content"))
        for row in analysis_rows
    )
    conflict_repair_hits = sum(
        1
        for row in analysis_rows
        if _count_hits(str(row.get("content") or ""), CONFLICT_REPAIR_PHRASES) > 0
    )
    self_regulation_hits = sum(
        1
        for row in analysis_rows
        if _count_hits(str(row.get("content") or ""), SELF_REGULATION_WORDS) > 0
    )
    constructive_conflict_hits = sum(
        1
        for row in analysis_rows
        if _count_hits(str(row.get("content") or ""), CONSTRUCTIVE_CONFLICT_WORDS) > 0
    )
    task_structuring_hits = sum(
        1
        for row in analysis_rows
        if _count_hits(
            str(row.get("content") or ""),
            TASK_STRUCTURING_WORDS + EXECUTION_WORDS,
        ) > 0
    )
    evidence_comparison_hits = sum(
        1
        for row in analysis_rows
        if _count_hits(str(row.get("content") or ""), EVIDENCE_COMPARISON_WORDS) > 0
    )
    consensus_summary_hits = sum(
        1
        for row in analysis_rows
        if _count_hits(str(row.get("content") or ""), CONSENSUS_SUMMARY_WORDS) > 0
    )
    frustration_hits = _count_hits(combined_text, FRUSTRATION_WORDS)
    low_motivation_hits = _count_hits(combined_text, LOW_MOTIVATION_WORDS)
    off_task_hits = _count_hits(combined_text, OFF_TASK_WORDS)
    passive_detachment_hits = _count_hits(combined_text, PASSIVE_DETACHMENT_WORDS)
    execution_hits = _count_hits(combined_text, EXECUTION_WORDS)
    dominance_phrases_hits = _count_hits(combined_text, DOMINANCE_PHRASES)
    agreement_hits = _count_hits(combined_text, AGREEMENT_WORDS)
    reasoning_hits = _count_hits(combined_text, REASONING_WORDS)
    evidence_hits = _count_hits(combined_text, EVIDENCE_WORDS)
    question_hits = _count_hits(combined_text, QUESTION_WORDS)
    summary_hits = _count_hits(combined_text, SUMMARY_WORDS)
    negative_hits = conflict_hits + frustration_hits + low_motivation_hits
    denominator = positive_hits + negative_hits
    polarity = 0.0
    if denominator:
        polarity = round((positive_hits - negative_hits) / denominator, 3)
    coordination_evidence_tags = _coordination_evidence_tags(combined_text)
    evidence_tags = list(coordination_evidence_tags)
    if passive_detachment_hits and "passive_withdrawal" not in evidence_tags:
        evidence_tags.append("passive_withdrawal")
    if low_motivation_hits and "low_motivation" not in evidence_tags:
        evidence_tags.append("low_motivation")
    if off_task_hits and "off_task_topic" not in evidence_tags:
        evidence_tags.append("off_task_topic")
    if conflict_hits and "direct_disagreement" not in evidence_tags:
        evidence_tags.append("direct_disagreement")
    if conflict_repair_hits and "conflict_repair" not in evidence_tags:
        evidence_tags.append("conflict_repair")
    if self_regulation_hits and "self_regulation" not in evidence_tags:
        evidence_tags.append("self_regulation")
    if constructive_conflict_hits and "constructive_conflict" not in evidence_tags:
        evidence_tags.append("constructive_conflict")
    if dominance_phrases_hits and "dominance_phrases" not in evidence_tags:
        evidence_tags.append("dominance_phrases")
    return {
        "positive_hits": positive_hits,
        "conflict_hits": conflict_hits,
        "conflict_repair_hits": conflict_repair_hits,
        "self_regulation_hits": self_regulation_hits,
        "constructive_conflict_hits": constructive_conflict_hits,
        "task_structuring_hits": task_structuring_hits,
        "evidence_comparison_hits": evidence_comparison_hits,
        "consensus_summary_hits": consensus_summary_hits,
        "frustration_hits": frustration_hits,
        "low_motivation_hits": low_motivation_hits,
        "off_task_hits": off_task_hits,
        "passive_detachment_hits": passive_detachment_hits,
        "execution_hits": execution_hits,
        "dominance_phrases_hits": dominance_phrases_hits,
        "agreement_hits": agreement_hits,
        "reasoning_hits": reasoning_hits,
        "evidence_hits": evidence_hits,
        "question_hits": question_hits,
        "summary_hits": summary_hits,
        "task_relevance_score": task_relevance_score,
        "semantic_repetition_score": duplicate_ratio,
        "affective_polarity_score": polarity,
        "coordination_evidence_tags": coordination_evidence_tags,
        "evidence_tags": evidence_tags,
        "analysis_message_count": len(analysis_rows),
    }


def extract_group_features(context):
    return {
        "behavior": extract_behavior_features(context),
        "text": extract_text_features(context),
    }
