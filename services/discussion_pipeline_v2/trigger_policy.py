# -*- coding: utf-8 -*-
"""TriggerPolicy：拆分状态确认 LLM 门控和策略复核候选判断。"""

from config import (
    PIPELINE_V2_ANALYZER_VERSION,
    SERA_LLM_ENABLED,
    STATE_LLM_ENABLED,
    STATE_LLM_FORCE_AFTER_NEW_MESSAGES,
    STATE_LLM_GATE_MIN_RULE_SCORE,
    STATE_LLM_MIN_NEW_MESSAGES,
)
from db import query_one
from services.discussion_pipeline_v2.monitor_run_repo import MonitorRunRepo


STUDENT_HELP_TRIGGER_TYPES = {"student_help", "student_help_request"}
STATE_DETECTOR_RISK_STATES = {"conflict_tension", "blocked_frustration", "task_detached"}
STRATEGY_REVIEW_STATES = {"conflict_tension", "negative_silence", "blocked_frustration", "task_detached"}


def _count_student_messages_since(group_id: int, since_sequence: int = None) -> int:
    """统计指定 sequence 之后的学生消息数量。"""
    if since_sequence is None:
        row = query_one(
            """
            SELECT COUNT(*) AS c FROM messages m
            JOIN users u ON u.id=m.user_id
            WHERE m.group_id=? AND u.role='student'
            """,
            (group_id,),
        )
        return int(row["c"]) if row else 0
    row = query_one(
        """
        SELECT COUNT(*) AS c FROM messages m
        JOIN users u ON u.id=m.user_id
        WHERE m.group_id=? AND u.role='student' AND (m.sequence IS NULL OR m.sequence>?)
        """,
        (group_id, since_sequence),
    )
    return int(row["c"]) if row else 0


def _score_map(rule_assessment: dict) -> dict:
    scores = {}
    if isinstance((rule_assessment or {}).get("scores"), dict):
        for state_code, value in rule_assessment.get("scores", {}).items():
            try:
                scores[state_code] = float(value or 0.0)
            except (TypeError, ValueError):
                scores[state_code] = 0.0
    for candidate in (rule_assessment or {}).get("candidates") or []:
        state_code = candidate.get("state_code")
        if not state_code:
            continue
        if candidate.get("state_code") == state_code:
            try:
                scores[state_code] = max(scores.get(state_code, 0.0), float(candidate.get("score") or 0.0))
            except (TypeError, ValueError):
                scores.setdefault(state_code, 0.0)
    return scores


def _top_non_unknown_scores(scores: dict) -> list:
    ranked = [
        (state_code, float(score or 0.0))
        for state_code, score in (scores or {}).items()
        if state_code != "unknown"
    ]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def _has_ambiguous_candidate(rule_assessment: dict, scores: dict) -> bool:
    ranked = _top_non_unknown_scores(scores)
    if len(ranked) < 2:
        return False
    top_state, top_score = ranked[0]
    second_state, second_score = ranked[1]
    if top_score < 0.15 or second_score <= 0:
        return False
    try:
        score_gap = float((rule_assessment or {}).get("score_gap", top_score - second_score))
    except (TypeError, ValueError):
        score_gap = top_score - second_score
    return abs(top_score - second_score) <= 0.15 or score_gap <= 0.15


def _has_valid_student_text(context: dict) -> bool:
    rows = (
        (context or {}).get("state_detector_messages")
        or (context or {}).get("recent_student_messages")
        or (context or {}).get("window_student_messages")
        or []
    )
    for row in rows:
        if (row.get("role") or "student") == "agent":
            continue
        if str(row.get("content") or "").strip():
            return True
    return False


def _has_multi_member_coordination_risk(context: dict, rule_assessment: dict, scores: dict) -> bool:
    signals = (rule_assessment or {}).get("signals") or {}
    rows = (
        (context or {}).get("recent_student_messages")
        or (context or {}).get("window_student_messages")
        or []
    )[-6:]
    speakers = {
        row.get("user_id") or row.get("username") or row.get("participant_code")
        for row in rows
        if str(row.get("content") or "").strip()
    }
    speakers.discard(None)
    if len(speakers) < 2:
        return False
    risk_signal = any(
        [
            int(signals.get("coordination_hits") or 0) >= 1,
            int(signals.get("passive_detachment_hits") or 0) >= 2,
            int(((signals.get("recent_conflict") or {}).get("conflict_hits")) or 0) >= 1,
            bool((signals.get("recent_offtask") or {}).get("has_recent_offtask")),
        ]
    )
    if not risk_signal:
        return False
    max_risk_score = max(
        [float((scores or {}).get(state) or 0.0) for state in STATE_DETECTOR_RISK_STATES] or [0.0]
    )
    return max_risk_score < 0.55


def _gate_result(gate: bool, reason: str, *, scores: dict, new_student_message_count: int) -> dict:
    max_rule_score = max([float(value or 0.0) for value in (scores or {}).values()] or [0.0])
    return {
        "gate": bool(gate),
        "gate_reason": reason,
        "max_rule_score": round(max_rule_score, 3),
        "new_student_message_count": int(new_student_message_count or 0),
    }


def _strategy_state_and_confidence(rule_assessment=None, final_state=None, confidence=None):
    state_code = final_state or (rule_assessment or {}).get("winning_state_code") or "unknown"
    if confidence is None:
        confidence = (rule_assessment or {}).get("winning_score", 0.0)
    try:
        confidence = float(confidence or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return state_code, confidence


def _has_completed_same_cutoff(group_id: int, cutoff_sequence: int, trigger_type: str = None) -> bool:
    row = query_one(
        """
        SELECT id
        FROM monitor_runs
        WHERE group_id=? AND cutoff_sequence=? AND analyzer_version=?
          AND status='completed'
          AND (? IS NULL OR COALESCE(trigger_type, 'new_message')=?)
        LIMIT 1
        """,
        (group_id, cutoff_sequence, PIPELINE_V2_ANALYZER_VERSION, trigger_type, trigger_type),
    )
    return bool(row)


def _is_stage1_internal_lock(group_id: int, cutoff_sequence: int, context: dict = None) -> bool:
    """Return true when the current room lock belongs to a stage-1 pipeline run."""

    group = query_one(
        """
        SELECT state, lock_token, active_intervention_run_id
        FROM groups
        WHERE id=?
        """,
        (group_id,),
    )
    if not group or group["state"] != "AI_INTERVENING":
        return False
    owner = group["active_intervention_run_id"]
    try:
        owner = int(owner)
    except (TypeError, ValueError):
        return False
    if owner >= 0 or not group["lock_token"]:
        return False
    pipeline_run_id = abs(owner)
    scope = context or {}
    row = query_one(
        """
        SELECT id
        FROM strategy_pipeline_runs
        WHERE id=?
          AND group_id=?
          AND room_lock_token=?
          AND input_cutoff_student_sequence<=?
          AND stage1_status='SUCCEEDED'
          AND COALESCE(final_status, '')='LOCKED'
          AND (? IS NULL OR session_id IS NULL OR session_id=?)
          AND (? IS NULL OR discussion_id IS NULL OR discussion_id=?)
        LIMIT 1
        """,
        (
            pipeline_run_id,
            group_id,
            group["lock_token"],
            int(cutoff_sequence or 0),
            scope.get("session_id"),
            scope.get("session_id"),
            scope.get("discussion_id"),
            scope.get("discussion_id"),
        ),
    )
    return bool(row)


class TriggerPolicy:
    """状态确认和策略复核的触发策略。"""

    @staticmethod
    def should_run_state_detector(
        group_id: int,
        cutoff_sequence: int,
        rule_assessment: dict,
        *,
        trigger_type: str = "student_message",
        new_student_message_count: int = None,
        context: dict = None,
    ) -> dict:
        scores = _score_map(rule_assessment)
        new_count = (
            int(new_student_message_count)
            if new_student_message_count is not None
            else _count_student_messages_since(group_id)
        )
        is_help_trigger = trigger_type in STUDENT_HELP_TRIGGER_TYPES

        if not STATE_LLM_ENABLED:
            return _gate_result(False, "state_llm_disabled", scores=scores, new_student_message_count=new_count)
        if not SERA_LLM_ENABLED:
            return _gate_result(False, "sera_llm_disabled", scores=scores, new_student_message_count=new_count)
        if trigger_type == "silence_check":
            return _gate_result(False, "silence_rule_owns_negative_silence", scores=scores, new_student_message_count=new_count)

        group = query_one("SELECT state FROM groups WHERE id=?", (group_id,))
        if group and group["state"] == "CLOSED":
            return _gate_result(False, "session_or_room_locked", scores=scores, new_student_message_count=new_count)
        if group and group["state"] == "AI_INTERVENING" and not _is_stage1_internal_lock(
            group_id,
            cutoff_sequence,
            context,
        ):
            return _gate_result(False, "session_or_room_locked", scores=scores, new_student_message_count=new_count)

        if _has_completed_same_cutoff(group_id, cutoff_sequence, trigger_type=trigger_type):
            return _gate_result(False, "same_cutoff_already_successful", scores=scores, new_student_message_count=new_count)

        if not _has_valid_student_text(context or {}):
            return _gate_result(False, "no_valid_student_text", scores=scores, new_student_message_count=new_count)

        if is_help_trigger:
            return _gate_result(True, "student_help_trigger", scores=scores, new_student_message_count=new_count)

        min_new = max(0, int(STATE_LLM_MIN_NEW_MESSAGES or 0))
        if new_count < min_new:
            return _gate_result(False, "insufficient_new_student_messages", scores=scores, new_student_message_count=new_count)

        max_risk_score = max(
            [float(scores.get(state) or 0.0) for state in STATE_DETECTOR_RISK_STATES] or [0.0]
        )
        if max_risk_score >= float(STATE_LLM_GATE_MIN_RULE_SCORE or 0.0):
            return _gate_result(True, "risk_rule_score_gate", scores=scores, new_student_message_count=new_count)

        if _has_ambiguous_candidate(rule_assessment, scores):
            return _gate_result(True, "ambiguous_rule_scores", scores=scores, new_student_message_count=new_count)

        force_after = max(1, int(STATE_LLM_FORCE_AFTER_NEW_MESSAGES or 1))
        candidate_state = (
            (rule_assessment or {}).get("candidate_state")
            or (rule_assessment or {}).get("winning_state_code")
            or "unknown"
        )
        if candidate_state == "unknown" and new_count >= force_after:
            return _gate_result(True, "periodic_unknown_confirmation", scores=scores, new_student_message_count=new_count)

        if _has_multi_member_coordination_risk(context or {}, rule_assessment, scores):
            return _gate_result(True, "multi_member_weak_risk_pattern", scores=scores, new_student_message_count=new_count)

        if float(scores.get("positive_collaboration") or 0.0) >= float(STATE_LLM_GATE_MIN_RULE_SCORE or 0.0):
            return _gate_result(True, "positive_collaboration_periodic_check", scores=scores, new_student_message_count=new_count)

        return _gate_result(False, "state_detector_gate_not_met", scores=scores, new_student_message_count=new_count)

    @staticmethod
    def should_enqueue_strategy_review(
        group_id: int,
        cutoff_sequence: int,
        rule_assessment: dict = None,
        *,
        final_state: str = None,
        confidence: float = None,
        trigger_type: str = "new_message",
    ) -> (bool, str):
        """返回是否应该进入自动策略复核候选队列。"""
        existing = MonitorRunRepo.find_by_unique_key(group_id, cutoff_sequence, trigger_type=trigger_type)
        if existing and existing["status"] in ("completed", "skipped"):
            return False, "same_cutoff_already_detected"

        group = query_one("SELECT state FROM groups WHERE id=?", (group_id,))
        if group and group["state"] == "AI_INTERVENING":
            return False, "room_already_intervening"

        state_code, score = _strategy_state_and_confidence(
            rule_assessment=rule_assessment,
            final_state=final_state,
            confidence=confidence,
        )

        if state_code in {"positive_collaboration", "unknown"}:
            return False, f"{state_code}_no_strategy_review_needed"

        if state_code not in STRATEGY_REVIEW_STATES:
            return False, f"non_review_state_{state_code}"

        if score < 0.4:
            return False, f"state_confidence_{score:.2f}_below_review_threshold"

        return True, f"state_review_{state_code}_confidence_{score:.2f}"

    @staticmethod
    def should_call_llm(
        group_id: int,
        cutoff_sequence: int,
        rule_assessment: dict,
        trigger_type: str = "new_message",
    ) -> (bool, str):
        """Deprecated compatibility wrapper for strategy-review enqueue checks."""
        return TriggerPolicy.should_enqueue_strategy_review(
            group_id,
            cutoff_sequence,
            rule_assessment,
            trigger_type=trigger_type,
        )
