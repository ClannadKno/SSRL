# -*- coding: utf-8 -*-
"""Stage 1 rule-screening support for the three-stage strategy pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional
import sqlite3
import uuid

from db import db, now_str, query_one
from services.three_stage_schema import dumps_json


STAGE1_SCHEMA_VERSION = "stage1.v1"
STAGE1_LOCK_PROMPT = "学习助手正在分析当前讨论，请稍候。"
STUDENT_HELP_TRIGGER_TYPES = {"student_help", "student_help_request", "help_request"}

LEGACY_STATE_TO_STAGE1 = {
    "positive_collaboration": {
        "coarse_state_code": "POSSIBLE_POSITIVE",
        "candidate_sub_states": ("standard", "execution_progress", "stage_achievement"),
        "risk_group": "LOW",
    },
    "negative_silence": {
        "coarse_state_code": "POSSIBLE_SILENCE",
        "candidate_sub_states": ("perfunctory_detachment", "individual_marginalization", "burnout"),
        "risk_group": "MEDIUM",
    },
    "conflict_tension": {
        "coarse_state_code": "POSSIBLE_CONFLICT",
        "candidate_sub_states": (
            "constructive_conflict",
            "interpersonal_conflict",
            "psychological_safety_risk",
        ),
        "risk_group": "HIGH",
    },
    "blocked_frustration": {
        "coarse_state_code": "POSSIBLE_BLOCKED",
        "candidate_sub_states": ("confusion", "frustration", "high_intensity_overload"),
        "risk_group": "MEDIUM",
    },
    "task_detached": {
        "coarse_state_code": "POSSIBLE_DETACHMENT",
        "candidate_sub_states": (
            "off_topic_unregulated",
            "off_topic_self_regulated",
            "perfunctory_detachment",
        ),
        "risk_group": "MEDIUM",
    },
    "unknown": {
        "coarse_state_code": "UNKNOWN_COARSE",
        "candidate_sub_states": ("unknown_sub_state",),
        "risk_group": "LOW",
    },
}
RISK_LEGACY_STATES = {
    "negative_silence",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
}
TRIGGER_PRIORITIES = {
    "student_help": 1,
    "student_help_request": 1,
    "help_request": 1,
    "silence_check": 3,
    "student_message": 6,
    "new_message": 6,
}


@dataclass(frozen=True)
class Stage1Result:
    schema_version: str
    pipeline_mode: str
    pipeline_run_id: Optional[int]
    run_uuid: Optional[str]
    persisted: bool
    idempotency_key: str
    group_id: int
    session_id: Optional[int]
    session_no: Optional[int]
    discussion_id: Optional[int]
    task_id: Optional[int]
    trigger_source: str
    trigger_message_id: Optional[int]
    trigger_priority: int
    input_start_sequence: Optional[int]
    input_end_sequence: Optional[int]
    input_cutoff_student_sequence: int
    coarse_decision: str
    coarse_state_code: str
    coarse_risk_group: str
    coarse_should_escalate: bool
    coarse_confidence: float
    candidate_sub_states: tuple[str, ...]
    rule_scores: dict[str, Any]
    quantitative_features: dict[str, Any]
    evidence_message_ids: list[int]
    reason_codes: list[str]
    requires_room_lock: bool
    requires_stage2: bool
    lock_prompt: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate_sub_states"] = list(self.candidate_sub_states)
        return data


class Stage1PipelineService:
    """Build and persist preliminary first-stage rule-screening runs.

    Stage 1 is intentionally lock-free.  A persisted row is scheduling input
    for an authoritative assessment batch, not a consumer that may own the
    student-input room lease.
    """

    @staticmethod
    def build_result(
        *,
        group_id: int,
        monitor_run_id: int,
        trigger_type: str,
        cutoff_sequence: int,
        scope: dict,
        window_start_sequence: Optional[int],
        window_end_sequence: Optional[int],
        new_student_message_count: int,
        rule_assessment: dict,
        rule_scores: dict,
        evidence_message_ids: list[int],
        features: dict,
        state_gate: dict = None,
        pipeline_mode: str = "strategy",
    ) -> Stage1Result:
        if pipeline_mode not in {"strategy", "state_only"}:
            raise ValueError("invalid_pipeline_mode")
        trigger = trigger_type or "new_message"
        winning_state = (
            (rule_assessment or {}).get("winning_state_code")
            or (rule_assessment or {}).get("state_code")
            or "unknown"
        )
        state_config = LEGACY_STATE_TO_STAGE1.get(winning_state, LEGACY_STATE_TO_STAGE1["unknown"])
        is_help = trigger in STUDENT_HELP_TRIGGER_TYPES
        confidence = _coerce_confidence((rule_assessment or {}).get("winning_score"))
        if is_help:
            coarse_decision = "URGENT_ESCALATE"
            coarse_state_code = "EXPLICIT_HELP"
            coarse_risk_group = "URGENT"
            candidate_sub_states = ("confusion", "frustration", "interpersonal_conflict")
        elif winning_state in RISK_LEGACY_STATES:
            coarse_decision = "ESCALATE"
            coarse_state_code = state_config["coarse_state_code"]
            coarse_risk_group = state_config["risk_group"]
            candidate_sub_states = state_config["candidate_sub_states"]
        else:
            coarse_decision = "NO_STRONG_INTERVENTION"
            coarse_state_code = state_config["coarse_state_code"]
            coarse_risk_group = state_config["risk_group"]
            candidate_sub_states = state_config["candidate_sub_states"]

        quantitative_features = _quantitative_features(
            features,
            new_student_message_count=new_student_message_count,
            state_gate=state_gate or {},
            candidate_sub_states=candidate_sub_states,
        )
        idempotency_key = _stage1_idempotency_key(
            group_id=group_id,
            session_id=scope.get("session_id"),
            discussion_id=scope.get("discussion_id"),
            cutoff_sequence=cutoff_sequence,
            trigger_type=trigger,
        )
        reason_codes = _reason_codes(
            trigger_type=trigger,
            winning_state=winning_state,
            rule_assessment=rule_assessment or {},
            features=features or {},
            state_gate=state_gate or {},
        )
        return Stage1Result(
            schema_version=STAGE1_SCHEMA_VERSION,
            pipeline_mode=pipeline_mode,
            pipeline_run_id=None,
            run_uuid=None,
            persisted=False,
            idempotency_key=idempotency_key,
            group_id=int(group_id),
            session_id=_optional_int(scope.get("session_id")),
            session_no=_optional_int(scope.get("session_no")),
            discussion_id=_optional_int(scope.get("discussion_id")),
            task_id=_optional_int(scope.get("task_id")),
            trigger_source=trigger,
            trigger_message_id=_optional_int(scope.get("message_id") or scope.get("scope_message_id")),
            trigger_priority=TRIGGER_PRIORITIES.get(trigger, 6),
            input_start_sequence=_optional_int(window_start_sequence),
            input_end_sequence=_optional_int(window_end_sequence or cutoff_sequence),
            input_cutoff_student_sequence=int(cutoff_sequence or 0),
            coarse_decision=coarse_decision,
            coarse_state_code=coarse_state_code,
            coarse_risk_group=coarse_risk_group,
            coarse_should_escalate=coarse_decision != "NO_STRONG_INTERVENTION",
            coarse_confidence=confidence,
            candidate_sub_states=tuple(candidate_sub_states),
            rule_scores=dict(rule_scores or {}),
            quantitative_features=quantitative_features,
            evidence_message_ids=_unique_ints(evidence_message_ids),
            reason_codes=reason_codes,
            requires_room_lock=False,
            requires_stage2=True,
            lock_prompt=None,
        )

    @staticmethod
    def persist(result: Stage1Result) -> Stage1Result:
        existing = _pipeline_row_by_key(result.idempotency_key)
        if existing:
            return _with_persistence(result, existing["id"], existing["run_uuid"], persisted=False)

        timestamp = now_str()
        run_uuid = str(uuid.uuid4())
        quant = dict(result.quantitative_features or {})
        quant["candidate_sub_states"] = list(result.candidate_sub_states)
        conn = db()
        try:
            cursor = conn.execute(
                """
                INSERT INTO strategy_pipeline_runs(
                    run_uuid, pipeline_mode,
                    group_id, session_id, session_no, discussion_id, task_id,
                    trigger_source, trigger_message_id, trigger_priority,
                    input_start_sequence, input_end_sequence, input_cutoff_student_sequence,
                    stage1_status, stage1_started_at, stage1_completed_at,
                    coarse_decision, coarse_state_code, coarse_risk_group,
                    coarse_should_escalate, coarse_confidence,
                    coarse_rule_scores_json, coarse_quantitative_features_json,
                    coarse_evidence_message_ids_json, coarse_reason_codes_json,
                    stage2_status, publish_status, final_status, skip_reason,
                    idempotency_key, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_uuid,
                    result.pipeline_mode,
                    result.group_id,
                    result.session_id,
                    result.session_no,
                    result.discussion_id,
                    result.task_id,
                    result.trigger_source,
                    result.trigger_message_id,
                    result.trigger_priority,
                    result.input_start_sequence,
                    result.input_end_sequence,
                    result.input_cutoff_student_sequence,
                    "SUCCEEDED",
                    timestamp,
                    timestamp,
                    result.coarse_decision,
                    result.coarse_state_code,
                    result.coarse_risk_group,
                    1 if result.coarse_should_escalate else 0,
                    result.coarse_confidence,
                    dumps_json(result.rule_scores),
                    dumps_json(quant),
                    dumps_json(result.evidence_message_ids),
                    dumps_json(result.reason_codes),
                    "PENDING",
                    "NOT_READY",
                    "PENDING_STAGE2",
                    None if result.requires_stage2 else "stage1_no_stage2_required",
                    result.idempotency_key,
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
            return _with_persistence(result, cursor.lastrowid, run_uuid, persisted=True)
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = _pipeline_row_by_key(result.idempotency_key)
            if existing:
                return _with_persistence(result, existing["id"], existing["run_uuid"], persisted=False)
            raise
        finally:
            conn.close()

    @staticmethod
    def acquire_room_lock(result: Stage1Result, *, enabled: bool) -> dict[str, Any]:
        """Return the explicit lock-free lifecycle result for preliminary work.

        The method remains as a compatibility boundary for older callers, but
        it must never mutate ``groups`` or write lease fields to the pipeline.
        """
        if result.trigger_source in STUDENT_HELP_TRIGGER_TYPES:
            return {
                "attempted": False,
                "acquired": False,
                "reason": "student_help_lock_owned_by_help_pipeline",
            }
        return {
            "attempted": False,
            "acquired": False,
            "reason": "PRELIMINARY_NO_LOCK",
            "pipeline_run_id": result.pipeline_run_id,
        }

    @staticmethod
    def finalize_without_stage2(
        result: Stage1Result,
        *,
        reason: str = "PRELIMINARY_NO_STAGE2_CONSUMER",
    ) -> dict[str, Any]:
        """Close a lock-free preliminary row that has no Stage 2 consumer.

        Silence checks run outside the authoritative assessment-batch worker.
        Leaving their audit rows in ``PENDING_STAGE2`` makes completed,
        lock-free diagnostics look like active Agent work forever.
        """
        if not result.pipeline_run_id:
            return {"terminalized": False, "reason": "pipeline_not_persisted"}
        timestamp = now_str()
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE strategy_pipeline_runs
                   SET stage2_status='SKIPPED',
                       stage2_completed_at=COALESCE(stage2_completed_at, ?),
                       publish_status='SKIPPED',
                       final_status='SKIPPED',
                       skip_reason=?,
                       updated_at=?
                 WHERE id=?
                   AND assessment_batch_id IS NULL
                   AND room_lock_token IS NULL
                   AND UPPER(COALESCE(stage2_status, 'PENDING'))='PENDING'
                   AND UPPER(COALESCE(final_status, 'PENDING_STAGE2'))='PENDING_STAGE2'
                """,
                (timestamp, str(reason), timestamp, int(result.pipeline_run_id)),
            )
            row = conn.execute(
                "SELECT stage2_status, publish_status, final_status, skip_reason "
                "FROM strategy_pipeline_runs WHERE id=?",
                (int(result.pipeline_run_id),),
            ).fetchone()
            conn.commit()
            return {
                "terminalized": cursor.rowcount == 1,
                "pipeline_run_id": int(result.pipeline_run_id),
                "stage2_status": row["stage2_status"] if row else None,
                "publish_status": row["publish_status"] if row else None,
                "final_status": row["final_status"] if row else None,
                "reason": row["skip_reason"] if row else str(reason),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _pipeline_row_by_key(idempotency_key: str):
    return query_one(
        "SELECT id, run_uuid FROM strategy_pipeline_runs WHERE idempotency_key=?",
        (idempotency_key,),
    )


def _with_persistence(
    result: Stage1Result,
    pipeline_run_id: int,
    run_uuid: str,
    *,
    persisted: bool,
) -> Stage1Result:
    data = result.to_dict()
    data.update(
        {
            "pipeline_run_id": int(pipeline_run_id),
            "run_uuid": run_uuid,
            "persisted": bool(persisted),
            "candidate_sub_states": tuple(data["candidate_sub_states"]),
        }
    )
    return Stage1Result(**data)


def _stage1_idempotency_key(
    *,
    group_id: int,
    session_id: Optional[int],
    discussion_id: Optional[int],
    cutoff_sequence: int,
    trigger_type: str,
) -> str:
    return (
        "stage1:"
        f"g={int(group_id)}:"
        f"sid={_optional_int(session_id) or 0}:"
        f"did={_optional_int(discussion_id) or 0}:"
        f"seq={int(cutoff_sequence or 0)}:"
        f"trigger={trigger_type or 'new_message'}"
    )


def _optional_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_confidence(value) -> float:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError):
        numeric = 0.0
    return round(max(0.0, min(1.0, numeric)), 3)


def _unique_ints(values) -> list[int]:
    result = []
    for value in values or []:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item not in result:
            result.append(item)
    return result


def _quantitative_features(
    features: dict,
    *,
    new_student_message_count: int,
    state_gate: dict,
    candidate_sub_states: tuple[str, ...],
) -> dict[str, Any]:
    behavior = (features or {}).get("behavior") or {}
    text = (features or {}).get("text") or {}
    return {
        "new_student_message_count": int(new_student_message_count or 0),
        "student_message_count": _optional_int(behavior.get("student_message_count")) or 0,
        "active_member_count": _optional_int(behavior.get("active_member_count")) or 0,
        "unique_student_speakers": _optional_int(behavior.get("unique_student_speakers")) or 0,
        "message_count_by_member": behavior.get("message_count_by_member") or {},
        "message_share_by_member": behavior.get("message_share_by_member") or {},
        "character_count_by_member": behavior.get("character_count_by_member") or {},
        "character_share_by_member": behavior.get("character_share_by_member") or {},
        "participation_entropy": behavior.get("participation_entropy"),
        "dominant_member_share": behavior.get("dominant_member_share"),
        "minimum_member_share": behavior.get("minimum_member_share"),
        "dominant_speaker_recent_share": behavior.get("dominant_speaker_recent_share"),
        "participation_imbalance": behavior.get("member_participation_imbalance", behavior.get("participation_imbalance")),
        "legacy_rule_participation_imbalance": behavior.get("participation_imbalance"),
        "seconds_since_last_message": behavior.get("seconds_since_last_message") or {},
        "messages_in_last_5_minutes": behavior.get("messages_in_last_5_minutes") or {},
        "messages_in_last_10_minutes": behavior.get("messages_in_last_10_minutes") or {},
        "consecutive_group_messages_without_member": behavior.get("consecutive_group_messages_without_member") or {},
        "mean_message_interval": behavior.get("mean_message_interval"),
        "median_message_interval": behavior.get("median_message_interval"),
        "p90_message_interval": behavior.get("p90_message_interval"),
        "messages_per_minute": behavior.get("messages_per_minute"),
        "messages_per_minute_1m": behavior.get("messages_per_minute_1m"),
        "messages_per_minute_5m": behavior.get("messages_per_minute_5m"),
        "turn_switch_rate": behavior.get("turn_switch_rate"),
        "same_speaker_streak_max": behavior.get("same_speaker_streak_max"),
        "burst_message_ratio": behavior.get("burst_message_ratio"),
        "silence_seconds": behavior.get("silence_seconds"),
        "short_message_ratio": behavior.get("short_message_ratio"),
        "repeated_message_ratio": behavior.get("repeated_message_ratio"),
        "interaction_intensity_score": behavior.get("interaction_intensity_score"),
        "task_progress": behavior.get("task_progress"),
        "conflict_hits": _optional_int(text.get("conflict_hits")) or 0,
        "frustration_hits": _optional_int(text.get("frustration_hits")) or 0,
        "low_motivation_hits": _optional_int(text.get("low_motivation_hits")) or 0,
        "off_task_hits": _optional_int(text.get("off_task_hits")) or 0,
        "passive_detachment_hits": _optional_int(text.get("passive_detachment_hits")) or 0,
        "self_regulation_hits": _optional_int(text.get("self_regulation_hits")) or 0,
        "task_relevance_score": text.get("task_relevance_score"),
        "semantic_repetition_score": text.get("semantic_repetition_score"),
        "state_detector_gate": bool((state_gate or {}).get("gate")),
        "state_detector_gate_reason": (state_gate or {}).get("gate_reason"),
        "candidate_sub_states": list(candidate_sub_states),
    }


def _reason_codes(
    *,
    trigger_type: str,
    winning_state: str,
    rule_assessment: dict,
    features: dict,
    state_gate: dict,
) -> list[str]:
    reasons = []
    if trigger_type in STUDENT_HELP_TRIGGER_TYPES:
        reasons.append("EXPLICIT_HELP_TRIGGER")
    if winning_state:
        reasons.append(f"RULE_STATE_{str(winning_state).upper()}")
    gate_reason = (state_gate or {}).get("gate_reason")
    if gate_reason:
        reasons.append(f"GATE_{str(gate_reason).upper()}")
    for tag in (((features or {}).get("text") or {}).get("evidence_tags") or [])[:4]:
        normalized = str(tag or "").strip().upper()
        if normalized:
            reasons.append(f"EVIDENCE_{normalized}")
    for candidate in (rule_assessment or {}).get("candidates") or []:
        state_code = candidate.get("state_code")
        if state_code == winning_state:
            for signal in candidate.get("signals") or []:
                if signal:
                    reasons.append(f"SIGNAL_{str(signal).upper()}")
            break
    deduped = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped[:12]
